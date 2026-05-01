# goodwe-bridge

Tiny HTTP bridge that exposes a GoodWe inverter's local-LAN poweron / poweroff
control over HTTP, so callers without UDP / Modbus support (HomeyScript,
Node-RED in some configs, shell scripts, …) can drive the inverter.

LAN-only by design — no auth. Don't expose it to the internet.

## Why

The GoodWe SEMS cloud `SaveRemoteControlInverter` endpoint accepts
`InverterStatus=1` (poweron) and returns `code: 0 "Successful"` — but on at
least the GW3000D-NS firmware it silently no-ops. The inverter never
transitions out of Wait Mode. The local LAN write to register **40330**
(`start` setting) works reliably and applies in roughly 90 s with sufficient
PV. This bridge exposes that local path over HTTP so a HomeyScript Flow can
call it.

## Endpoints

| Method | Path       | Behaviour                                                                       |
|--------|------------|---------------------------------------------------------------------------------|
| POST   | /poweron   | Writes register 40330 (`start` = 1). Returns immediately on UDP ACK.            |
| POST   | /poweroff  | Writes register 40331 (`stop`  = 1). Returns immediately on UDP ACK.            |
| GET    | /status    | Snapshot of `read_runtime_data`: work_mode_label, ppv1, pgrid, e_day, …         |
| GET    | /health    | Process liveness only — never touches the inverter. Used by Docker HEALTHCHECK. |
| GET    | /ready     | Probes the inverter (`read_runtime_data`). For human diagnostics.               |

All responses are JSON. Success bodies start with `{"ok": true, …}`.
Errors are uniformly `{"ok": false, "error": "<code>", "detail": "…"}` with
HTTP status 4xx/5xx. Error codes:

- `bad_request` (400), `busy` (409),
- `unreachable` (502), `inverter_error` (502), `timeout` (504),
- `server_error` (500).

Concurrent writes: a single-flight asyncio lock causes the second concurrent
`/poweron` or `/poweroff` to return `409 {"error": "busy"}`. Reads are
concurrent-safe.

**The bridge does not poll for transition completion.** A 200 response means
the inverter ACKed the UDP write — it does *not* mean the inverter is now in
Normal mode. Wake-up requires sufficient PV input. Call `/status` ~90 s later
to confirm the actual mode.

## Configuration (env vars in `compose.yaml`)

| Name           | Required | Default     | Notes |
|----------------|----------|-------------|-------|
| `GOODWE_HOST`  | yes      | —           | Inverter LAN IP, e.g. `192.168.20.34` |
| `GOODWE_FAMILY`| no       | `DT`        | goodwe library family hint |
| `BRIDGE_PORT`  | no       | `8765`      | HTTP listen port (inside container) |
| `LAN_TIMEOUT`  | no       | `2`         | UDP timeout (seconds) |
| `LAN_RETRIES`  | no       | `3`         | UDP retries |

## Deploy on a NAS

### Synology DSM 7 / Container Manager (most common)

1. Clone this repo into the docker share:
   ```sh
   git clone https://github.com/mtnnn/goodwe-bridge.git /volume1/docker/goodwe-bridge
   cd /volume1/docker/goodwe-bridge
   ```
2. Edit `compose.yaml` if your inverter isn't at `192.168.20.34`.
3. Container Manager → Project → Create →
   - Path: `/volume1/docker/goodwe-bridge`
   - Source: *Use existing docker-compose.yml*, pick `compose.yaml`.
   - Build & start.

The Project view shows logs and healthcheck status.

### QNAP Container Station 3+

Applications → Create → upload `compose.yaml`. ARM models: build on a dev
machine first (`docker buildx --platform linux/arm64`) and
`docker save … | ssh nas docker load`. All deps are pure Python, so cross-arch
builds work.

### Unraid

Compose Manager plugin → paste `compose.yaml`. Or hand-roll a Docker template.

### Anywhere with `docker compose`

```sh
git clone https://github.com/mtnnn/goodwe-bridge.git
cd goodwe-bridge
docker compose up -d --build
docker compose logs -f goodwe-bridge
```

## Smoke tests

Replace `<host>` with `127.0.0.1` (local) or your NAS IP.

```sh
curl -sS http://<host>:8765/health
# {"ok":true,"status":"alive"}

curl -sS http://<host>:8765/ready
# {"ok":true,"model":"GW3000D-NS","serial":"..."}

curl -sS http://<host>:8765/status
# {"ok":true,"work_mode_label":"Normal","ppv1":1840,...}

curl -sS -X POST http://<host>:8765/poweron
# {"ok":true,"action":"poweron","work_mode_before":"Wait Mode","ppv1":12}

# ~90s later, confirm transition:
curl -sS http://<host>:8765/status
# work_mode_label should now be "Normal" (assuming sufficient PV)
```

## Calling from Homey

HomeyScript can `fetch` the bridge directly. Example for poweron:

```js
const BRIDGE_URL = 'http://192.168.20.10:8765/poweron'; // your NAS IP
const TIMEOUT_MS = 30000;

const ctrl = new AbortController();
const timer = setTimeout(() => ctrl.abort(), TIMEOUT_MS);
let body;
try {
  const r = await fetch(BRIDGE_URL, { method: 'POST', signal: ctrl.signal });
  if (!r.ok) throw new Error(`bridge HTTP ${r.status}: ${await r.text()}`);
  body = await r.json();
} finally {
  clearTimeout(timer);
}
if (body.ok !== true) throw new Error(`bridge rejected: ${JSON.stringify(body)}`);
console.log('local poweron accepted:', body);
return true;
```

For poweroff, change the URL path to `/poweroff` and the log message
accordingly.

## Notes / caveats

- **Single-flight writes.** One `/poweron` or `/poweroff` at a time; the second
  concurrent call gets `409 busy`. Reads are unrestricted.
- **Night-time.** The inverter is fully off and unreachable on UDP. `/health`
  stays green so the container is *not* restart-looped overnight; `/status` and
  `/ready` return 502/504 with clear error JSON. This is intentional.
- **`goodwe` library version.** Pinned to `0.2.29` (D-NS Watts vs percent
  fix). Bumping requires re-verifying writes don't regress on this firmware.

## License

MIT
