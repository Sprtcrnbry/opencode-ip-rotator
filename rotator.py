import ctypes
import json
import logging
import os
import shutil
import signal
import sqlite3
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
PERIODIC_ROTATION_INTERVAL = int(os.environ.get("WARP_ROTATION_INTERVAL", "86400"))

INITIAL_RETRY_DELAY = int(os.environ.get("WARP_RETRY_DELAY", "3"))
MAX_RETRIES = int(os.environ.get("WARP_MAX_RETRIES", "5"))
AUTO_RECYCLE_THRESHOLD = int(os.environ.get("AUTO_RECYCLE_THRESHOLD", "50"))
CUSTOM_OUTBOUND_PROXY = os.environ.get("CUSTOM_OUTBOUND_PROXY", "").strip()
WG_DIR = Path("/app/wireguard")
WG_ACCOUNT = WG_DIR / "wgcf-account.toml"
WG_PROFILE = WG_DIR / "wgcf-profile.conf"
WG_QUICK_CONF = Path("/etc/wireguard/wgcf0.conf")
WIREPROXY_CONFIG = WG_DIR / "wireproxy.conf"
WIREPROXY_PORT = 41000

# Proxy Pool Configuration
PROXY_LIST_FILE = os.environ.get("PROXY_LIST_FILE", "/app/data/proxies.txt")
PROXY_LIST_ENV = os.environ.get("PROXY_LIST", "").strip()
_proxy_pool: List[str] = []
_proxy_index = 0
_proxy_lock = threading.Lock()

def load_proxy_list() -> None:
    """Load proxy list from file and environment variable."""
    global _proxy_pool, _proxy_index
    proxies = []
    
    # Load from file
    proxy_file = Path(PROXY_LIST_FILE)
    if proxy_file.exists():
        try:
            with open(proxy_file, "r", encoding="utf-8") as f:
                lines = [line.strip() for line in f if line.strip() and not line.startswith("#")]
                proxies.extend(lines)
        except Exception as e:
            log.error(f"Error reading proxy list file {PROXY_LIST_FILE}: {e}")
    
    # Load from environment variable
    if PROXY_LIST_ENV:
        proxies.extend([p.strip() for p in PROXY_LIST_ENV.split(",") if p.strip()])
    
    # Deduplicate while preserving order
    _proxy_pool = list(dict.fromkeys(proxies))
    _proxy_index = 0
    
    if _proxy_pool:
        log.info(f"Loaded {len(_proxy_pool)} proxies into rotation pool.")
    else:
        log.info("No proxies configured. WARP rotation will be the only IP rotation method.")

def get_next_proxy() -> Optional[Dict[str, str]]:
    """Get the next proxy from the pool in round-robin fashion."""
    global _proxy_index
    with _proxy_lock:
        if not _proxy_pool:
            return None
        proxy_url = _proxy_pool[_proxy_index % len(_proxy_pool)]
        _proxy_index += 1
        return {"http": proxy_url, "https": proxy_url}

class JSONFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        log_entry = {
            "timestamp": self.formatTime(record, self.datefmt or "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info and record.exc_info[0]:
            log_entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(log_entry)

LOG_FORMAT = os.environ.get("LOG_FORMAT", "text").lower()
if LOG_FORMAT == "json":
    _handler = logging.StreamHandler()
    _handler.setFormatter(JSONFormatter())
    logging.basicConfig(level=logging.INFO, handlers=[_handler])
else:
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
FLOW_LEASE_DB_PATH = Path(os.environ.get("METRICS_DB_PATH", "/app/data/metrics.db"))

# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------
def has_active_flow_leases() -> bool:
    """Read proxy-owned stream leases from the shared metrics database."""
    if not FLOW_LEASE_DB_PATH.exists():
        return False
    try:
        conn = sqlite3.connect(str(FLOW_LEASE_DB_PATH), timeout=5)
        try:
            conn.execute("DELETE FROM active_flow_leases WHERE expires_at <= ?", (time.time(),))
            conn.commit()
            row = conn.execute("SELECT 1 FROM active_flow_leases LIMIT 1").fetchone()
            return row is not None
        finally:
            conn.close()
    except sqlite3.OperationalError as exc:
        # During startup table may not exist yet — do NOT block rotation.
        # Old code returned True here, which deadlocked rotations on fresh DB.
        if "no such table" in str(exc).lower():
            return False
        log.warning("Unable to inspect active stream leases: %s", exc)
        return False

def get_public_ip() -> Optional[str]:
    """Fetches current public IP using Cloudflare trace or multi-provider fallbacks."""
    # 1. Cloudflare trace (fastest, native to Cloudflare/WARP)
    for trace_url in ("https://cloudflare.com/cdn-cgi/trace", "https://1.1.1.1/cdn-cgi/trace"):
        try:
            from curl_cffi import requests
            resp = requests.get(trace_url, impersonate="chrome124", timeout=4)
            if resp.status_code == 200:
                for line in resp.text.splitlines():
                    if line.startswith("ip="):
                        ip = line.split("=", 1)[1].strip()
                        if ip:
                            return ip
        except Exception:
            pass

    # 2. ipify JSON fallback
    try:
        from curl_cffi import requests
        resp = requests.get("https://api.ipify.org?format=json", impersonate="chrome124", timeout=4)
        if resp.status_code == 200:
            return resp.json().get("ip")
    except Exception:
        pass

    # 3. icanhazip / ifconfig.me fallbacks
    for fallback_url in ("https://icanhazip.com", "https://ifconfig.me/ip"):
        try:
            from curl_cffi import requests
            resp = requests.get(fallback_url, impersonate="chrome124", timeout=4)
            if resp.status_code == 200 and resp.text.strip():
                return resp.text.strip()
        except Exception:
            pass

    return None


def get_public_ip_via_proxy(proxy: Dict[str, str]) -> Optional[str]:
    """Fetches current public IP using a specific proxy."""
    try:
        from curl_cffi import requests
        resp = requests.get("https://api.ipify.org?format=json", impersonate="chrome124", timeout=10, proxies=proxy)
        if resp.status_code == 200:
            return resp.json().get("ip")
    except Exception:
        try:
            from curl_cffi import requests
            resp = requests.get("https://ifconfig.me/ip", impersonate="chrome124", timeout=10, proxies=proxy)
            if resp.status_code == 200:
                return resp.text.strip()
        except Exception:
            return None
    return None


ip_history: List[Dict[str, Any]] = []

# --- WireGuard rotation helpers (must match entrypoint's WG setup) ----------------
def _wireguard_tunnel_capable() -> bool:
    return Path("/dev/net/tun").exists() and bool(shutil.which("wg-quick")) and bool(shutil.which("wireguard-go"))


def _is_wireguard_tunnel_active() -> bool:
    try:
        r = subprocess.run(["wg", "show", "interfaces"], capture_output=True, text=True, timeout=5, check=False)
        if "wgcf0" in r.stdout:
            return True
        r2 = subprocess.run(["ip", "link", "show", "dev", "wgcf0"], capture_output=True, text=True, timeout=3, check=False)
        return r2.returncode == 0
    except Exception:
        return False


def _is_wireproxy_active() -> bool:
    try:
        r = subprocess.run(["pgrep", "-f", "wireproxy"], capture_output=True, text=True, timeout=3, check=False)
        return r.returncode == 0 and bool(r.stdout.strip())
    except Exception:
        return False


def _current_wireproxy_url() -> str:
    # env may be mutated at runtime after entrypoint; read live
    live = os.environ.get("CUSTOM_OUTBOUND_PROXY", "").strip()
    if live and str(WIREPROXY_PORT) in live:
        return live
    if CUSTOM_OUTBOUND_PROXY and str(WIREPROXY_PORT) in CUSTOM_OUTBOUND_PROXY:
        return CUSTOM_OUTBOUND_PROXY
    return live or CUSTOM_OUTBOUND_PROXY


def _teardown_wireguard_backends() -> None:
    for cmd in (["pkill", "-f", "wireproxy"], ["wg-quick", "down", "wgcf0"]):
        try:
            subprocess.run(cmd, capture_output=True, timeout=8, check=False)
        except Exception:
            pass
    try:
        subprocess.run(["pkill", "-f", "wireguard-go wgcf0"], capture_output=True, timeout=5, check=False)
    except Exception:
        pass
    time.sleep(1)


def _bring_up_wireguard_tunnel() -> bool:
    try:
        WG_QUICK_CONF.parent.mkdir(parents=True, exist_ok=True)
        # strip DNS — container has no resolvconf (case-insensitive, handles "DNS=1.1.1.1")
        with open(WG_PROFILE, "r", encoding="utf-8") as src:
            lines = [l for l in src if not l.strip().lower().startswith("dns")]
        WG_QUICK_CONF.write_text("".join(lines), encoding="utf-8")
        WG_QUICK_CONF.chmod(0o600)
    except Exception as e:
        log.warning("Failed to stage %s: %s", WG_QUICK_CONF, e)
        return False
    try:
        r = subprocess.run(["wg-quick", "up", str(WG_QUICK_CONF)], capture_output=True, text=True, timeout=20, check=False)
        if r.returncode != 0:
            log.warning("wg-quick up failed: %s %s", r.stdout.strip()[:300], r.stderr.strip()[:300])
            return False
    except Exception as e:
        log.warning("wg-quick up error: %s", e)
        return False
    # verify egress via tunnel (no proxy)
    for _ in range(3):
        time.sleep(2)
        ip = get_public_ip()
        if ip:
            return True
    return _is_wireguard_tunnel_active()


def _bring_up_wireproxy() -> bool:
    try:
        WG_DIR.mkdir(parents=True, exist_ok=True)
        WIREPROXY_CONFIG.write_text(
            f"WGConfig = {WG_PROFILE}\n\n[Socks5]\nBindAddress = 127.0.0.1:{WIREPROXY_PORT}\n",
            encoding="utf-8",
        )
    except Exception as e:
        log.warning("Failed to write %s: %s", WIREPROXY_CONFIG, e)
        return False
    try:
        subprocess.run(["pkill", "-f", "wireproxy"], capture_output=True, timeout=5, check=False)
        time.sleep(1)
        subprocess.Popen(["wireproxy", "-c", str(WIREPROXY_CONFIG)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:
        log.warning("Failed to start wireproxy: %s", e)
        return False
    time.sleep(3)
    proxy = {"http": f"socks5://127.0.0.1:{WIREPROXY_PORT}", "https": f"socks5://127.0.0.1:{WIREPROXY_PORT}"}
    ip = get_public_ip_via_proxy(proxy)
    if ip:
        env_url = f"socks5://127.0.0.1:{WIREPROXY_PORT}"
        os.environ["CUSTOM_OUTBOUND_PROXY"] = env_url
        # keep module-level var in sync for any stale check
        globals()["CUSTOM_OUTBOUND_PROXY"] = env_url
        return True
    return False


def _record_wireguard_success(new_ip: str, reason: str) -> None:
    global _current_ip, rotation_count
    _current_ip = new_ip
    rotation_count += 1
    loc = get_ip_location(new_ip)
    ts = time.strftime("%H:%M:%S", time.localtime())
    ip_history.append({"ip": new_ip, "country": loc.get("country", "Unknown"), "flag": loc.get("flag", "🌐"), "timestamp": ts, "reason": reason})
    if len(ip_history) > 20:
        ip_history.pop(0)
    try:
        db_path = Path(os.environ.get("METRICS_DB_PATH", "/app/data/metrics.db"))
        if db_path.exists():
            conn = sqlite3.connect(str(db_path))
            conn.execute("INSERT INTO ip_history (ip, country, flag, timestamp, reason) VALUES (?, ?, ?, ?, ?)", (new_ip, loc.get("country", "Unknown"), loc.get("flag", "🌐"), ts, reason))
            conn.commit()
            conn.close()
    except Exception as err:
        log.error(f"Failed to write WG rotation to DB: {err}")
    if rotation_count >= AUTO_RECYCLE_THRESHOLD:
        log.warning(f"Auto-recycle threshold reached ({rotation_count}/{AUTO_RECYCLE_THRESHOLD}). Triggering container refresh...")
        trigger_container_recycle()
    log.info("WireGuard rotation successful (%s)! New IP: %s %s (%s) (Total Rotations: %s)", reason, new_ip, loc.get("flag"), loc.get("country"), rotation_count)


def _rotate_wireguard_account(old_ip: Optional[str]) -> bool:
    """WireGuard rotation with two phases:
    1) light restart with the existing wgcf profile (no new account) — cheap, keeps quota;
    2) if IP didn't change, re-register a fresh WARP account via wgcf and bring up the best backend.
    Returns True iff the public IP actually changed."""
    global _current_ip, rotation_count
    if not shutil.which("wgcf"):
        return False
    WG_DIR.mkdir(parents=True, exist_ok=True)

    # Keep backups so a failed re-register doesn't brick a working tunnel.
    bak_account: Optional[bytes] = None
    bak_profile: Optional[bytes] = None
    bak_quick: Optional[bytes] = None
    try:
        if WG_ACCOUNT.exists():
            bak_account = WG_ACCOUNT.read_bytes()
        if WG_PROFILE.exists():
            bak_profile = WG_PROFILE.read_bytes()
        if WG_QUICK_CONF.exists():
            bak_quick = WG_QUICK_CONF.read_bytes()
    except Exception:
        pass

    # Phase 1: light restart with the existing profile (no new account). WARP
    # often hands out a new egress colo on reconnect, so this succeeds without
    # burning a new device registration.
    if WG_PROFILE.exists():
        was_tunnel = _is_wireguard_tunnel_active()
        was_proxy = _is_wireproxy_active()
        _teardown_wireguard_backends()
        # Prefer the backend that was active before; otherwise prefer tunnel when capable.
        prefer_tunnel = _wireguard_tunnel_capable() and (was_tunnel or not was_proxy)
        if prefer_tunnel:
            if _bring_up_wireguard_tunnel():
                new_ip = get_public_ip()
                if new_ip and new_ip != old_ip:
                    os.environ.pop("CUSTOM_OUTBOUND_PROXY", None)
                    try:
                        globals()["CUSTOM_OUTBOUND_PROXY"] = ""
                    except Exception:
                        pass
                    _record_wireguard_success(new_ip, "WireGuard tunnel restart")
                    return True
                log.info("WireGuard light tunnel restart kept IP %s — will re-register", new_ip)
                _teardown_wireguard_backends()
        if shutil.which("wireproxy"):
            if _bring_up_wireproxy():
                proxy = {"http": f"socks5://127.0.0.1:{WIREPROXY_PORT}", "https": f"socks5://127.0.0.1:{WIREPROXY_PORT}"}
                new_ip = get_public_ip_via_proxy(proxy)
                if new_ip and new_ip != old_ip:
                    _record_wireguard_success(new_ip, "WireGuard SOCKS5 restart")
                    return True
                log.info("WireGuard light SOCKS5 restart kept IP %s — will re-register", new_ip)
                _teardown_wireguard_backends()
        # light restart didn't change IP — fall through to re-register
        log.info("WireGuard light restart did not change IP — re-registering WARP account...")

    # Phase 2: re-register a fresh WARP account
    log.info("WireGuard rotation: re-registering WARP account via wgcf...")
    _teardown_wireguard_backends()
    for p in (WG_ACCOUNT, WG_PROFILE):
        try:
            if p.exists():
                p.unlink()
        except Exception:
            pass
    try:
        if WG_QUICK_CONF.exists():
            WG_QUICK_CONF.unlink()
    except Exception:
        pass

    try:
        r1 = subprocess.run(["wgcf", "register", "--accept-tos"], cwd=str(WG_DIR), capture_output=True, text=True, timeout=30, check=False)
        if r1.returncode != 0:
            log.warning("wgcf register failed: %s %s", r1.stdout.strip()[:400], r1.stderr.strip()[:400])
            raise RuntimeError("wgcf register failed")
        r2 = subprocess.run(["wgcf", "generate", "--profile", str(WG_PROFILE)], cwd=str(WG_DIR), capture_output=True, text=True, timeout=20, check=False)
        if r2.returncode != 0:
            log.warning("wgcf generate failed: %s %s", r2.stdout.strip()[:400], r2.stderr.strip()[:400])
            raise RuntimeError("wgcf generate failed")
    except Exception as e:
        log.warning("wgcf register/generate error: %s — restoring previous profile", e)
        # restore backups so the old tunnel can be brought back up
        try:
            if bak_account is not None:
                WG_ACCOUNT.write_bytes(bak_account)
            if bak_profile is not None:
                WG_PROFILE.write_bytes(bak_profile)
            if bak_quick is not None:
                WG_QUICK_CONF.parent.mkdir(parents=True, exist_ok=True)
                WG_QUICK_CONF.write_bytes(bak_quick)
        except Exception:
            pass
        # try to restore previous backend so we don't leave the system without egress
        try:
            if bak_profile is not None:
                if _wireguard_tunnel_capable() and bak_quick is not None:
                    _bring_up_wireguard_tunnel()
                elif shutil.which("wireproxy"):
                    _bring_up_wireproxy()
        except Exception:
            pass
        return False

    # Prefer real tunnel (kernel or wireguard-go) when TUN is available; else SOCKS5.
    tried_tunnel = False
    if _wireguard_tunnel_capable():
        tried_tunnel = True
        if _bring_up_wireguard_tunnel():
            new_ip = get_public_ip()
            if new_ip and new_ip != old_ip:
                os.environ.pop("CUSTOM_OUTBOUND_PROXY", None)
                try:
                    globals()["CUSTOM_OUTBOUND_PROXY"] = ""
                except Exception:
                    pass
                _record_wireguard_success(new_ip, "WireGuard tunnel rotation")
                return True
            log.warning("WireGuard tunnel came up but IP did not change (old=%s new=%s) — falling through to SOCKS5", old_ip, new_ip)
            _teardown_wireguard_backends()
        else:
            log.warning("WireGuard tunnel bring-up failed — trying SOCKS5 fallback")

    if shutil.which("wireproxy"):
        if _bring_up_wireproxy():
            proxy = {"http": f"socks5://127.0.0.1:{WIREPROXY_PORT}", "https": f"socks5://127.0.0.1:{WIREPROXY_PORT}"}
            new_ip = get_public_ip_via_proxy(proxy)
            if new_ip and new_ip != old_ip:
                _record_wireguard_success(new_ip, "WireGuard SOCKS5 rotation")
                return True
            log.warning("WireGuard SOCKS5 brought up but IP did not change (old=%s new=%s)", old_ip, new_ip)
        else:
            log.warning("WireGuard SOCKS5 bring-up failed")
    elif tried_tunnel:
        log.warning("wireproxy not installed, and tunnel rotation did not yield a new IP")

    return False


def restart_wireguard_proxy() -> bool:
    """Back-compat shim — now re-registers the WARP account so the IP actually rotates.
    Kept for any external caller that still imports it."""
    old_ip = _current_ip or get_public_ip()
    return _rotate_wireguard_account(old_ip)

_ip_location_cache: Dict[str, Dict[str, str]] = {}

def get_ip_location(ip: str) -> Dict[str, str]:
    """Fetches country, flag emoji, and location details for a given IP with in-memory caching."""
    if not ip or ip == "Disconnected":
        return {"country": "Unknown", "countryCode": "UN", "flag": "🌐"}
    if ip in _ip_location_cache:
        return _ip_location_cache[ip]
    try:
        from curl_cffi import requests
        resp = requests.get(f"http://ip-api.com/json/{ip}", impersonate="chrome124", timeout=3)
        if resp.status_code == 200:
            data = resp.json()
            country_code = data.get("countryCode", "UN")
            # Generate flag emoji from country code
            flag = "".join(chr(127397 + ord(c)) for c in country_code) if len(country_code) == 2 else "🌐"
            loc = {
                "country": data.get("country", "Unknown"),
                "countryCode": country_code,
                "city": data.get("city", ""),
                "flag": flag
            }
            _ip_location_cache[ip] = loc
            return loc
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

def get_warp_bin() -> str:
    path = shutil.which("warp-cli")
    if path:
        return path
    candidates = [
        r"C:\Program Files\Cloudflare\Cloudflare WARP\warp-cli.exe",
        r"C:\Program Files (x86)\Cloudflare\Cloudflare WARP\warp-cli.exe",
        "/usr/bin/warp-cli",
        "/usr/local/bin/warp-cli",
        "/bin/warp-cli",
    ]
    for candidate in candidates:
        if os.path.exists(candidate):
            return candidate
    return "warp-cli"

# -----------------------------------------------------------------------------
# WARP Controller with IP Verification & Auto-Recycle Trigger
# -----------------------------------------------------------------------------
def rotate_warp(reason: str = "Triggered", force: bool = False) -> bool:
    global _current_ip, rotation_count
    with rotation_lock:
        with flow_lock:
            if not force and (active_flows_count > 0 or has_active_flow_leases()):
                log.info("IP rotation skipped — an active streaming flow lease is in progress.")
                return False

            old_ip = _current_ip or get_public_ip()
            log.info(f"Initiating guaranteed IP rotation... (Reason: {reason} | Current IP: {old_ip})")

            # Try local WARP CLI rotation first
            warp_bin = get_warp_bin()
            warp_available = shutil.which(warp_bin) or os.path.exists(warp_bin)
            # Check daemon actually answering — avoids 4× delete/new loops when warp-svc dead
            daemon_ok = False
            daemon_proxy_mode = False
            if warp_available:
                try:
                    st = subprocess.run([warp_bin, "status"], capture_output=True, text=True, timeout=8, check=False)
                    out = (st.stdout + st.stderr).lower()
                    if "unable to connect to cloudflarewarp daemon" in out:
                        log.warning("WARP daemon not answering — skipping WARP rotation, using proxy fallback.")
                        warp_available = False
                    else:
                        daemon_ok = True
                        daemon_proxy_mode = "proxy" in out
                        if st.returncode != 0 and "no registration" in out:
                            log.warning("WARP status: no registration — will recreate on next attempt.")
                except Exception as e:
                    log.warning(f"WARP status check failed: {e} — skipping WARP rotation.")
                    warp_available = False
            if warp_available and daemon_ok:
                max_attempts = 4
                for attempt in range(1, max_attempts + 1):
                    try:
                        log.info(f"WARP rotation attempt {attempt}/{max_attempts}... (proxy_mode={daemon_proxy_mode})")
                        # Light reconnect first; only cycle registration if IP doesn't change or daemon says missing
                        r1 = subprocess.run([warp_bin, "--accept-tos", "disconnect"], capture_output=True, text=True, timeout=10, check=False)
                        if r1.returncode != 0:
                            log.debug(f"disconnect stderr: {r1.stderr.strip()[:200]}")
                        time.sleep(1)

                        # In proxy mode disconnect+connect is enough; skip delete/new unless forced
                        need_new_reg = False
                        if not daemon_proxy_mode and attempt > 1:
                            need_new_reg = True
                        # Check if last connect complained about registration
                        if need_new_reg:
                            d = subprocess.run([warp_bin, "--accept-tos", "registration", "delete"], capture_output=True, text=True, timeout=10, check=False)
                            log.debug(f"registration delete: rc={d.returncode} {d.stderr.strip()[:150]}")
                            time.sleep(1)
                            n = subprocess.run([warp_bin, "--accept-tos", "registration", "new"], capture_output=True, text=True, timeout=12, check=False)
                            log.debug(f"registration new: rc={n.returncode} {n.stdout.strip()[:150]} {n.stderr.strip()[:150]}")
                            if n.returncode != 0 and "already registered" not in (n.stdout + n.stderr).lower():
                                log.warning(f"registration new failed: {n.stderr.strip()[:200]}")
                            time.sleep(1)

                        res = subprocess.run([warp_bin, "--accept-tos", "connect"], capture_output=True, text=True, timeout=12, check=False)
                        if res.returncode != 0:
                            log.warning(f"warp connect rc={res.returncode}: {res.stderr.strip()[:200]} {res.stdout.strip()[:200]}")
                            # If registration missing, force recreate next loop
                            if "registration" in (res.stdout + res.stderr).lower():
                                daemon_proxy_mode = False
                                continue

                        time.sleep(3)
                        # Refresh daemon mode flag after connect
                        try:
                            st2 = subprocess.run([warp_bin, "status"], capture_output=True, text=True, timeout=8, check=False)
                            if "connected" not in st2.stdout.lower() and "connected" not in st2.stderr.lower():
                                log.warning(f"warp status after connect not Connected: {st2.stdout.strip()[:200]}")
                                # still check IP — some versions report Connecting but IP already rotated
                        except Exception:
                            pass
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

                            log.info(f"Guaranteed WARP IP rotation successful! New Verified IP: {new_ip} {loc.get('flag')} ({loc.get('country')}) (Total Rotations: {rotation_count})")

                            if rotation_count >= AUTO_RECYCLE_THRESHOLD:
                                log.warning(f"Auto-recycle threshold reached ({rotation_count}/{AUTO_RECYCLE_THRESHOLD}). Triggering container refresh...")
                                trigger_container_recycle()

                            return True
                        else:
                            log.warning(f"Attempt {attempt}: Assigned IP ({new_ip}) was identical to old IP ({old_ip}). Retrying fresh registration...")
                            daemon_proxy_mode = False  # force registration cycle next attempt
                    except FileNotFoundError:
                        log.error(f"Cloudflare WARP CLI ('{warp_bin}') was not found. Please install Cloudflare WARP and add warp-cli to PATH.")
                        break
                    except Exception as e:
                        log.error(f"Error during WARP rotation attempt {attempt}: {e}")
                        time.sleep(1)
            else:
                if not warp_available:
                    log.warning("WARP CLI/daemon not available — skipping to proxy fallback.")
                else:
                    log.warning("WARP daemon not ready — skipping to proxy fallback.")
            # Userspace WireGuard fallback: re-register via wgcf and bring up tunnel or SOCKS5.
            # _rotate_wireguard_account already verifies the IP changed and records history.
            if _rotate_wireguard_account(old_ip):
                return True
            # Try remote rotator service as fallback
            rotator_endpoints = ["http://warp-rotator:8001/rotate", "http://127.0.0.1:8001/rotate"]
            for endpoint in rotator_endpoints:
                try:
                    req = Request(endpoint, data=b"", headers={"User-Agent": "rotator-fallback"}, method="POST")
                    with urlopen(req, timeout=35) as resp:
                        if resp.status == 200:
                            res_data = json.loads(resp.read().decode("utf-8"))
                            if res_data.get("status") == "success":
                                _current_ip = res_data.get("verified_ip", _current_ip)
                                log.info(f"Rotation via remote rotator service ({endpoint}) successful. Verified IP: {_current_ip}")
                                return True
                except Exception:
                    pass

            # Try proxy rotation as final fallback
            log.warning("WARP and remote rotator unavailable. Attempting proxy rotation...")
            proxy = get_next_proxy()
            if proxy:
                new_ip = get_public_ip_via_proxy(proxy)
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
                        "reason": f"{reason} (via proxy)"
                    })
                    if len(ip_history) > 20:
                        ip_history.pop(0)

                    log.info(f"Proxy IP rotation successful! New Verified IP: {new_ip} {loc.get('flag')} ({loc.get('country')}) (Total Rotations: {rotation_count})")
                    return True
                else:
                    log.warning("Proxy rotation failed to provide a different IP.")
            else:
                log.warning("No proxies available for rotation.")

            log.error("All IP rotation methods failed (WARP, remote rotator, proxy).")
            return False

def trigger_container_recycle():
    """Triggers self-destruction/recycle script if inside container."""
    if not shutil.which("docker") and not os.path.exists("/var/run/docker.sock"):
        log.warning("Auto-recycle skipped: docker CLI/socket not available here. Run 'python manager.py' on the host to recycle the container.")
        return
    try:
        subprocess.Popen([sys.executable, "manager.py"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
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
    if interval <= 0:
        return
    log.info(f"Health check monitor started. Endpoint: {endpoint} (Interval: {interval}s)")
    retry_count = 0
    global _current_ip

    while not stop_event.is_set():
        if not _current_ip or _current_ip == "Disconnected":
            new_ip = get_public_ip()
            if new_ip:
                _current_ip = new_ip
                log.info(f"WARP verified public IP updated: {_current_ip}")

        # Auto-reconnect WARP if it was disconnected (e.g. after server shutdown)
        try:
            warp_bin = get_warp_bin()
            if shutil.which(warp_bin) or os.path.exists(warp_bin):
                status = subprocess.run([warp_bin, "status"], capture_output=True, text=True, timeout=10, check=False)
                out = status.stdout + status.stderr
                if "Unable to connect to CloudflareWARP daemon" in out:
                    log.debug("WARP daemon not answering — health check skip reconnect.")
                elif "Disconnected" in out:
                    log.warning("WARP tunnel is disconnected — auto-reconnecting...")
                    # Version-agnostic registration check: status tells us if missing
                    if "No registration" in out or "Registration missing" in out or "not registered" in out.lower():
                        log.warning("No WARP registration found — creating one before reconnect...")
                        subprocess.run([warp_bin, "--accept-tos", "registration", "new"], capture_output=True, text=True, timeout=15, check=False)
                        time.sleep(2)
                    else:
                        # Double-check via registration show for new CLI
                        rs = subprocess.run([warp_bin, "--accept-tos", "registration", "show"], capture_output=True, text=True, timeout=8, check=False)
                        if rs.returncode != 0 and "Device ID" not in rs.stdout:
                            # Old CLI fallback: try plural, ignore failure
                            subprocess.run([warp_bin, "--accept-tos", "registration", "new"], capture_output=True, text=True, timeout=15, check=False)
                            time.sleep(2)
                    subprocess.run([warp_bin, "--accept-tos", "connect"], capture_output=True, text=True, timeout=15, check=False)
                    time.sleep(3)
                    new_ip = get_public_ip()
                    if new_ip:
                        _current_ip = new_ip
                        log.info(f"WARP reconnected. Verified IP: {new_ip}")
        except Exception as e:
            log.debug(f"WARP auto-reconnect check error: {e}")
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
        if has_active_flow_leases():
            log.info("Scheduled IP rotation deferred — an active streaming flow lease is in progress.")
            continue
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
        def http_rotate(force: bool = True):
            success = rotate_warp(reason="Remote HTTP Dashboard Trigger", force=force)
            return {"status": "success" if success else "failed", "verified_ip": _current_ip}
        
        @rotator_app.get("/status")
        def http_status():
            return {"current_ip": _current_ip, "rotations": rotation_count, "history": ip_history}

        uvicorn.run(rotator_app, host="0.0.0.0", port=8001, log_level="warning", ws="none")
    except Exception as e:
        log.error(f"Failed to start rotator HTTP listener: {e}")

def _cleanup_warp():
    # Teardown WireGuard backends first (no warp-cli needed)
    try:
        _teardown_wireguard_backends()
    except Exception:
        pass
    warp_bin = get_warp_bin()
    if not shutil.which(warp_bin) and not os.path.exists(warp_bin):
        return
    log.info("Disconnecting WARP and cleaning up...")
    try:
        subprocess.run([warp_bin, "--accept-tos", "disconnect"], capture_output=True, text=True, timeout=10, check=False)
        subprocess.run([warp_bin, "--accept-tos", "registration", "delete"], capture_output=True, text=True, timeout=10, check=False)
    except Exception as e:
        log.warning(f"Error during WARP cleanup: {e}")
    log.info("WARP cleanup complete.")

def start_rotator_background_tasks(stop_event: threading.Event) -> None:
    """Starts rotator background tasks inside the unified server process."""
    load_proxy_list()
    global _current_ip

    # Actively resolve IP on startup with retries
    for _ in range(5):
        _current_ip = get_public_ip()
        if _current_ip:
            break
        time.sleep(1)

    log.info(f"Initialized in-process WARP Rotator. Current IP: {_current_ip or 'Disconnected'}")

    if CHECK_INTERVAL > 0:
        health_thread = threading.Thread(
            target=health_check_loop,
            args=(CHECK_ENDPOINT, CHECK_INTERVAL, INITIAL_RETRY_DELAY, MAX_RETRIES, stop_event),
            daemon=True,
            name="rotator-health-check"
        )
        health_thread.start()

    if PERIODIC_ROTATION_INTERVAL > 0:
        periodic_thread = threading.Thread(
            target=periodic_rotation_loop,
            args=(PERIODIC_ROTATION_INTERVAL, stop_event),
            daemon=True,
            name="rotator-periodic"
        )
        periodic_thread.start()

def main() -> None:
    elevate()
    stop_event = threading.Event()

    def _handle_signal(signum, frame):
        log.warning(f"Received signal {signum}, shutting down rotator...")
        stop_event.set()
        _cleanup_warp()
        sys.exit(0)

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    start_rotator_background_tasks(stop_event)
    http_thread = threading.Thread(target=start_rotator_http_server, daemon=True)
    http_thread.start()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        log.info("Shutting down Rotator...")
        stop_event.set()
        _cleanup_warp()

if __name__ == "__main__":
    main()
