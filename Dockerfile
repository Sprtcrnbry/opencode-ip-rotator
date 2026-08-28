# syntax=docker/dockerfile:1
FROM python:3.11-slim AS app

ENV DEBIAN_FRONTEND=noninteractive \
    WARP_LOG_LEVEL=info \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Install iptables, nftables, dbus, ca-certificates, and cloudflare-warp
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    gnupg \
    lsb-release \
    iptables \
    nftables \
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

COPY entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

EXPOSE 8000
ENTRYPOINT ["/app/entrypoint.sh"]
