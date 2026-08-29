# OpenCode IP Rotator & Proxy Server

[![GitHub release](https://img.shields.io/github/v/release/alztrk/opencode-ip-rotator?style=flat-square&color=blue)](https://github.com/alztrk/opencode-ip-rotator)
[![Docker Image](https://img.shields.io/badge/docker-unified-blue.svg?style=flat-square&logo=docker)](https://github.com/Sprtcrnbry/opencode-ip-rotator/pkgs/container/opencode-ip-rotator)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg?style=flat-square)](https://opensource.org/licenses/MIT)
[![Python 3.11](https://img.shields.io/badge/python-3.11-brightgreen.svg?style=flat-square&logo=python)](https://python.org)

Unified Cloudflare WARP IP rotator and OpenAI/Anthropic/Responses proxy for OpenCode Zen. Single container (`warp-svc` + `wireguard-go`/`wireproxy` fallbacks + FastAPI) with verified IP rotation, SQLite persistence, and a live dashboard. Built to survive hosts without TUN or with broken `nft` — it falls back automatically to real WireGuard (`wg-quick` → kernel or `wireguard-go`) and then to a userspace SOCKS5 (`wgcf` + `wireproxy`) before proxies/direct.

![OpenCode IP Rotator Dashboard Preview](docs/dashboard_preview.jpg)

---

## Key Features

- **Unified single container** — `warp-svc`/`wireguard-go`/`wireproxy` + FastAPI in one image, no IPC, in-memory flow locking.
- **Resilient egress chain** — `WARP tunnel → WARP proxy (40000) → WireGuard tunnel (wgcf0, kernel or wireguard-go) → WireGuard SOCKS5 (wgcf+wireproxy :41000) → custom proxies → direct` — verified via `cloudflare.com/cdn-cgi/trace`.
- **Zero-latency streaming** — SSE passthrough with lease-guarded rotation, HTTP/2 keep-alive pool (`curl_cffi` `chrome124`).
- **Dashboard** (`/dashboard`) — IP, location, active flows, rotations, model usage, inspector for recent requests.
- **SQLite persistence** — `data/metrics.db` (WAL) stores model usage, `ip_history` (20 in-mem, 100 on disk), warp quality.
- **Model auto-discovery** — `models.dev` + upstream `/v1/models` enrich every model with context/output/reasoning/tool/attachment metadata; `big-pickle` + `*-free` filtered.
- **OpenAI / Anthropic / Responses compat** — `/v1/chat/completions`, `/v1/messages`, `/v1/responses`, `/v1/models`.
- **Custom proxy pool** — `data/proxies.txt` or `PROXY_LIST` env, round-robin, used as last fallback and as outbound proxy for upstream calls.
- **Safety** — flow-lease + `rotation_lock` prevents stream truncation, `ROTATION_DRAIN_TIMEOUT`, idempotent warp re-registration, Prometheus metrics.

---

## Architecture

```
[OpenCode Client] ──(HTTP/2)──> [Unified Proxy & WARP Node :8000] ──(SQLite)──> data/metrics.db
                                           │
          WARP tunnel → WARP proxy → WireGuard tunnel → WireGuard SOCKS5 → proxies → direct
                                           ▼
                              [opencode.ai/zen/v1]
```

**Components**

1. **`server.py`** — FastAPI on `127.0.0.1:8000` ( `0.0.0.0` in Docker). Forwards `x-opencode-*`, `Authorization: Bearer public`, streams SSE, exposes `/dashboard`, `/metrics`, `/health`, `/api/rotate`.
2. **`rotator.py`** — In-process WARP rotation, health checks (`WARP_CHECK_INTERVAL`), periodic rotation, two-phase WireGuard rotation (light restart → `wgcf` re-register), lease DB `active_flow_leases`.
3. **`entrypoint.sh`** — Starts `dbus`/`warp-svc`, waits for daemon (caps restarts at 3, detects `nft` failures), registers WARP, connects `warp` → `proxy` → `wg-quick/wireguard-go` → `wireproxy`; writes `/tmp/warp-env` (`CUSTOM_OUTBOUND_PROXY`) for `server.py`.
4. **`manager.py`** — `docker compose down -v && up -d --build` recycle when `AUTO_RECYCLE_THRESHOLD` hit.

---

## Quick Start

```bash
python setup.py   # pip install, warp-cli check, writes ~/.config/opencode/opencode.jsonc
```

---

## Installation

### Docker (recommended)

Isolates WARP/WireGuard, gives a fresh `/etc/machine-id` on each recreate, and works on Docker Desktop (Mac/Win) via the `wireproxy` fallback even without host TUN.

**Prereqs:** Docker Engine 20.10+, Compose v2+.

**Pull and launch** — images publish to GHCR on every `master` push, no local build needed:

```bash
docker login ghcr.io
docker compose up -d
```

Dashboard: `http://127.0.0.1:8000/dashboard`

**Recreate with fresh identity:**

```bash
python manager.py
# or
docker compose down && docker compose up -d --build
```

No `proxies.txt` required — WARP/WireGuard is the primary egress. Add `data/proxies.txt` only for a custom pool.

### Native (Linux / Windows)

**Prereqs:** Python 3.10+, Cloudflare WARP installed (`warp-cli` on PATH), admin/root for WARP daemon. `wgcf`/`wireproxy`/`wireguard-go` are Docker-only; native falls back to `warp-cli` → custom proxies → direct.

```bash
pip install -r requirements.txt
python server.py          # unified proxy + in-process rotator
# legacy split (still works):
# python rotator.py       # in another terminal
# python server.py
```

---

## Configuration

### API key

Always use `public`:

```
Authorization: Bearer public
# or x-api-key: public
```

The proxy maps dummy keys (`any`, `test`, `dummy`, etc.) to `Bearer public` and forwards `x-opencode-*` headers.

### OpenAI-compatible provider (`opencode.jsonc`)

```jsonc
{
  "provider": {
    "opencode-zen-local": {
      "npm": "@ai-sdk/openai-compatible",
      "options": { "baseURL": "http://127.0.0.1:8000/v1", "apiKey": "public" },
      "name": "OpenCode Zen Local Proxy"
    }
  }
}
```

### Anthropic provider

```jsonc
{
  "provider": {
    "my-anthropic-proxy": {
      "npm": "@ai-sdk/anthropic",
      "options": { "baseURL": "http://127.0.0.1:8000", "apiKey": "public" }
    }
  }
}
```

`/v1/messages` is translated to OpenAI internally and uses the same egress chain.

### Custom proxy pool

**File:** `data/proxies.txt` (one per line, single source — the old root `proxies.txt:/app/proxies.txt` double-mount is removed):

```
http://user:pass@proxy1.example.com:8080
socks5://proxy2.example.com:1080
```

**Env:** `PROXY_LIST="http://proxy1:8080,socks5://proxy2:1080" docker compose up -d`

Merged file+env, deduped, round-robin. Consumed as `{"http": url, "https": url}` for `curl_cffi`.

### Egress fallback chain

Container picks the first working egress:

1. **WARP tunnel** `warp-svc` + `warp-cli` `mode warp` (needs TUN + `NET_ADMIN` + working `nft`).
2. **WARP proxy** `warp-cli` `mode proxy` `127.0.0.1:40000` (same daemon, SOCKS5).
3. **WireGuard tunnel** `wgcf` profile → `wg-quick` `wgcf0` (kernel if present, else `wireguard-go` userspace, still needs TUN).
4. **WireGuard SOCKS5** `wgcf` + `wireproxy` `127.0.0.1:41000` (pure userspace, no TUN) — account/profile in `/app/wireguard` (ephemeral layer, survives `restart` but not `down`).
5. **Custom proxies / direct**.

Rotation mirrors the chain. WARP does `disconnect/connect` then `registration delete/new` only when IP didn't change (proxy mode skips delete). WireGuard does light restart with the same profile first, then `wgcf register --accept-tos` + `generate` if still stuck, then tunnel → SOCKS5. All paths verify `cloudflare.com/cdn-cgi/trace` IP changed and record to `ip_history`.

---

## API Endpoints

| Endpoint | Method | Description |
|---|---:|---|
| `/v1/chat/completions` | `POST` | OpenAI chat, retries on 429/5xx, rotates IP, SSE streaming. |
| `/v1/messages` | `POST` | Anthropic compat (translated to OpenAI). |
| `/v1/responses` | `POST` | Responses API (`/v1/responses`). |
| `/v1/models` | `GET` | Discovered `big-pickle` + `*-free` models with context/output/flag metadata. |
| `/dashboard` | `GET` | HTML dashboard. |
| `/metrics` | `GET` | JSON: `verified_public_ip`, `uptime`, `model_usage`, `ip_history`, `warp_quality`, `recent_requests`. |
| `/metrics-prometheus` | `GET` | Prometheus exposition. |
| `/health` | `GET` | `{"status":"healthy","database":"connected",...}` + `proxy_warp_health` gauge. |
| `/api/rotate` | `POST` | Manual rotation (lease-guarded). |
| `/api/recent-requests` | `GET` | Ring buffer (50) of redacted headers + payload summaries. |

Upstream `429` is preserved with `Retry-After`/`X-Rate-Limit-Reason`; 429 triggers async rotation with 10s cooldown (`ROTATION_429_COOLDOWN_SECONDS`) and `ROTATION_DRAIN_TIMEOUT` (30s) to avoid coroutine pile-up.

---

## Environment Variables

| Variable | Default | Description |
|---|---:|---|
| `OPENCODE_ZEN_PORT` | `8000` | Proxy listen port. |
| `OPENCODE_ZEN_HOST` | `127.0.0.1` (`0.0.0.0` in compose) | Bind host. |
| `OPENCODE_ZEN_TARGET_BASE` | `https://opencode.ai/zen/v1` | Upstream base. |
| `WARP_CHECK_ENDPOINT` | `https://opencode.ai` | Health-check HEAD target. |
| `WARP_CHECK_INTERVAL` | `15` | Healthcheck secs (`0` disable). |
| `WARP_ROTATION_INTERVAL` | `86400` | Periodic rotation secs (`0` disable, 86400 = 24h). |
| `WARP_RETRY_DELAY` / `WARP_MAX_RETRIES` | `3` / `5` | Healthcheck retry knobs. |
| `AUTO_RECYCLE_THRESHOLD` | `50` | Rotations before `manager.py` recycle (needs `docker` or `/var/run/docker.sock`). |
| `MAX_RETRIES_ON_429` / `INITIAL_BACKOFF` | `8` / `1` | Per-request upstream retry. |
| `ROTATION_429_COOLDOWN_SECONDS` | `10` | Cooldown for auto-rotation on 429. |
| `ROTATION_DRAIN_TIMEOUT` | `30` | Max wait for in-flight rotation before proceeding. |
| `MAX_CONCURRENT_UPSTREAM` | `80` | Semaphore for upstream `curl_cffi` calls. |
| `FLOW_LEASE_TTL_SECONDS` / `FLOW_LEASE_HEARTBEAT_SECONDS` | `90` / `15` | SSE lease TTL/heartbeat (SQLite `active_flow_leases`). |
| `METRICS_DB_PATH` | `/app/data/metrics.db` | SQLite file (WAL). |
| `PROXY_LIST_FILE` | `/app/data/proxies.txt` | Custom pool file. |
| `PROXY_LIST` | *(empty)* | Comma-separated custom proxies (merged with file). |
| `CUSTOM_OUTBOUND_PROXY` | *(empty)* | Enforced outbound `socks5://…` (set by entrypoint fallback to `:40000` or `:41000`; also read live in rotator). |
| `CORS_ALLOW_ORIGINS` | `http://127.0.0.1:8000,http://localhost:8000` | CORS allowlist. |
| `LOG_LEVEL` / `LOG_FORMAT` | `INFO` / `text` | `text` or `json`. |
| `ENABLE_HTTP2` | `false` | `true` to use HTTP/2 upstream (default `V1_1`). |
| `WARP_ROTATOR_URL` | `http://127.0.0.1:8001` | Legacy split-rotator HTTP fallback; unused in unified mode. |

Compose healthcheck: `curl -sf http://localhost:8000/health | grep '"status"'` `interval:30s` `start_period:60s` (covers WARP+WireGuard startup).

---

## Deploy from GHCR

Every `master` push builds a single unified image:

- `ghcr.io/Sprtcrnbry/opencode-ip-rotator:latest` — proxy + rotator + WARP/WireGuard (root `Dockerfile`, multi-arch `amd64`/`arm64`/`arm` for `wgcf`/`wireproxy` + `wireguard-go` via `go install`).

```bash
docker login ghcr.io
docker compose up -d   # compose already references the published image
```

First start registers a WARP account automatically (`warp-cli` or `wgcf` on fallback). `data/proxies.txt` optional.

---

## Responsibility Disclaimer

Educational / research / resilience-testing use only. You are responsible for complying with upstream ToS and acceptable-use policies. Maintainers assume no liability for suspensions or misuse.

---

## License

[MIT](LICENSE)
