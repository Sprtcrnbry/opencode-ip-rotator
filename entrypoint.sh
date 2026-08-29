#!/bin/bash
set +e

# Fix proxies.txt mount bug: if host file missing, Docker creates directory at /app/data/proxies.txt
if [ -d /app/data/proxies.txt ]; then
  echo "Fixing proxies.txt: is directory (missing host file), removing..."
  rm -rf /app/data/proxies.txt
  touch /app/data/proxies.txt 2>/dev/null || true
fi
mkdir -p /run/dbus /var/run/dbus 2>/dev/null || true
if [ ! -e /run/dbus/pid ] && [ ! -e /var/run/dbus/pid ]; then
  echo "Starting D-Bus..."
  if command -v service >/dev/null 2>&1; then
    service dbus start 2>/dev/null || dbus-daemon --system --fork 2>/dev/null || true
  else
    dbus-daemon --system --fork 2>/dev/null || true
  fi
  sleep 1
fi

# --- helpers -----------------------------------------------------------------
start_warp_svc() {
  pkill warp-svc 2>/dev/null || true
  sleep 1
  if [ ! -c /dev/net/tun ]; then
    mkdir -p /dev/net 2>/dev/null || true
    mknod /dev/net/tun c 10 200 2>/dev/null || true
    chmod 600 /dev/net/tun 2>/dev/null || true
  fi
  echo "Starting warp-svc..."
  rm -f /tmp/warp-svc.log
  warp-svc > /tmp/warp-svc.log 2>&1 &
  WARP_SVC_PID=$!
  echo "$WARP_SVC_PID" > /tmp/warp-svc.pid
  sleep 2
}

is_warp_svc_alive() {
  if [ -n "${WARP_SVC_PID:-}" ] && kill -0 "$WARP_SVC_PID" 2>/dev/null; then return 0; fi
  if pgrep -x warp-svc >/dev/null 2>&1; then return 0; fi
  return 1
}

# --- warp-svc start ----------------------------------------------------------
start_warp_svc

# Wait for warp-svc to be ready (up to 40s, max 3 restarts then fallback)
# Must not mark ready on transient "Configuring initial firewall rules" — daemon still booting and may crash.
echo "Waiting for warp-svc to be ready..."
READY=0
RESTARTS=0
for i in $(seq 1 40); do
  if ! is_warp_svc_alive; then
    RESTARTS=$((RESTARTS+1))
    echo "warp-svc died unexpectedly after ${i}s (restart $RESTARTS/3) — last log:"
    cat /tmp/warp-svc.log 2>/dev/null | tail -n 30 || true
    if [ "$RESTARTS" -ge 3 ]; then
      echo "warp-svc crashed $RESTARTS times — host kernel/nft incompatible with WARP tunnel. Falling back to proxy/direct."
      break
    fi
    start_warp_svc
    sleep 2
    continue
  fi
  OUT=$(warp-cli --accept-tos status 2>&1 || true)
  if echo "$OUT" | grep -qi "Unable to connect to CloudflareWARP daemon"; then
    sleep 1
    continue
  fi
  if echo "$OUT" | grep -qi "Configuring initial firewall"; then
    echo "  still configuring firewall (${i}s)..."
    if grep -qiE "nft.*failed|Failed to configure firewall|permission denied|No such file or directory.*CloudflareWARP" /tmp/warp-svc.log 2>/dev/null; then
      echo "  firewall nft error detected — WARP tunnel incompatible with this host, will fallback to proxy"
      break
    fi
    sleep 2
    continue
  fi
  # Ready states: Disconnected, Connected, or explicit No registration
  if echo "$OUT" | grep -qiE "Disconnected|Connected|No registration|Registration missing"; then
    echo "warp-svc ready after ${i}s"
    echo "$OUT" | head -n 8
    READY=1
    break
  fi
  if echo "$OUT" | grep -qiE "Status update|Trace|Mode|Account"; then
    if echo "$OUT" | grep -qi "Connecting"; then
      sleep 1
      continue
    fi
    echo "warp-svc ready after ${i}s (generic status)"
    echo "$OUT" | head -n 8
    READY=1
    break
  fi
  sleep 1
