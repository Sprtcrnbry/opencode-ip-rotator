import ctypes
import json
import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

# -----------------------------------------------------------------------------
# Configuration
# -----------------------------------------------------------------------------
CHECK_ENDPOINT = os.environ.get("WARP_CHECK_ENDPOINT", "https://opencode.ai")
CHECK_INTERVAL = int(os.environ.get("WARP_CHECK_INTERVAL", "15"))
PERIODIC_ROTATION_INTERVAL = int(os.environ.get("WARP_ROTATION_INTERVAL", "300"))

INITIAL_RETRY_DELAY = int(os.environ.get("WARP_RETRY_DELAY", "3"))
MAX_RETRIES = int(os.environ.get("WARP_MAX_RETRIES", "5"))
AUTO_RECYCLE_THRESHOLD = int(os.environ.get("AUTO_RECYCLE_THRESHOLD", "50"))
CUSTOM_OUTBOUND_PROXY = os.environ.get("CUSTOM_OUTBOUND_PROXY", "").strip()

logging.basicConfig(
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    level=logging.INFO,
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("rotator")

rotation_lock = threading.Lock()
active_flows_count = 0
flow_lock = threading.Lock()
_current_ip: Optional[str] = None
rotation_count = 0

# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------
def get_public_ip() -> Optional[str]:
    """Fetches current public IP using Chrome TLS impersonation."""
    try:
        from curl_cffi import requests
        resp = requests.get("https://api.ipify.org?format=json", impersonate="chrome124", timeout=5)
        if resp.status_code == 200:
            return resp.json().get("ip")
    except Exception:
        try:
            from curl_cffi import requests
            resp = requests.get("https://ifconfig.me/ip", impersonate="chrome124", timeout=5)
            if resp.status_code == 200:
                return resp.text.strip()
        except Exception:
            return None

ip_history: List[Dict[str, Any]] = []

def get_ip_location(ip: str) -> Dict[str, str]:
    """Fetches country, flag emoji, and location details for a given IP."""
    if not ip:
        return {"country": "Unknown", "countryCode": "UN", "flag": "🌐"}
    try:
        from curl_cffi import requests
        resp = requests.get(f"http://ip-api.com/json/{ip}", impersonate="chrome124", timeout=5)
        if resp.status_code == 200:
            data = resp.json()
            country_code = data.get("countryCode", "UN")
            # Generate flag emoji from country code
            flag = "".join(chr(127397 + ord(c)) for c in country_code) if len(country_code) == 2 else "🌐"
            return {
                "country": data.get("country", "Unknown"),
                "countryCode": country_code,
                "city": data.get("city", ""),
                "flag": flag
            }
    except Exception:
        pass
    return {"country": "Unknown", "countryCode": "UN", "flag": "🌐"}

def is_admin() -> bool:
    if os.name != "nt":
        return True
    try:
        return ctypes.windll.shell32.IsUserAnAdmin() != 0
    except Exception:
        return False

def elevate() -> None:
    if os.name == "nt" and not is_admin():
        log.info("Requesting administrative privileges...")
        params = subprocess.list2cmdline(sys.argv)
        try:
            ctypes.windll.shell32.ShellExecuteW(None, "runas", sys.executable, params, None, 1)
        except Exception as e:
            log.error(f"Failed to elevate: {e}")
        sys.exit()

# -----------------------------------------------------------------------------
# WARP Controller with IP Verification & Auto-Recycle Trigger
# -----------------------------------------------------------------------------
def rotate_warp(reason: str = "Triggered") -> bool:
    global _current_ip, rotation_count
    with rotation_lock:
        with flow_lock:
            if active_flows_count > 0:
                log.info(f"IP rotation skipped — {active_flows_count} active flow(s) in progress.")
                return False

            old_ip = _current_ip or get_public_ip()
            log.info(f"Initiating guaranteed IP rotation... (Reason: {reason} | Current IP: {old_ip})")

            max_attempts = 4
            for attempt in range(1, max_attempts + 1):
                try:
                    log.info(f"Rotation attempt {attempt}/{max_attempts}...")
                    subprocess.run(["warp-cli", "--accept-tos", "disconnect"], capture_output=True, text=True, timeout=10, check=False)
                    time.sleep(1)

                    subprocess.run(["warp-cli", "--accept-tos", "registration", "delete"], capture_output=True, text=True, timeout=10, check=False)
                    time.sleep(1)
                    subprocess.run(["warp-cli", "--accept-tos", "registration", "new"], capture_output=True, text=True, timeout=10, check=False)
                    time.sleep(1)

                    res = subprocess.run(["warp-cli", "--accept-tos", "connect"], capture_output=True, text=True, timeout=10, check=False)

                    if res.returncode == 0:
                        time.sleep(3)
                        new_ip = get_public_ip()

                        if new_ip and new_ip != old_ip:
                            _current_ip = new_ip
                            rotation_count += 1
                            loc = get_ip_location(new_ip)

                            timestamp_str = time.strftime("%H:%M:%S", time.localtime())
                            ip_history.append({
                                "ip": new_ip,
                                "country": loc.get("country", "Unknown"),
                                "flag": loc.get("flag", "🌐"),
                                "timestamp": timestamp_str,
                                "reason": reason
                            })
                            if len(ip_history) > 20:
                                ip_history.pop(0)

                            try:
                                import sqlite3
                                from pathlib import Path
                                db_path = Path(os.environ.get("METRICS_DB_PATH", "/app/data/metrics.db"))
                                if db_path.exists():
                                    conn = sqlite3.connect(str(db_path))
                                    cursor = conn.cursor()
                                    cursor.execute(
                                        "INSERT INTO ip_history (ip, country, flag, timestamp, reason) VALUES (?, ?, ?, ?, ?)",
                                        (new_ip, loc.get("country", "Unknown"), loc.get("flag", "🌐"), timestamp_str, reason)
                                    )
                                    conn.commit()
                                    conn.close()
                            except Exception as err:
                                log.error(f"Failed to write IP rotation to SQLite DB: {err}")

                            log.info(f"Guaranteed IP rotation successful! New Verified IP: {new_ip} {loc.get('flag')} ({loc.get('country')}) (Total Rotations: {rotation_count})")

                            if rotation_count >= AUTO_RECYCLE_THRESHOLD:
                                log.warning(f"Auto-recycle threshold reached ({rotation_count}/{AUTO_RECYCLE_THRESHOLD}). Triggering container refresh...")
                                trigger_container_recycle()

                            return True
                        else:
                            log.warning(f"Attempt {attempt}: Assigned IP ({new_ip}) was identical to old IP ({old_ip}). Retrying fresh registration...")
                except Exception as e:
                    log.error(f"Error during rotation attempt {attempt}: {e}")
                    time.sleep(1)

            log.error(f"Failed to obtain a different IP address after {max_attempts} attempts.")
            return False

def trigger_container_recycle():
    """Triggers self-destruction/recycle script if inside container."""
    try:
        subprocess.Popen(["python3", "manager.py"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:
        log.error(f"Failed to trigger auto-recycle: {e}")

def handle_rate_limit(attempt: int, initial_delay: int, max_retries: int) -> bool:
    delay = initial_delay * (2 ** (attempt - 1))
    log.warning(f"HTTP 429 Rate Limit detected! Retry attempt {attempt}/{max_retries} — Backoff delay: {delay}s")
    time.sleep(delay)
    return rotate_warp(reason=f"HTTP 429 - Attempt {attempt}")

# -----------------------------------------------------------------------------
# Background Monitors
# -----------------------------------------------------------------------------
def health_check_loop(endpoint: str, interval: int, initial_delay: int, max_retries: int, stop_event: threading.Event) -> None:
    log.info(f"Health check monitor started. Endpoint: {endpoint} (Interval: {interval}s)")
    retry_count = 0

    while not stop_event.is_set():
        try:
            req = Request(endpoint, headers={"User-Agent": "WARP-Guard/1.0"}, method="HEAD")
            with urlopen(req, timeout=10) as resp:
                if resp.status == 200:
                    retry_count = 0
        except HTTPError as e:
            if e.code == 429:
                retry_count += 1
                if retry_count <= max_retries:
                    handle_rate_limit(retry_count, initial_delay, max_retries)
                else:
                    log.error(f"Maximum retry attempts ({max_retries}) reached. Pausing health check for 30s.")
                    time.sleep(30)
                    retry_count = 0
        except Exception as e:
            log.debug(f"Health check error: {e}")

        stop_event.wait(interval)

def periodic_rotation_loop(interval: int, stop_event: threading.Event) -> None:
    if interval <= 0:
        return

    log.info(f"Periodic IP rotator started. (Interval: {interval}s)")
    while not stop_event.is_set():
        if stop_event.wait(interval):
            break
        rotate_warp(reason="Scheduled Interval")

def start_rotator_http_server():
    """Starts a lightweight HTTP server inside warp-rotator container on port 8001 to handle remote rotate requests."""
    try:
        from fastapi import FastAPI
        import uvicorn
        
        rotator_app = FastAPI()
        
        @rotator_app.get("/health")
        def http_health():
            return {"status": "healthy", "current_ip": _current_ip, "rotations": rotation_count}

        @rotator_app.post("/rotate")
        def http_rotate():
            success = rotate_warp(reason="Remote HTTP Dashboard Trigger")
            return {"status": "success" if success else "failed", "verified_ip": _current_ip}
        
        @rotator_app.get("/status")
        def http_status():
            return {"current_ip": _current_ip, "rotations": rotation_count, "history": ip_history}

        uvicorn.run(rotator_app, host="0.0.0.0", port=8001, log_level="warning")
    except Exception as e:
        log.error(f"Failed to start rotator HTTP listener: {e}")

def main() -> None:
    elevate()
    global _current_ip
    _current_ip = get_public_ip()
    log.info(f"Starting IP Rotator Node... Initial Verified Public IP: {_current_ip}")

    stop_event = threading.Event()
    health_thread = threading.Thread(target=health_check_loop, args=(CHECK_ENDPOINT, CHECK_INTERVAL, INITIAL_RETRY_DELAY, MAX_RETRIES, stop_event), daemon=True)
    periodic_thread = threading.Thread(target=periodic_rotation_loop, args=(PERIODIC_ROTATION_INTERVAL, stop_event), daemon=True)
    http_thread = threading.Thread(target=start_rotator_http_server, daemon=True)

    health_thread.start()
    periodic_thread.start()
    http_thread.start()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        log.info("Shutting down Rotator...")
        stop_event.set()

if __name__ == "__main__":
    main()
