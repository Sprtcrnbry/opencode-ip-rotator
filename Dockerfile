# syntax=docker/dockerfile:1

# ---------------------------------------------------------------------------
# Proxy stage — published as :latest (Alpine-based, ~80MB)
# ---------------------------------------------------------------------------
FROM python:3.11-alpine AS proxy
WORKDIR /app

RUN apk add --no-cache curl ca-certificates && \
    apk add --no-cache --virtual .build-deps gcc musl-dev python3-dev libffi-dev && \
    pip install --no-cache-dir --upgrade pip

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && \
    apk del .build-deps

COPY server.py .
COPY rate_limits.py .
COPY rotator.py .
COPY templates/ ./templates/

EXPOSE 8000
CMD ["python", "server.py"]

# ---------------------------------------------------------------------------
# WARP rotator stage — published as :warp (Debian-based, pruned)
# ---------------------------------------------------------------------------
FROM python:3.11-slim AS warp
ENV WARP_LOG_LEVEL=info \
    DEBIAN_FRONTEND=noninteractive
WORKDIR /app

# Install iptables, dbus, and cloudflare-warp while purging setup tools
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    gnupg \
    lsb-release \
    iptables \
    dbus \
    && curl -fsSL https://pkg.cloudflareclient.com/pubkey.gpg | gpg --yes --dearmor --output /usr/share/keyrings/cloudflare-warp-archive-keyring.gpg \
    && echo "deb [signed-by=/usr/share/keyrings/cloudflare-warp-archive-keyring.gpg] https://pkg.cloudflareclient.com/ $(lsb_release -cs) main" | tee /etc/apt/sources.list.d/cloudflare-client.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends cloudflare-warp \
    && apt-get purge -y gnupg lsb-release \
    && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/* /var/cache/apt/*

# rotator.py imports fastapi/uvicorn/curl_cffi for the :8001 listener
RUN pip3 install --no-cache-dir curl_cffi "fastapi" "uvicorn[standard]"

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
