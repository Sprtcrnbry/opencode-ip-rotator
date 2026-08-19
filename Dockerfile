# syntax=docker/dockerfile:1

# ---------------------------------------------------------------------------
# Shared base: both images build from python:3.11-slim so GHCR dedupes the
# base layer. The proxy shares the warp-rotator network namespace
# (docker-compose network_mode: service:warp-rotator) and never runs warp-cli,
# so it carries no Cloudflare WARP install.
# ---------------------------------------------------------------------------
FROM python:3.11-slim AS base
ENV DEBIAN_FRONTEND=noninteractive

# ---------------------------------------------------------------------------
# Proxy stage — published as :latest
# ---------------------------------------------------------------------------
FROM base AS proxy
WORKDIR /app

# `curl` is only for the docker-compose healthcheck; the proxy shares the
# warp-rotator network namespace, so it never runs warp-cli itself.
RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server.py .
COPY rate_limits.py .
COPY rotator.py .
COPY templates/ ./templates/

EXPOSE 8000
CMD ["python", "server.py"]

# ---------------------------------------------------------------------------
# WARP rotator stage — published as :warp
# ---------------------------------------------------------------------------
FROM base AS warp
ENV WARP_LOG_LEVEL=info
WORKDIR /app

# warp-svc needs dbus + iptables; rotator.py shells out to warp-cli.
# sudo/net-tools/iproute2 are not required by the entrypoint.
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    gnupg \
    lsb-release \
    iptables \
    dbus \
    && rm -rf /var/lib/apt/lists/*

# rotator.py lazily imports fastapi/uvicorn for the :8001 /health + /rotate
# listener — compose gates the proxy on that healthcheck. Keep both installed.
RUN pip3 install --no-cache-dir curl_cffi "fastapi" "uvicorn[standard]"

RUN curl -fsSL https://pkg.cloudflareclient.com/pubkey.gpg | gpg --yes --dearmor --output /usr/share/keyrings/cloudflare-warp-archive-keyring.gpg \
    && echo "deb [signed-by=/usr/share/keyrings/cloudflare-warp-archive-keyring.gpg] https://pkg.cloudflareclient.com/ $(lsb_release -cs) main" | tee /etc/apt/sources.list.d/cloudflare-client.list \
    && apt-get update \
    && apt-get install -y cloudflare-warp \
    && rm -rf /var/lib/apt/lists/*

COPY rotator.py .
COPY manager.py .

RUN echo '#!/bin/bash\n\
service dbus start\n\
warp-svc &\n\
sleep 3\n\
warp-cli --accept-tos registration new\n\
warp-cli --accept-tos mode warp\n\
warp-cli --accept-tos connect\n\
sleep 3\n\
exec python3 rotator.py\n\
' > /app/entrypoint.sh && chmod +x /app/entrypoint.sh

ENTRYPOINT ["/app/entrypoint.sh"]
