#!/bin/bash
set +e

# Single proxies.txt source: ./data:/app/data -> /app/data/proxies.txt (README).
# Docker creates a directory at the bind target when the host file is missing — remove it.
if [ -d /app/data/proxies.txt ]; then
  echo "Fixing proxies.txt: is directory (missing host file), removing..."
  rm -rf /app/data/proxies.txt
fi
# Ensure file exists for rotator/server (avoids Is a directory)
touch /app/data/proxies.txt 2>/dev/null || true
if [ -f /app/data/proxies.txt ] && [ -s /app/data/proxies.txt ]; then
  echo "Proxy pool: $(wc -l < /app/data/proxies.txt 2>/dev/null | tr -d ' ') proxies in /app/data/proxies.txt"
else
  echo "Proxy pool: none (direct fallback)"
# Detect and record the host's direct unproxied IP to guard against leaks
HOST_DIRECT_IP=$(curl -s --max-time 6 https://cloudflare.com/cdn-cgi/trace 2>/dev/null | grep "^ip=" | cut -d= -f2 || true)
if [ -n "$HOST_DIRECT_IP" ]; then
  echo "Baseline host direct IP detected: $HOST_DIRECT_IP (leak protection active)"
  export HOST_DIRECT_IP
fi

mkdir -p /run/dbus /var/run/dbus 2>/dev/null || true
if [ ! -e /run/dbus/pid ] && [ ! -e /var/run/dbus/pid ]; then
  if command -v service >/dev/null 2>&1; then
    service dbus start 2>/dev/null || dbus-daemon --system --fork 2>/dev/null || true
  else
    dbus-daemon --system --fork 2>/dev/null || true
  fi
  sleep 1
fi

# --- helpers -----------------------------------------------------------------
WARP_RESTARTS=0
start_warp_svc() {
  pkill warp-svc 2>/dev/null || true
  sleep 1
  if [ ! -c /dev/net/tun ]; then
    mkdir -p /dev/net 2>/dev/null || true
    mknod /dev/net/tun c 10 200 2>/dev/null || true
    chmod 600 /dev/net/tun 2>/dev/null || true
  fi
  echo "Starting warp-svc... (restart $((WARP_RESTARTS+1)))"
  rm -f /tmp/warp-svc.log
  warp-svc > /tmp/warp-svc.log 2>&1 &
  WARP_SVC_PID=$!
  echo "$WARP_SVC_PID" > /tmp/warp-svc.pid
  WARP_RESTARTS=$((WARP_RESTARTS+1))
  sleep 2
}

is_warp_svc_alive() {
  if [ -n "${WARP_SVC_PID:-}" ] && kill -0 "$WARP_SVC_PID" 2>/dev/null; then return 0; fi
  if pgrep -x warp-svc >/dev/null 2>&1; then return 0; fi
  return 1
}

is_warp_broken() {
  # RESTARTS is the initial-wait counter; WARP_RESTARTS is global starts
  if [ "${RESTARTS:-0}" -ge 3 ] || [ "${WARP_RESTARTS:-0}" -ge 4 ]; then return 0; fi
  if grep -qiE "Watchdog reports that daemon has disconnected|Dropping WarpService|Failed to send message to request_sender|bus has stopped|Shutting down watchdog actor" /tmp/warp-svc.log 2>/dev/null; then return 0; fi
  if grep -qiE "Failed to configure firewall|nft.*failed|iptables.*failed|No such file or directory.*CloudflareWARP" /tmp/warp-svc.log 2>/dev/null; then return 0; fi
  return 1
}

# --- wgcf/wireguard userspace WireGuard fallbacks ---------------------------------
# When warp-svc / warp-cli cannot run (missing TUN, host nft incompatibility, daemon
# crashes), fall back to a pure-userspace WARP tunnel. wgcf registers a Cloudflare
# account and generates a WireGuard profile. Two tiers on top of that profile:
#   1. wg-quick tunnel (kernel WireGuard if the host module is present, otherwise the
#      userspace wireguard-go binary) — a real wg0 interface, no proxy hop.
#   2. wireproxy (Go userspace WireGuard) — SOCKS5 proxy, no /dev/net/tun needed.
WG_DIR=/app/wireguard
WG_IFACE=wgcf0
WIREPROXY_PORT=41000

ensure_wgcf_profile() {
  mkdir -p "$WG_DIR"
  if ! command -v wgcf >/dev/null 2>&1; then
    echo "  wgcf binary missing — skipping WireGuard fallback"
    return 1
  fi
  # wgcf writes wgcf-account.toml in the current directory — run it inside WG_DIR without leaking cwd for the caller (exec python server.py must still find /app/server.py).
  if [ ! -f "$WG_DIR/wgcf-account.toml" ]; then
    echo "  Registering Cloudflare WARP account via wgcf..."
    if ! (cd "$WG_DIR" && wgcf register --accept-tos >/tmp/wgcf-register.log 2>&1); then
      echo "  wgcf register failed:"
      tail -n 5 /tmp/wgcf-register.log 2>/dev/null || true
      return 1
    fi
  fi
  if [ ! -f "$WG_DIR/wgcf-profile.conf" ]; then
    echo "  Generating WireGuard profile via wgcf..."
    if ! (cd "$WG_DIR" && wgcf generate --profile wgcf-profile.conf >/tmp/wgcf-generate.log 2>&1); then
      echo "  wgcf generate failed:"
      tail -n 5 /tmp/wgcf-generate.log 2>/dev/null || true
      return 1
    fi
  fi
  return 0
}

start_wireguard_tunnel() {
  # Full TUN tunnel via wg-quick. wg-quick uses the host kernel module when present
  # and auto-falls back to the userspace wireguard-go binary otherwise (needs TUN + NET_ADMIN).
  if [ ! -c /dev/net/tun ]; then
    echo "  no /dev/net/tun — skipping WireGuard tunnel"
    return 1
  fi
  if ! command -v wg-quick >/dev/null 2>&1 || ! command -v wireguard-go >/dev/null 2>&1; then
    echo "  wg-quick/wireguard-go missing — skipping WireGuard tunnel"
    return 1
  fi
  ensure_wgcf_profile || return 1
  # Strip the DNS line: resolvconf is not installed and container DNS stays authoritative.
  mkdir -p /etc/wireguard
  grep -vi '^[[:space:]]*DNS[[:space:]]*=' "$WG_DIR/wgcf-profile.conf" > /etc/wireguard/wgcf0.conf
  chmod 600 /etc/wireguard/wgcf0.conf
  pkill -f "wireguard-go $WG_IFACE" 2>/dev/null || true
  wg-quick down wgcf0 >/dev/null 2>&1 || true
  echo "  Bringing up WireGuard tunnel $WG_IFACE (kernel module or wireguard-go)..."
  if ! wg-quick up /etc/wireguard/wgcf0.conf >/tmp/wg-quick.log 2>&1; then
    echo "  wg-quick failed:"
    tail -n 15 /tmp/wg-quick.log 2>/dev/null || true
    wg-quick down wgcf0 >/dev/null 2>&1 || true
    pkill -f "wireguard-go $WG_IFACE" 2>/dev/null || true
    return 1
  fi
  local trace_out ip warp_status
  trace_out=$(curl -s --max-time 8 https://cloudflare.com/cdn-cgi/trace 2>/dev/null || true)
  ip=$(echo "$trace_out" | grep "^ip=" | cut -d= -f2)
  warp_status=$(echo "$trace_out" | grep "^warp=" | cut -d= -f2)
  if [ -n "$ip" ] && { [ "$warp_status" = "on" ] || [ "$warp_status" = "plus" ]; } && { [ -z "$HOST_DIRECT_IP" ] || [ "$ip" != "$HOST_DIRECT_IP" ]; }; then
    echo "  WireGuard tunnel ($WG_IFACE) egress OK — IP: $ip (warp=$warp_status)"
    return 0
  fi
  echo "  WireGuard tunnel egress verification failed (ip=$ip warp=$warp_status direct=$HOST_DIRECT_IP) — tearing down"
  wg-quick down wgcf0 >/dev/null 2>&1 || true
  pkill -f "wireguard-go $WG_IFACE" 2>/dev/null || true
  return 1
}

start_wireguard_warp() {
  # SOCKS5 fallback: no TUN, purely userspace via wireproxy.
  if ! command -v wgcf >/dev/null 2>&1 || ! command -v wireproxy >/dev/null 2>&1; then
    echo "  wgcf/wireproxy binaries missing — skipping WireGuard SOCKS5 fallback"
    return 1
  fi
  ensure_wgcf_profile || return 1
  # wireproxy.conf imports the wgcf profile and exposes SOCKS5.
  cat > "$WG_DIR/wireproxy.conf" <<EOF
WGConfig = $WG_DIR/wgcf-profile.conf

[Socks5]
BindAddress = 127.0.0.1:$WIREPROXY_PORT
EOF
  # Start wireproxy (kill stale instance first).
  pkill -f "wireproxy -c $WG_DIR/wireproxy.conf" 2>/dev/null || true
  sleep 1
  nohup wireproxy -c "$WG_DIR/wireproxy.conf" >/tmp/wireproxy.log 2>&1 &
  sleep 3
  # Verify egress through the tunnel before advertising it.
  local trace_out ip warp_status
  trace_out=$(curl -s --max-time 8 --proxy "socks5h://127.0.0.1:$WIREPROXY_PORT" https://cloudflare.com/cdn-cgi/trace 2>/dev/null || true)
  ip=$(echo "$trace_out" | grep "^ip=" | cut -d= -f2)
  warp_status=$(echo "$trace_out" | grep "^warp=" | cut -d= -f2)
  if [ -n "$ip" ] && { [ "$warp_status" = "on" ] || [ "$warp_status" = "plus" ]; } && { [ -z "$HOST_DIRECT_IP" ] || [ "$ip" != "$HOST_DIRECT_IP" ]; }; then
    echo "  WireGuard (wgcf/wireproxy) egress OK — IP: $ip (warp=$warp_status)"
    export CUSTOM_OUTBOUND_PROXY="socks5://127.0.0.1:$WIREPROXY_PORT"
    echo "CUSTOM_OUTBOUND_PROXY=$CUSTOM_OUTBOUND_PROXY" > /tmp/warp-env
    return 0
  fi
  echo "  WireGuard SOCKS5 egress verification failed (ip=$ip warp=$warp_status direct=$HOST_DIRECT_IP)"
  tail -n 20 /tmp/wireproxy.log 2>/dev/null || true
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
# Detect if warp tunnel is viable: check for nft/iptables/watchdog hard failures or repeated crashes
WARP_TUNNEL_BROKEN=0
if is_warp_broken; then
  echo "warp-svc unstable (restarts=$WARP_RESTARTS/$RESTARTS, watchdog/nft error in log) — skipping WARP tunnel, will try proxy/wireguard"
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
  for i in $(seq 1 3); do
    if is_warp_broken; then
      echo "warp-svc flagged broken during connect loop (watchdog/nft, restarts=$WARP_RESTARTS) — aborting WARP attempts"
      break
    fi
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
    if is_warp_broken; then
      echo "watchdog/nft error detected mid-connect — aborting to WireGuard"
      break
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
    if is_warp_broken; then
      echo "warp-svc flagged broken (watchdog/nft or $WARP_RESTARTS restarts) — skipping proxy mode, trying WireGuard directly..."
    else
      # Ensure daemon alive for proxy mode
      if ! is_warp_svc_alive; then
        echo "warp-svc not alive for proxy mode — restarting..."
        start_warp_svc
        sleep 3
      fi
      # Only try proxy mode if daemon is actually answering
      if warp-cli --accept-tos status 2>&1 | grep -qi "Unable to connect to CloudflareWARP daemon"; then
        echo "warp-svc not answering for proxy mode — skipping to WireGuard"
      else
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
          NEED_PROXY=0
        else
          echo "WARP proxy mode failed."
        fi
      fi
    fi
    if [ "$NEED_PROXY" -eq 1 ]; then
      echo "Trying userspace WireGuard tunnel (wg-quick / wireguard-go)..."
      if ! start_wireguard_tunnel; then
        echo "WireGuard tunnel failed. Trying userspace WireGuard SOCKS5 (wgcf/wireproxy)..."
        if ! start_wireguard_warp; then
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

# Show verified egress IP (prefer proxy if configured)
if [ -n "${CUSTOM_OUTBOUND_PROXY:-}" ]; then
  TRACE_OUT=$(curl -s --max-time 6 --proxy "$CUSTOM_OUTBOUND_PROXY" https://cloudflare.com/cdn-cgi/trace 2>/dev/null || true)
else
  TRACE_OUT=$(curl -s --max-time 6 https://cloudflare.com/cdn-cgi/trace 2>/dev/null || true)
fi
EGRESS_IP=$(echo "$TRACE_OUT" | grep "^ip=" | cut -d= -f2)
EGRESS_WARP=$(echo "$TRACE_OUT" | grep "^warp=" | cut -d= -f2)
echo "Verified egress IP: ${EGRESS_IP:-unknown} (warp=${EGRESS_WARP:-off})"
echo "warp-cli status:"
warp-cli --accept-tos status 2>&1 | head -n 20 || true
if [ -n "${CUSTOM_OUTBOUND_PROXY:-}" ]; then echo "Using outbound proxy: $CUSTOM_OUTBOUND_PROXY"; fi

exec python server.py
