# OpenCode IP Rotator & Proxy Server

[![GitHub release](https://img.shields.io/github/v/release/alztrk/opencode-ip-rotator?style=flat-square&color=blue)](https://github.com/alztrk/opencode-ip-rotator)
[![Docker Image](https://img.shields.io/badge/docker-microservices-blue.svg?style=flat-square&logo=docker)](https://github.com/alztrk/opencode-ip-rotator)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg?style=flat-square)](https://opensource.org/licenses/MIT)
[![Python 3.11](https://img.shields.io/badge/python-3.11-brightgreen.svg?style=flat-square&logo=python)](https://python.org)

A microservice-architected Cloudflare WARP IP rotator and proxy server for OpenCode Zen. Designed to prevent HTTP 429 rate limits, guarantee unique IP rotation per cycle, log usage metrics into SQLite, and display real-time statistics on a clean web dashboard.

![OpenCode IP Rotator Dashboard Preview](docs/dashboard_preview.jpg)

---

## Key Features

- **Unified Single Container**: High-performance, all-in-one container combining Cloudflare WARP and FastAPI server with zero IPC overhead and in-memory flow locking.
- **Zero-Latency TTFT**: Immediate token streaming with HTTP/2 keep-alive connection pooling to maximize prompt generation speed.
- **Clean Management Dashboard**: Lightweight Web UI displaying active connections, current location, token statistics, and manual rotation controls.
- **SQLite Data Persistence**: Stores token consumption, model request counts, and historical IP rotation logs on disk.
- **USD Savings Calculator**: Estimates cost savings per model based on prompt and completion token rates.
- **Table Pagination**: Built-in 5-item pagination for model usage and IP rotation log tables.
- **In-Memory Active Flow Locking**: Protects active SSE streams from being interrupted during IP rotation.
- **Anthropic & Responses API Compatibility**: Native `/v1/messages` and `/v1/responses` endpoints.
- **Custom Proxy Pool Support**: Round-robin outbound proxy pool via `data/proxies.txt` or `PROXY_LIST` environment variable.
- **Resilient Egress Fallback Chain**: If the WARP daemon cannot run (missing `/dev/net/tun`, host firewall incompatibility, repeated crashes), the container automatically falls back — first to a real WireGuard tunnel (`wg-quick` on a `wgcf`-generated profile, using the host kernel module or the userspace `wireguard-go` dataplane), then to a pure-userspace SOCKS5 proxy (`wgcf` + `wireproxy`) that needs no kernel WireGuard or `NET_ADMIN`.

---

## Architecture Overview

```
[OpenCode Client] ──(HTTP/2)──> [Unified Proxy & WARP Node (Port 8000)] ──(SQLite)──> [metrics.db]
                                           │
                        (WARP tunnel → WARP proxy → WireGuard tunnel → WireGuard SOCKS5 → proxies → direct)
                                           ▼
                              [OpenCode Zen API Endpoint]
```

### Core Architecture Components

1. **Proxy Server (`server.py`):** An OpenAI, Anthropic, and Responses compatible proxy server running on `http://127.0.0.1:8000`. It processes requests, forwards headers dynamically, handles streaming SSE responses, and presents a Web Management Dashboard.
2. **Rotator Module (`rotator.py`):** Manages WARP tunnel rotation, health monitoring, and IP verification directly in-process.
3. **Container Manager (`manager.py`):** Provides automated ephemeral container lifecycle management.

---

## Detailed Features

- **OpenAI Standard Compatibility:** Fully exposes `/v1/chat/completions` and `/v1/models` endpoints to integrate with standard clients.
- **Dynamic Header Forwarding:** Captures and forwards all incoming client metadata including `x-opencode-*` headers and injects `Authorization: Bearer public` credentials required by the upstream API.
- **Verified Public IP Rotation:** Validates public IP changes via external IP lookup services to guarantee a distinct IP allocation after every disconnection cycle.
- **Active Flow Locking:** Prevents IP rotations during active Server-Sent Events (SSE) streaming sessions to prevent connection truncation and stream drops.
- **Dynamic Model Auto-Discovery:** Periodically queries the upstream API to discover newly available free models without requiring code changes or static lists.
- **Web Management Dashboard:** Includes a clean, dark-themed web interface accessible at `http://127.0.0.1:8000/dashboard` for monitoring public IP status, active connections, total rotations, and triggering manual rotations.

---

## Quick Automated Setup

Run the automated installer script to install Python dependencies, verify system requirements, and automatically configure `~/.config/opencode/opencode.jsonc`:

```bash
python setup.py
```

---

## Installation & Deployment

### Option 1: Docker Container Deployment (Recommended)

Running the project in Docker isolates the execution environment, preventing local network configuration changes and ensuring a new environment identity (`/etc/machine-id`) on every initialization.

#### Prerequisites
- Docker Engine 20.10+
- Docker Compose v2+

#### Pull and Launch

Images publish to GHCR on every push to `master` (see *Deploy from GitHub Container Registry* below). Log in once, then start — no local build required:

```bash
docker login ghcr.io
docker compose up -d
```

#### Access Web Dashboard
Open your browser and navigate to:
`http://127.0.0.1:8000/dashboard`

#### Environment Re-creation
To manually trigger environment self-destruction and re-create a container with fresh hardware identifiers:
```bash
python manager.py
```

---

### Option 2: Local Native Execution

#### Prerequisites
- Python 3.10 or higher
- Cloudflare WARP CLI (`warp-cli`) installed and added to system PATH
- Administrative privileges (required for `warp-cli` operations on Windows)

> The `wgcf`/`wireproxy`/`wireguard-go` fallback stack is bundled in the Docker image and used there only. Native mode relies on the local `warp-cli` (and falls back to the custom proxy pool / direct).

#### Steps

1. Install Python dependencies:
   ```bash
   pip install -r requirements.txt
   ```

2. Start the rotator background service:
   ```bash
   python rotator.py
   ```

3. Launch the proxy server:
   ```bash
   python server.py
   ```

---

## Configuration

### API Key Requirement

Always configure your client or SDK to use **`public`** as the API key (e.g. `Authorization: Bearer public` or `x-api-key: public`). The proxy uses this key to interface with upstream public free-tier models.

---

### OpenAI-Compatible Provider (Default)

To use the local proxy server within OpenCode, update your configuration file at `~/.config/opencode/opencode.jsonc`:

```jsonc
{
  "provider": {
    "opencode-zen-local": {
      "npm": "@ai-sdk/openai-compatible",
      "options": {
        "baseURL": "http://127.0.0.1:8000/v1",
        "apiKey": "public"
      },
      "name": "OpenCode Zen Local Proxy"
    }
  }
}
```

> **Note:** Models are automatically discovered from the proxy server's `/v1/models` endpoint. You do not need to hardcode model names manually.

---

### Anthropic API Provider

The proxy exposes a native Anthropic-compatible `/v1/messages` endpoint. To use it with OpenCode's Anthropic provider:

```jsonc
{
  "provider": {
    "my-anthropic-proxy": {
      "npm": "@ai-sdk/anthropic",
      "options": {
        "baseURL": "http://127.0.0.1:8000",
        "apiKey": "public"
      }
    }
  }
}
```

> Requests sent to `/v1/messages` are translated to OpenAI format internally and routed through the same WARP-protected upstream.

---

### Custom Outbound Proxy Pool

If you want to use your own HTTP/SOCKS5 proxies instead of (or in addition to) Cloudflare WARP:

**Option 1 — File:** Create `data/proxies.txt` with one proxy per line:
```
http://user:pass@proxy1.example.com:8080
socks5://proxy2.example.com:1080
```

**Option 2 — Environment variable:**
```bash
PROXY_LIST="http://proxy1:8080,socks5://proxy2:1080" docker compose up -d
```

The proxy pool rotates in round-robin order across all outbound requests.

### Egress Fallback Chain

The container selects the first working egress path, in order:

1. **Cloudflare WARP tunnel** (`warp-svc` / `warp-cli`, full TUN mode).
2. **WARP proxy mode** — the WARP daemon exposed as a local SOCKS5 proxy on `127.0.0.1:40000`.
3. **WireGuard tunnel** (`wg-quick` on the `wgcf` profile) — when the WARP daemon cannot run at all (no TUN device, kernel/`nft` incompatibility, daemon crashes). `wgcf` registers a WARP account and generates a WireGuard profile. `wg-quick` brings up a real `wgcf0` interface — the host kernel module when present, otherwise the userspace `wireguard-go` dataplane (built into the image).
4. **WireGuard SOCKS5** (`wgcf` + `wireproxy`) — if even the TUN-based tunnel fails, `wireproxy` (Go userspace WireGuard) exposes the same profile as a SOCKS5 proxy on `127.0.0.1:41000`. The account and profile persist in `/app/wireguard` across container restarts. IP rotation through this path restarts the `wireproxy` tunnel.
5. **Custom proxy pool** / direct connection — last resort.

---

## API Endpoints Reference

| Endpoint | Method | Description |
| :--- | :--- | :--- |
| `/v1/chat/completions` | `POST` | OpenAI-compatible chat completion endpoint with automatic retry and IP rotation. |
| `/v1/messages` | `POST` | Anthropic-compatible endpoint (`/v1/messages`) for Claude clients and `@ai-sdk/anthropic`. |
| `/v1/models` | `GET` | Returns list of currently discovered active free models. |
| `/dashboard` | `GET` | Renders the HTML Web Management Dashboard. |
| `/metrics` | `GET` | Returns structured JSON metrics including verified IP, uptime, and request counters. |
| `/api/rotate` | `POST` | Triggers an immediate manual IP rotation cycle. |

---

## Technical Specifications & Environment Variables

| Variable | Default Value | Description |
| :--- | :--- | :--- |
| `OPENCODE_ZEN_PORT` | `8000` | Local port for the proxy server. |
| `OPENCODE_ZEN_HOST` | `127.0.0.1` | Host address for binding the server (`0.0.0.0` in Docker). |
| `WARP_CHECK_INTERVAL` | `15` | Health check interval in seconds (set `0` to disable). |
| `WARP_ROTATION_INTERVAL` | `300` | Periodic timed IP rotation interval in seconds (set `0` to disable timed rotation). |
| `AUTO_RECYCLE_THRESHOLD` | `50` | Maximum rotations before triggering container environment refresh. |
| `CORS_ALLOW_ORIGINS` | `http://127.0.0.1:8000,http://localhost:8000` | Comma-separated browser origins allowed to call the proxy. |
| `WARP_ROTATOR_URL` | `http://127.0.0.1:8001` | Internal rotator endpoint. Do not expose port 8001 publicly. |
| `PROXY_LIST_FILE` | `/app/data/proxies.txt` | Path to the custom proxy pool file (one proxy per line). |
| `PROXY_LIST` | *(empty)* | Comma-separated custom proxies, merged with `PROXY_LIST_FILE`. |
| `CUSTOM_OUTBOUND_PROXY` | *(empty)* | Single outbound proxy (e.g. `socks5://127.0.0.1:41000`), set automatically by the fallback chain. |

### Rate-limit behavior

The proxy preserves upstream `429` responses, including `Retry-After`, and does not treat them as a signal to bypass account, model, provider, or subscription limits. The dashboard can trigger manual WARP rotation. Everything runs in a single container, so the egress path used for upstream requests is the same path verified by health checks and rotation logic.

---

## Deploy from GitHub Container Registry

Every push to `master` publishes a single all-in-one image to GHCR (no local build needed):

- `ghcr.io/sprtcrnbry/opencode-ip-rotator:latest` — unified proxy + rotator + WARP/WireGuard fallback container (built from the root `Dockerfile`)

Log in once, then run. The default `docker-compose.yml` already references the
published image, so no local build is needed:

```bash
docker login ghcr.io -u Sprtcrnbry
docker compose up -d
```

On first start the container registers a Cloudflare WARP account automatically (via `warp-cli`, or `wgcf` when running on the WireGuard fallback). `data/proxies.txt` is optional — only needed when you want a custom proxy pool.


---

## Responsibility Disclaimer

This project is intended for educational, research, and infrastructure resilience testing purposes. Users are responsible for ensuring their usage complies with applicable terms of service and acceptable use policies of third-party service providers. The maintainers assume no liability for account suspensions, service interruptions, or misuse.

---

## License

This software is released under the [MIT License](LICENSE).
