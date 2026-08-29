#!/bin/bash
set +e

# --- D-Bus: warp-svc requires system bus -------------------------------------
mkdir -p /run/dbus /var/run/dbus 2>/dev/null || true
if [ ! -e /run/dbus/pid ] && [ ! -e /var/run/dbus/pid ]; then
  echo "Starting D-Bus..."
  # service dbus start may not exist on slim; fallback to dbus-daemon
  if command -v service >/dev/null 2>&1; then
    service dbus start 2>/dev/null || dbus-daemon --system --fork 2>/dev/null || true
  else
    dbus-daemon --system --fork 2>/dev/null || true
  fi
  sleep 1
fi

# --- warp-svc ---------------------------------------------------------------
echo "Starting warp-svc..."
# Kill stale warp-svc if present
pkill warp-svc 2>/dev/null || true
# Ensure /dev/net/tun exists (compose mounts it, but local docker may not)
if [ ! -c /dev/net/tun ]; then
  mkdir -p /dev/net 2>/dev/null || true
  mknod /dev/net/tun c 10 200 2>/dev/null || true
  chmod 600 /dev/net/tun 2>/dev/null || true
fi
warp-svc > /tmp/warp-svc.log 2>&1 &
WARP_SVC_PID=$!
sleep 2

# Wait for warp-svc to be ready (up to 60s)
# warp-cli returns 0 even when daemon not ready, so check stderr for "Unable to connect"
echo "Waiting for warp-svc to be ready..."
READY=0
for i in $(seq 1 60); do
  OUT=$(warp-cli --accept-tos status 2>&1 || true)
  if echo "$OUT" | grep -qi "Unable to connect to CloudflareWARP daemon"; then
    sleep 1
    continue
  fi
  # Any other output means daemon is answering (even if Disconnected/No registration)
  if echo "$OUT" | grep -qiE "Status|Trace|Mode|Account|Registration|Disconnected|Connected|No registration"; then
    echo "warp-svc ready after ${i}s"
    echo "$OUT" | head -n 5
    READY=1
    break
  fi
  sleep 1
done
if [ "$READY" -eq 0 ]; then
  echo "WARNING: warp-svc did not become ready in 60s. Last status:"
  warp-cli --accept-tos status 2>&1 || true
  cat /tmp/warp-svc.log 2>/dev/null | tail -n 20 || true
fi

# --- Registration: version-agnostic check -----------------------------------
# Old CLI: `warp-cli registrations` (plural) — does not exist on 2024+ CLI
# New CLI: `warp-cli status` or `warp-cli registration show`
# We detect "no registration" from status output, then create
NEEDS_REG=0
STATUS_OUT=$(warp-cli --accept-tos status 2>&1 || true)
if echo "$STATUS_OUT" | grep -qiE "No registration|Registration missing|not registered|No account"; then
  NEEDS_REG=1
else
  # Extra check: registration show should succeed if registered
  if ! warp-cli --accept-tos registration show >/dev/null 2>&1; then
    # Try alternative plural for old CLI before deciding
    if ! warp-cli --accept-tos registrations show >/dev/null 2>&1 && ! warp-cli --accept-tos registrations >/dev/null 2>&1; then
      # Only mark needs-reg if status also looks disconnected/missing, not just command not found
      # Check if daemon says Disconnected but without account
      if echo "$STATUS_OUT" | grep -qi "Account type.*Unknown\|Device ID.*Unknown"; then
        NEEDS_REG=1
      fi
    fi
  fi
fi
# Fallback: if warp-svc just started, always ensure one registration attempt if none detected
if echo "$STATUS_OUT" | grep -qi "Disconnected" && [ "$NEEDS_REG" -eq 0 ]; then
  # Peek at registration show again strictly
  if ! warp-cli --accept-tos registration show 2>&1 | grep -qi "Device ID\|Account ID"; then
    NEEDS_REG=0
  fi
fi

if [ "$NEEDS_REG" -eq 1 ]; then
  echo "No WARP registration found, creating one..."
  for i in $(seq 1 5); do
    if warp-cli --accept-tos registration new 2>&1; then
      echo "WARP registration created on attempt ${i}"
      break
    fi
    echo "Registration attempt ${i} failed, retrying in 2s..."
    warp-cli --accept-tos status 2>&1 | head -n 5 || true
    sleep 2
  done
