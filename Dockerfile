# syntax=docker/dockerfile:1
FROM python:3.11-slim AS app

ENV DEBIAN_FRONTEND=noninteractive \
    WARP_LOG_LEVEL=info \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Install iptables, dbus, ca-certificates, and cloudflare-warp
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    gnupg \
    lsb-release \
    iptables \
    dbus \
    ca-certificates \
    && curl -fsSL https://pkg.cloudflareclient.com/pubkey.gpg | gpg --yes --dearmor --output /usr/share/keyrings/cloudflare-warp-archive-keyring.gpg \
    && echo "deb [signed-by=/usr/share/keyrings/cloudflare-warp-archive-keyring.gpg] https://pkg.cloudflareclient.com/ $(lsb_release -cs) main" | tee /etc/apt/sources.list.d/cloudflare-client.list \
    && apt-get update \
    && apt-get install -y cloudflare-warp \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server.py .
COPY rate_limits.py .
COPY rotator.py .
COPY manager.py .
COPY templates/ ./templates/

RUN echo '#!/bin/bash\n\
service dbus start\n\
warp-svc &\n\
sleep 3\n\
warp-cli --accept-tos registration new || true\n\
warp-cli --accept-tos mode warp || true\n\
warp-cli --accept-tos connect || true\n\
sleep 2\n\
exec python server.py\n\
' > /app/entrypoint.sh && chmod +x /app/entrypoint.sh

EXPOSE 8000
ENTRYPOINT ["/app/entrypoint.sh"]
