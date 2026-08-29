# syntax=docker/dockerfile:1

# wireguard-go: userspace WireGuard dataplane that wg-quick auto-falls back to
# when the host kernel module is absent. No release binaries exist (go install only).
FROM golang:1.25-alpine AS wireguard-builder
ARG TARGETARCH
ARG WIREGUARD_GO_VERSION=v0.0.0-20260522210424-ecfc5a8d5446
RUN set -eux; \
    case "${TARGETARCH:-amd64}" in \
        arm) GOARCH=arm GOARM=7 ;; \
        arm64) GOARCH=arm64 ;; \
        amd64) GOARCH=amd64 ;; \
        *) GOARCH="${TARGETARCH}" ;; \
    esac; \
    CGO_ENABLED=0 GOARCH="$GOARCH" ${GOARM:+GOARM="$GOARM"} go install golang.zx2c4.com/wireguard@${WIREGUARD_GO_VERSION} \
    && mv /go/bin/wireguard /go/bin/wireguard-go

FROM python:3.11-slim AS app

ENV DEBIAN_FRONTEND=noninteractive \
    WARP_LOG_LEVEL=info \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Install iptables, nftables, dbus, ca-certificates, and cloudflare-warp
# Added: iproute2 (ip link for TUN debug), procps (ps/pkill), tini (init)
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    gnupg \
    lsb-release \
    iptables \
    nftables \
    iproute2 \
    procps \
    dbus \
    tini \
    ca-certificates \
    wireguard-tools \
    && curl -fsSL https://pkg.cloudflareclient.com/pubkey.gpg | gpg --yes --dearmor --output /usr/share/keyrings/cloudflare-warp-archive-keyring.gpg \
    && echo "deb [signed-by=/usr/share/keyrings/cloudflare-warp-archive-keyring.gpg] https://pkg.cloudflareclient.com/ $(lsb_release -cs) main" | tee /etc/apt/sources.list.d/cloudflare-client.list \
    && apt-get update \
    && apt-get install -y cloudflare-warp \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /run/dbus /var/run/dbus /app/data /app/wireguard

# Userspace WireGuard fallback (no TUN/kernel needed): wgcf registers a WARP
# account + generates a WireGuard profile; wireproxy exposes it as a SOCKS5 proxy.
ARG TARGETARCH
RUN set -eux; \
    case "${TARGETARCH:-amd64}" in \
        amd64) WGCF_ARCH=amd64; WP_ARCH=amd64 ;; \
        arm64) WGCF_ARCH=arm64; WP_ARCH=arm64 ;; \
        arm) WGCF_ARCH=armv7; WP_ARCH=arm ;; \
        *) echo "Unsupported arch: ${TARGETARCH:-amd64}"; exit 1 ;; \
    esac; \
    curl -fsSL "https://github.com/ViRb3/wgcf/releases/download/v2.2.32/wgcf_2.2.32_linux_${WGCF_ARCH}" -o /usr/local/bin/wgcf; \
    curl -fsSL "https://github.com/windtf/wireproxy/releases/download/v1.1.3/wireproxy_linux_${WP_ARCH}.tar.gz" -o /tmp/wireproxy.tar.gz; \
    tar -xzf /tmp/wireproxy.tar.gz -C /usr/local/bin; \
    rm -f /tmp/wireproxy.tar.gz; \
    chmod +x /usr/local/bin/wgcf /usr/local/bin/wireproxy
COPY --from=wireguard-builder /go/bin/wireguard-go /usr/local/bin/wireguard-go
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
