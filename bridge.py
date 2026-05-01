"""GoodWe LAN HTTP bridge.

Tiny aiohttp service that wraps the goodwe Python library to expose
poweron / poweroff / status over HTTP, for callers that can't speak
UDP-Modbus (e.g. HomeyScript). Talks to the inverter on UDP 8899.
"""
from __future__ import annotations

import asyncio
import hmac
import logging
import os
import sys
import time
from typing import Any, Awaitable, Callable

import goodwe
from aiohttp import web

LOG = logging.getLogger("goodwe-bridge")


def env_int(name: str, default: int) -> int:
    v = os.environ.get(name)
    return int(v) if v else default


GOODWE_HOST = os.environ.get("GOODWE_HOST")
GOODWE_FAMILY = os.environ.get("GOODWE_FAMILY", "DT")
LAN_TIMEOUT = env_int("LAN_TIMEOUT", 2)
LAN_RETRIES = env_int("LAN_RETRIES", 3)
BRIDGE_PORT = env_int("BRIDGE_PORT", 8765)
BRIDGE_TOKEN = os.environ.get("BRIDGE_TOKEN") or None


class HttpError(Exception):
    def __init__(self, code: str, detail: str, status: int):
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.status = status


def err_json(error: str, detail: str, status: int) -> web.Response:
    return web.json_response({"ok": False, "error": error, "detail": detail}, status=status)


@web.middleware
async def auth_middleware(request: web.Request, handler):
    if BRIDGE_TOKEN and request.path != "/health":
        header = request.headers.get("Authorization", "")
        prefix = "Bearer "
        if not header.startswith(prefix) or not hmac.compare_digest(
            header[len(prefix) :], BRIDGE_TOKEN
        ):
            return err_json("unauthorized", "missing or invalid Bearer token", 401)
    t0 = time.time()
    try:
        resp = await handler(request)
    except HttpError as e:
        resp = err_json(e.code, e.detail, e.status)
    except Exception as e:
        LOG.exception("unhandled error in handler")
        resp = err_json("server_error", f"{type(e).__name__}: {e}", 500)
    elapsed_ms = (time.time() - t0) * 1000
    LOG.info("%s %s -> %s (%.0fms)", request.method, request.path, resp.status, elapsed_ms)
    return resp


async def get_inverter(app: web.Application) -> goodwe.Inverter:
    """Lazily connect; cache the handle in app['inv']."""
    inv = app.get("inv")
    if inv is None:
        if not GOODWE_HOST:
            raise HttpError("server_error", "GOODWE_HOST env var not set", 500)
        LOG.info("connecting to inverter %s family=%s", GOODWE_HOST, GOODWE_FAMILY)
        inv = await goodwe.connect(
            host=GOODWE_HOST,
            family=GOODWE_FAMILY,
            timeout=LAN_TIMEOUT,
            retries=LAN_RETRIES,
        )
        app["inv"] = inv
        LOG.info("connected: model=%s sn=%s", getattr(inv, "model_name", "?"),
                 getattr(inv, "serial_number", "?"))
    return inv


async def call_inverter(
    app: web.Application,
    fn: Callable[[goodwe.Inverter], Awaitable[Any]],
) -> Any:
    """Run fn(inv); on any transport error, drop the cached handle and
    surface a clear HttpError. Wraps both the connect and the call."""
    try:
        inv = await get_inverter(app)
        return await fn(inv)
    except (TimeoutError, asyncio.TimeoutError) as e:
        app["inv"] = None
        raise HttpError("timeout", f"{type(e).__name__}: {e}", 504)
    except OSError as e:
        app["inv"] = None
        raise HttpError("unreachable", f"{type(e).__name__}: {e}", 502)
    except goodwe.InverterError as e:
        app["inv"] = None
        raise HttpError("inverter_error", f"{type(e).__name__}: {e}", 502)


async def write_setting(app: web.Application, action: str, setting: str) -> dict[str, Any]:
    lock: asyncio.Lock = app["lock"]
    if lock.locked():
        raise HttpError("busy", "another write is in flight", 409)
    async with lock:
        async def do(inv: goodwe.Inverter) -> dict[str, Any]:
            rt = await inv.read_runtime_data()
            await inv.write_setting(setting, 1)
            return {
                "ok": True,
                "action": action,
                "work_mode_before": rt.get("work_mode_label"),
                "ppv1": rt.get("ppv1"),
            }
        return await call_inverter(app, do)


async def handle_poweron(request: web.Request) -> web.Response:
    payload = await write_setting(request.app, "poweron", "start")
    return web.json_response(payload)


async def handle_poweroff(request: web.Request) -> web.Response:
    payload = await write_setting(request.app, "poweroff", "stop")
    return web.json_response(payload)


async def handle_status(request: web.Request) -> web.Response:
    async def do(inv: goodwe.Inverter) -> dict[str, Any]:
        rt = await inv.read_runtime_data()
        keys = (
            "work_mode", "work_mode_label", "ppv1", "ppv", "pgrid",
            "vac1", "fac1", "temperature", "e_day", "e_total",
        )
        return {"ok": True, **{k: rt[k] for k in keys if k in rt}}
    payload = await call_inverter(request.app, do)
    return web.json_response(payload)


async def handle_health(request: web.Request) -> web.Response:
    """Process liveness — never touches the inverter, so the container is
    not restart-looped at night when the inverter is unreachable."""
    return web.json_response({"ok": True, "status": "alive"})


async def handle_ready(request: web.Request) -> web.Response:
    """Probes the inverter. For human diagnostics; not used by Docker HEALTHCHECK."""
    async def do(inv: goodwe.Inverter) -> dict[str, Any]:
        await inv.read_runtime_data()
        return {
            "ok": True,
            "model": getattr(inv, "model_name", None),
            "serial": getattr(inv, "serial_number", None),
        }
    payload = await call_inverter(request.app, do)
    return web.json_response(payload)


async def on_cleanup(app: web.Application) -> None:
    LOG.info("shutting down")
    app["inv"] = None


def make_app() -> web.Application:
    app = web.Application(middlewares=[auth_middleware])
    app["lock"] = asyncio.Lock()
    app["inv"] = None
    app.router.add_post("/poweron", handle_poweron)
    app.router.add_post("/poweroff", handle_poweroff)
    app.router.add_get("/status", handle_status)
    app.router.add_get("/health", handle_health)
    app.router.add_get("/ready", handle_ready)
    app.on_cleanup.append(on_cleanup)
    return app


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stdout,
    )
    if not GOODWE_HOST:
        LOG.error("GOODWE_HOST env var is required")
        sys.exit(2)
    LOG.info(
        "bridge starting: port=%s host=%s family=%s auth=%s",
        BRIDGE_PORT, GOODWE_HOST, GOODWE_FAMILY,
        "enabled" if BRIDGE_TOKEN else "disabled",
    )
    web.run_app(
        make_app(),
        host="0.0.0.0",
        port=BRIDGE_PORT,
        print=lambda *a, **k: None,
    )


if __name__ == "__main__":
    main()
