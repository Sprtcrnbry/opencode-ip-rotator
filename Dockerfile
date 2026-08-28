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
set +e\n\
service dbus start\n\
warp-svc &\n\
\n\
# Wait for warp-svc to be ready (up to 30s)\n\
echo "Waiting for warp-svc to be ready..."\n\
for i in $(seq 1 30); do\n\
  if warp-cli status >/dev/null 2>&1; then\n\
    echo "warp-svc ready after ${i}s"\n\
    break\n\
  fi\n\
  sleep 1\n\
done\n\
\n\
# Ensure a registration exists (retry up to 5 times)\n\
if ! warp-cli registrations 2>/dev/null | grep -q "ID"; then\n\
  echo "No WARP registration found, creating one..."\n\
  for i in $(seq 1 5); do\n\
    if warp-cli --accept-tos registration new; then\n\
      echo "WARP registration created on attempt ${i}"\n\
      break\n\
    fi\n\
    echo "Registration attempt ${i} failed, retrying in 2s..."\n\
    sleep 2\n\
  done\n\
fi\n\
\n\
warp-cli --accept-tos mode warp || true\n\
\n\
# Connect (retry up to 5 times)\n\
for i in $(seq 1 5); do\n\
  if warp-cli --accept-tos connect; then\n\
    echo "WARP connected on attempt ${i}"\n\
    break\n\
  fi\n\
  echo "Connect attempt ${i} failed, retrying in 2s..."\n\
  sleep 2\n\
done\n\
sleep 2\n\
\n\
exec python server.py\n\
' > /app/entrypoint.sh && chmod +x /app/entrypoint.sh

EXPOSE 8000
ENTRYPOINT ["/app/entrypoint.sh"]