else
  echo "WARP registration present, skipping creation."
fi

# --- Mode + Connect ---------------------------------------------------------
# Try tunnel mode first (warp). Some hosts/kernels reject TUN -> fallback to proxy
echo "Setting WARP mode to warp..."
warp-cli --accept-tos mode warp 2>&1 || warp-cli --accept-tos set-mode warp 2>&1 || true
sleep 1

echo "Connecting WARP..."
CONNECTED=0
for i in $(seq 1 5); do
  warp-cli --accept-tos connect 2>&1 || true
  sleep 3
  if warp-cli --accept-tos status 2>&1 | grep -qi "Connected"; then
    echo "WARP connected on attempt ${i}"
    CONNECTED=1
    break
  fi
  echo "Connect attempt ${i} not yet Connected, status:"
  warp-cli --accept-tos status 2>&1 | head -n 10 || true
  sleep 2
done

# Verify tunnel is actually up; fall back to proxy mode if not
if [ "$CONNECTED" -eq 0 ]; then
  if ! warp-cli --accept-tos status 2>&1 | grep -qi "Connected"; then
    echo "WARP tunnel mode failed, falling back to proxy mode..."
    warp-cli --accept-tos disconnect 2>&1 || true
    sleep 1
    warp-cli --accept-tos mode proxy 2>&1 || warp-cli --accept-tos set-mode proxy 2>&1 || true
    # proxy port command varies: `warp-cli proxy port` vs `warp-cli set-proxy port`
    warp-cli --accept-tos proxy port 40000 2>&1 || warp-cli --accept-tos set-proxy port 40000 2>&1 || true
    sleep 1
    warp-cli --accept-tos connect 2>&1 || true
    sleep 3
    if warp-cli --accept-tos status 2>&1 | grep -qi "Connected"; then
      echo "WARP proxy mode connected on port 40000"
      export CUSTOM_OUTBOUND_PROXY="socks5://127.0.0.1:40000"
      # Also export for rotator fallback + server proxy pool
      echo "CUSTOM_OUTBOUND_PROXY=$CUSTOM_OUTBOUND_PROXY" >> /tmp/warp-env 2>/dev/null || true
    else
      echo "WARNING: WARP could not connect in any mode. Using direct connection."
      echo "--- warp-svc log tail ---"
      cat /tmp/warp-svc.log 2>/dev/null | tail -n 30 || true
      echo "--- warp-cli status ---"
      warp-cli --accept-tos status 2>&1 || true
    fi
  fi
else
  echo "WARP tunnel mode Connected."
fi

# If proxy mode was set, ensure env var is exported for the Python process
if [ -f /tmp/warp-env ]; then
  set -a; . /tmp/warp-env; set +a
fi
# Also handle case where proxy mode Connected but CUSTOM_OUTBOUND_PROXY not yet set
if warp-cli --accept-tos status 2>&1 | grep -qi "Proxy" && warp-cli --accept-tos status 2>&1 | grep -qi "Connected"; then
  if [ -z "${CUSTOM_OUTBOUND_PROXY:-}" ]; then
    export CUSTOM_OUTBOUND_PROXY="socks5://127.0.0.1:40000"
    echo "Exported CUSTOM_OUTBOUND_PROXY=$CUSTOM_OUTBOUND_PROXY (proxy mode detected)"
  fi
fi

# Show verified egress IP (try direct and via proxy)
EGRESS_IP=$(curl -s --max-time 5 https://cloudflare.com/cdn-cgi/trace 2>/dev/null | grep "^ip=" | cut -d= -f2)
if [ -z "$EGRESS_IP" ] && [ -n "${CUSTOM_OUTBOUND_PROXY:-}" ]; then
  EGRESS_IP=$(curl -s --max-time 5 --proxy "$CUSTOM_OUTBOUND_PROXY" https://cloudflare.com/cdn-cgi/trace 2>/dev/null | grep "^ip=" | cut -d= -f2)
fi
echo "Verified egress IP: ${EGRESS_IP:-unknown}"
echo "warp-cli status:"
warp-cli --accept-tos status 2>&1 | head -n 20 || true

# Pass through CUSTOM_OUTBOUND_PROXY to server.py (rotator reads it at import)
exec python server.py