done
if [ "$READY" -eq 0 ]; then
  echo "WARNING: warp-svc did not become ready (restarts=$RESTARTS). Last status:"
  warp-cli --accept-tos status 2>&1 | head -n 10 || true
  echo "--- warp-svc log tail ---"
  cat /tmp/warp-svc.log 2>/dev/null | tail -n 40 || true
  # If we hit restart limit or nft error, don't loop forever — go to proxy fallback
  if [ "$RESTARTS" -ge 3 ] || grep -qiE "nft.*failed|Failed to configure firewall|No such file or directory.*CloudflareWARP" /tmp/warp-svc.log 2>/dev/null; then
    echo "WARP tunnel incompatible with host kernel — will use proxy/direct fallback"
  elif ! is_warp_svc_alive; then
    echo "warp-svc not alive — restarting once for proxy fallback..."
    start_warp_svc
    sleep 3
  fi
fi
# If daemon died after ready check, don't restart infinitely — one restart then fallback
if ! is_warp_svc_alive; then
  if [ "$RESTARTS" -ge 3 ]; then
    echo "warp-svc still dead after $RESTARTS restarts — skipping to proxy/direct"
  else
    echo "warp-svc died right after ready check — restarting..."
    cat /tmp/warp-svc.log 2>/dev/null | tail -n 20 || true
    start_warp_svc
    sleep 3
  fi
fi
if warp-cli --accept-tos status 2>&1 | grep -qi "Connection refused"; then
  if [ "$RESTARTS" -ge 3 ]; then
    echo "warp-cli still Connection refused after $RESTARTS restarts — will try proxy/direct"
  else
    echo "warp-cli still Connection refused — restarting warp-svc..."
    cat /tmp/warp-svc.log 2>/dev/null | tail -n 20 || true
    start_warp_svc
    sleep 3
  fi
fi
# --- Registration: version-agnostic check -----------------------------------
NEEDS_REG=0
STATUS_OUT=$(warp-cli --accept-tos status 2>&1 || true)
if echo "$STATUS_OUT" | grep -qi "Connection refused"; then
  echo "warp-svc still not answering — will try proxy mode directly, skipping registration check"
  NEEDS_REG=0
elif echo "$STATUS_OUT" | grep -qiE "No registration|Registration missing|not registered|No account"; then
  NEEDS_REG=1
else
  # registration show succeeds if registered
  if ! warp-cli --accept-tos registration show >/dev/null 2>&1; then
    if echo "$STATUS_OUT" | grep -qiE "Account type.*Unknown|Device ID.*Unknown"; then
      NEEDS_REG=1
    fi
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
    # If daemon died during registration, restart
    if ! is_warp_svc_alive; then start_warp_svc; sleep 2; fi
    sleep 2
  done
else
  echo "WARP registration present, skipping creation."
fi

# --- Mode + Connect ---------------------------------------------------------
# Detect if warp tunnel is viable: check for nft/iptables hard failures in log or repeated crashes
WARP_TUNNEL_BROKEN=0
if [ "${RESTARTS:-0}" -ge 3 ]; then
  echo "warp-svc crashed $RESTARTS times — host incompatible with WARP tunnel, skipping to proxy/direct"
  WARP_TUNNEL_BROKEN=1
elif grep -qiE "Failed to configure firewall|nft.*failed|iptables.*failed|No such file or directory.*CloudflareWARP" /tmp/warp-svc.log 2>/dev/null; then
  echo "Detected firewall/nft failure in warp-svc log — skipping warp tunnel, going straight to proxy mode"
  WARP_TUNNEL_BROKEN=1
fi
# Also if /dev/net/tun not functional, skip warp
if [ ! -c /dev/net/tun ]; then
  echo "/dev/net/tun not present — skipping warp tunnel"
  WARP_TUNNEL_BROKEN=1
