#!/bin/bash
set +e
service dbus start
warp-svc &

# Wait for warp-svc to be ready (up to 30s)
echo "Waiting for warp-svc to be ready..."
for i in $(seq 1 30); do
  if warp-cli status >/dev/null 2>&1; then
    echo "warp-svc ready after ${i}s"
    break
  fi
  sleep 1
done

# Ensure a registration exists (retry up to 5 times)
if ! warp-cli registrations 2>/dev/null | grep -q "ID"; then
  echo "No WARP registration found, creating one..."
  for i in $(seq 1 5); do
    if warp-cli --accept-tos registration new; then
      echo "WARP registration created on attempt ${i}"
      break
    fi
    echo "Registration attempt ${i} failed, retrying in 2s..."
    sleep 2
  done
fi

warp-cli --accept-tos mode warp || true

# Connect (retry up to 5 times)
for i in $(seq 1 5); do
  if warp-cli --accept-tos connect; then
    echo "WARP connected on attempt ${i}"
    break
  fi
  echo "Connect attempt ${i} failed, retrying in 2s..."
  sleep 2
done
sleep 3

# Verify tunnel is actually up; fall back to proxy mode if not
if ! warp-cli status 2>/dev/null | grep -q "Connected"; then
  echo "WARP tunnel mode failed, falling back to proxy mode..."
  warp-cli --accept-tos disconnect || true
  warp-cli --accept-tos mode proxy || true
  warp-cli --accept-tos proxy port 40000 || true
  warp-cli --accept-tos connect || true
  sleep 3
  if warp-cli status 2>/dev/null | grep -q "Connected"; then
    echo "WARP proxy mode connected on port 40000"
    export CUSTOM_OUTBOUND_PROXY="socks5://127.0.0.1:40000"
  else
    echo "WARNING: WARP could not connect in any mode. Using direct connection."
  fi
fi

# Show verified egress IP
EGRESS_IP=$(curl -s --max-time 5 https://cloudflare.com/cdn-cgi/trace 2>/dev/null | grep "^ip=" | cut -d= -f2)
echo "Verified egress IP: ${EGRESS_IP:-unknown}"

exec python server.py
