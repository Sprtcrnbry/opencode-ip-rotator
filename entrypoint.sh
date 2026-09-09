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
fi

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

  # Pre-configure Cloudflare WARP in Proxy Mode so warp-svc starts as a SOCKS5 proxy
  # on 127.0.0.1:40000 and NEVER attempts to create CloudflareWARP TUN or configure
  # nftables (which panics on minimal VPS kernels lacking nft_rt).
  mkdir -p /var/lib/cloudflare-warp
  cat > /var/lib/cloudflare-warp/mdm.xml << 'EOF'
<dict>
    <key>service_mode</key>
    <string>proxy</string>
    <key>proxy_port</key>
    <integer>40000</integer>
</dict>
EOF
  chmod 644 /var/lib/cloudflare-warp/mdm.xml
  # Clean stale crash/temp files from previous runs
  rm -f /var/lib/cloudflare-warp/.tmp* /var/lib/cloudflare-warp/emergency_disconnect.json 2>/dev/null || true

  # Restore persisted client registration from ./data/warp-client if present
  mkdir -p /app/data/warp-client
  if [ -f /app/data/warp-client/reg.json ] && [ ! -f /var/lib/cloudflare-warp/reg.json ]; then
    echo "Restoring existing WARP client registration from /app/data/warp-client..."
    cp -a /app/data/warp-client/* /var/lib/cloudflare-warp/ 2>/dev/null || true
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
  return 1
}

# --- wgcf/wireguard userspace WireGuard fallbacks ---------------------------------
# When warp-svc / warp-cli cannot run, fall back to userspace WireGuard.
# Account and profile are stored in /app/data/wireguard to avoid 429 rate limits across restarts.
WG_DIR="${WG_DIR:-/app/data/wireguard}"
# Migrate existing wireguard account from ephemeral container storage to persistent storage if needed
if [ -f /app/wireguard/wgcf-account.toml ] && [ ! -f "$WG_DIR/wgcf-account.toml" ]; then
  mkdir -p "$WG_DIR"
  cp -a /app/wireguard/* "$WG_DIR/" 2>/dev/null || true
fi
WG_IFACE=wgcf0
WIREPROXY_PORT=41000

ensure_wgcf_profile() {
  mkdir -p "$WG_DIR"
  if ! command -v wgcf >/dev/null 2>&1; then
    echo "  wgcf binary missing — skipping WireGuard fallback"
    return 1
  fi
  # wgcf writes wgcf-account.toml in the current directory — run it inside WG_DIR without leaking cwd.
  if [ ! -f "$WG_DIR/wgcf-account.toml" ]; then
    echo "  Registering Cloudflare WARP account via wgcf..."
    if ! (cd "$WG_DIR" && wgcf register --accept-tos >/tmp/wgcf-register.log 2>&1); then
      echo "  wgcf register failed:"
      tail -n 5 /tmp/wgcf-register.log 2>/dev/null || true
      return 1
    fi
  else
    echo "  Existing WireGuard account found at $WG_DIR/wgcf-account.toml (skipping registration)"
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
      # Persist registration to ./data/warp-client so container restarts don't re-register
      mkdir -p /app/data/warp-client
      cp -a /var/lib/cloudflare-warp/reg.json /var/lib/cloudflare-warp/conf.json /app/data/warp-client/ 2>/dev/null || true
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
  mkdir -p /app/data/warp-client
  cp -a /var/lib/cloudflare-warp/reg.json /var/lib/cloudflare-warp/conf.json /app/data/warp-client/ 2>/dev/null || true
fi

# --- Mode + Connect ---------------------------------------------------------
# Set proxy mode on port 40000 (userspace SOCKS5 proxy, kernel-agnostic)
echo "Setting WARP mode to proxy (port 40000)..."
warp-cli --accept-tos mode proxy 2>&1 || warp-cli --accept-tos set-mode proxy 2>&1 || true
warp-cli --accept-tos proxy port 40000 2>&1 || warp-cli --accept-tos set-proxy port 40000 2>&1 || true
sleep 1

echo "Connecting WARP..."
CONNECTED=0
for i in $(seq 1 3); do
  if is_warp_broken; then
    echo "warp-svc flagged broken during connect loop (watchdog/restarts=$WARP_RESTARTS) — aborting WARP attempts"
    break
  fi
  if ! is_warp_svc_alive; then
    echo "warp-svc died before connect attempt $i — restarting..."
    cat /tmp/warp-svc.log 2>/dev/null | tail -n 20 || true
    start_warp_svc
    sleep 2
    warp-cli --accept-tos mode proxy 2>&1 || warp-cli --accept-tos set-mode proxy 2>&1 || true
    warp-cli --accept-tos proxy port 40000 2>&1 || warp-cli --accept-tos set-proxy port 40000 2>&1 || true
  fi
  warp-cli --accept-tos connect 2>&1 || true
  sleep 3

  # Check if status says connected OR if SOCKS5 proxy is responding with WARP trace
  STATUS_CHECK=$(warp-cli --accept-tos status 2>&1 || true)
  if echo "$STATUS_CHECK" | grep -qi "Connected"; then
    echo "WARP connected on attempt ${i}"
    # Verify egress via port 40000
    TRACE_CHECK=$(curl -s --max-time 6 --proxy "socks5://127.0.0.1:40000" https://cloudflare.com/cdn-cgi/trace 2>/dev/null || true)
    CHECK_IP=$(echo "$TRACE_CHECK" | grep "^ip=" | cut -d= -f2)
    CHECK_WARP=$(echo "$TRACE_CHECK" | grep "^warp=" | cut -d= -f2)
    if [ -n "$CHECK_IP" ] && { [ "$CHECK_WARP" = "on" ] || [ "$CHECK_WARP" = "plus" ]; } && { [ -z "$HOST_DIRECT_IP" ] || [ "$CHECK_IP" != "$HOST_DIRECT_IP" ]; }; then
      echo "WARP proxy mode verified on attempt ${i} — IP: $CHECK_IP (warp=$CHECK_WARP)"
      export CUSTOM_OUTBOUND_PROXY="socks5://127.0.0.1:40000"
      echo "CUSTOM_OUTBOUND_PROXY=$CUSTOM_OUTBOUND_PROXY" > /tmp/warp-env 2>/dev/null || true
      CONNECTED=1
      break
    else
      echo "WARP status reports Connected but proxy test failed (ip=$CHECK_IP warp=$CHECK_WARP direct=$HOST_DIRECT_IP)"
    fi
  fi
  echo "Connect attempt ${i} not yet Connected, status:"
  echo "$STATUS_CHECK" | head -n 10
  if echo "$STATUS_CHECK" | grep -qi "Connection refused"; then
    echo "  -> daemon Connection refused, will restart before next attempt"
    cat /tmp/warp-svc.log 2>/dev/null | tail -n 15 || true
    if ! is_warp_svc_alive; then start_warp_svc; sleep 2; fi
  fi
  sleep 2
done

# If warp-svc failed to connect, fall back to WireGuard
if [ "$CONNECTED" -eq 0 ]; then
  echo "WARP daemon proxy mode not connected — trying userspace WireGuard SOCKS5 (wireproxy)..."
  if start_wireguard_warp; then
    CONNECTED=1
  else
    echo "WireGuard SOCKS5 failed. Trying WireGuard tunnel (wg-quick)..."
    if start_wireguard_tunnel; then
      CONNECTED=1
    else
      echo "CRITICAL: WARP could not connect in any mode."
      echo "--- warp-svc log tail ---"
      cat /tmp/warp-svc.log 2>/dev/null | tail -n 40 || true
      echo "--- warp-cli status ---"
      warp-cli --accept-tos status 2>&1 || true
    fi
  fi
fi

# If proxy mode was set, ensure env var is exported for the Python process
if [ -f /tmp/warp-env ]; then
  set -a; . /tmp/warp-env; set +a
fi
if [ -z "${CUSTOM_OUTBOUND_PROXY:-}" ]; then
  if curl -s --max-time 2 --proxy "socks5://127.0.0.1:40000" https://cloudflare.com/cdn-cgi/trace 2>/dev/null | grep -qiE "warp=(on|plus)"; then
    export CUSTOM_OUTBOUND_PROXY="socks5://127.0.0.1:40000"
    echo "Exported CUSTOM_OUTBOUND_PROXY=$CUSTOM_OUTBOUND_PROXY (auto-detected listening warp proxy)"
  elif curl -s --max-time 2 --proxy "socks5://127.0.0.1:41000" https://cloudflare.com/cdn-cgi/trace 2>/dev/null | grep -qiE "warp=(on|plus)"; then
    export CUSTOM_OUTBOUND_PROXY="socks5://127.0.0.1:41000"
    echo "Exported CUSTOM_OUTBOUND_PROXY=$CUSTOM_OUTBOUND_PROXY (auto-detected listening wireproxy)"
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
if [ -n "$HOST_DIRECT_IP" ] && [ "$EGRESS_IP" = "$HOST_DIRECT_IP" ]; then
  echo "WARNING: Verified egress IP matches host direct IP $HOST_DIRECT_IP! Leak protection will block unproxied upstream requests."
fi
echo "warp-cli status:"
warp-cli --accept-tos status 2>&1 | head -n 20 || true
if [ -n "${CUSTOM_OUTBOUND_PROXY:-}" ]; then echo "Using outbound proxy: $CUSTOM_OUTBOUND_PROXY"; fi

exec python server.py