fi
if [ "$WARP_TUNNEL_BROKEN" -eq 0 ]; then
  echo "Setting WARP mode to warp..."
  warp-cli --accept-tos mode warp 2>&1 || warp-cli --accept-tos set-mode warp 2>&1 || true
  sleep 1

  echo "Connecting WARP..."
  CONNECTED=0
  for i in $(seq 1 5); do
    if ! is_warp_svc_alive; then
      echo "warp-svc died before connect attempt $i — restarting..."
      cat /tmp/warp-svc.log 2>/dev/null | tail -n 20 || true
      start_warp_svc
      sleep 2
    fi
    warp-cli --accept-tos connect 2>&1 || true
    sleep 3
    if warp-cli --accept-tos status 2>&1 | grep -qi "Connected"; then
      echo "WARP connected on attempt ${i}"
      CONNECTED=1
      break
    fi
    echo "Connect attempt ${i} not yet Connected, status:"
    warp-cli --accept-tos status 2>&1 | head -n 10 || true
    if warp-cli --accept-tos status 2>&1 | grep -qi "Connection refused"; then
      echo "  -> daemon Connection refused, will restart before next attempt"
      cat /tmp/warp-svc.log 2>/dev/null | tail -n 15 || true
      if ! is_warp_svc_alive; then start_warp_svc; sleep 2; fi
    fi
    sleep 2
  done
else
  CONNECTED=0
fi

# Verify tunnel is actually up; fall back to proxy mode if not
if [ "$CONNECTED" -eq 0 ]; then
  NEED_PROXY=0
  if [ "$WARP_TUNNEL_BROKEN" -eq 1 ]; then
    NEED_PROXY=1
    echo "WARP tunnel skipped due to earlier firewall/TUN check — falling back to proxy mode..."
  elif ! warp-cli --accept-tos status 2>&1 | grep -qi "Connected"; then
    NEED_PROXY=1
    echo "WARP tunnel mode failed, falling back to proxy mode..."
  fi
  if [ "$NEED_PROXY" -eq 1 ]; then
    # Ensure daemon alive for proxy mode
    if ! is_warp_svc_alive; then
      echo "warp-svc not alive for proxy mode — restarting..."
      start_warp_svc
      sleep 3
    fi
    warp-cli --accept-tos disconnect 2>&1 || true
    sleep 1
    warp-cli --accept-tos mode proxy 2>&1 || warp-cli --accept-tos set-mode proxy 2>&1 || true
    warp-cli --accept-tos proxy port 40000 2>&1 || warp-cli --accept-tos set-proxy port 40000 2>&1 || true
    sleep 1
    warp-cli --accept-tos connect 2>&1 || true
    sleep 3
    if warp-cli --accept-tos status 2>&1 | grep -qi "Connected"; then
      echo "WARP proxy mode connected on port 40000"
      export CUSTOM_OUTBOUND_PROXY="socks5://127.0.0.1:40000"
      echo "CUSTOM_OUTBOUND_PROXY=$CUSTOM_OUTBOUND_PROXY" > /tmp/warp-env 2>/dev/null || true
    else
      echo "WARNING: WARP could not connect in any mode. Using direct connection."
      echo "--- warp-svc log tail ---"
      cat /tmp/warp-svc.log 2>/dev/null | tail -n 40 || true
      echo "--- warp-cli status ---"
      warp-cli --accept-tos status 2>&1 || true
      echo "--- ip link / nft check ---"
      ip link show 2>/dev/null | head -n 20 || true
      nft list ruleset 2>&1 | head -n 20 || iptables -L 2>&1 | head -n 20 || true
    fi
  fi
else
  echo "WARP tunnel mode Connected."
fi

# If proxy mode was set, ensure env var is exported for the Python process
if [ -f /tmp/warp-env ]; then
  set -a; . /tmp/warp-env; set +a
fi
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
if [ -n "${CUSTOM_OUTBOUND_PROXY:-}" ]; then echo "Using outbound proxy: $CUSTOM_OUTBOUND_PROXY"; fi

exec python server.py
