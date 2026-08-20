import asyncio
import json
import logging
import os
import random
import signal
import sqlite3
import threading
import time
import uuid
import ipaddress
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncGenerator, Dict, List, Optional
from urllib.request import Request as UrlRequest, urlopen

import uvicorn
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import StreamingResponse, JSONResponse, HTMLResponse, Response
from fastapi.templating import Jinja2Templates
from fastapi.middleware.cors import CORSMiddleware
from prometheus_client import Counter, Gauge, Histogram, REGISTRY, generate_latest, CONTENT_TYPE_LATEST

from curl_cffi import requests as cffi_requests
from rate_limits import classify_upstream_429
from rotator import flow_lock, active_flows_count, get_public_ip, get_ip_location, rotation_count

# -----------------------------------------------------------------------------
# Constants
# -----------------------------------------------------------------------------
IP_HISTORY_LIMIT = 20
PAGE_SIZE = 5
BACKOFF_CAP = 30
POLL_ATTEMPTS = 6
WARP_ROTATION_ATTEMPTS = 4
WARP_POST_ROTATION_SLEEP = 3
DEFAULT_PROMPT_TOKENS = 50
DEFAULT_COMPLETION_TOKENS = 100
STREAM_CHUNK_SIZE = 4096
MODEL_DISCOVERY_INTERVAL = 300
DASHBOARD_REFRESH_INTERVAL = 3
STARTUP_TIME = time.time()
ENABLE_HTTP2 = os.environ.get("ENABLE_HTTP2", "false").lower() in ("true", "1", "yes")
STREAM_TIMEOUT = 600
FLOW_LEASE_TTL_SECONDS = int(os.environ.get("FLOW_LEASE_TTL_SECONDS", "90"))
FLOW_LEASE_HEARTBEAT_SECONDS = int(os.environ.get("FLOW_LEASE_HEARTBEAT_SECONDS", "15"))

# -----------------------------------------------------------------------------
# JSON Structured Logging
# -----------------------------------------------------------------------------
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

# -----------------------------------------------------------------------------
# Proxy Pool / Custom Proxy List Support
# -----------------------------------------------------------------------------
PROXY_FILE = Path(os.environ.get("PROXY_LIST_FILE", "/app/data/proxies.txt"))
_proxy_pool: List[str] = []
_proxy_index = 0
_proxy_lock = threading.Lock()

def load_proxy_list():
    global _proxy_pool
    proxies = []
    if PROXY_FILE.exists():
        try:
            with open(PROXY_FILE, "r", encoding="utf-8") as f:
                lines = [line.strip() for line in f if line.strip() and not line.startswith("#")]
                proxies.extend(lines)
        except Exception as e:
            log.error(f"Error reading proxies.txt: {e}")
    env_proxies = os.environ.get("PROXY_LIST", "").strip()
    if env_proxies:
        proxies.extend([p.strip() for p in env_proxies.split(",") if p.strip()])
    _proxy_pool = list(dict.fromkeys(proxies))
    if _proxy_pool:
        log.info(f"Loaded {len(_proxy_pool)} custom proxies into pool.")

def get_next_outbound_proxy() -> Optional[Dict[str, str]]:
    global _proxy_index
    with _proxy_lock:
        if _proxy_pool:
            proxy_url = _proxy_pool[_proxy_index % len(_proxy_pool)]
            _proxy_index += 1
            return {"http": proxy_url, "https": proxy_url}
        custom_proxy = os.environ.get("CUSTOM_OUTBOUND_PROXY", "").strip()
        if custom_proxy:
            return {"http": custom_proxy, "https": custom_proxy}

# -----------------------------------------------------------------------------
# SQLite — WAL mode + retry for concurrent safety
# -----------------------------------------------------------------------------
DB_FILE = Path(os.environ.get("METRICS_DB_PATH", "/app/data/metrics.db"))
_db_lock = threading.Lock()

def _get_conn():
    conn = sqlite3.connect(str(DB_FILE), timeout=5)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn

def _db_execute(statement: str, params=()):
    for attempt in range(3):
        try:
            with _db_lock:
                conn = _get_conn()
                try:
                    cursor = conn.cursor()
                    cursor.execute(statement, params)
                    conn.commit()
                    return cursor
                finally:
                    conn.close()
        except sqlite3.OperationalError as e:
            if "busy" in str(e).lower() and attempt < 2:
                time.sleep(0.1 * (attempt + 1))
                continue
            raise

def _db_fetchall(statement: str, params=()) -> list:
    for attempt in range(3):
        try:
            with _db_lock:
                conn = _get_conn()
                try:
                    cursor = conn.cursor()
                    cursor.execute(statement, params)
                    return cursor.fetchall()
                finally:
                    conn.close()
        except sqlite3.OperationalError as e:
            if "busy" in str(e).lower() and attempt < 2:
                time.sleep(0.1 * (attempt + 1))
                continue
            raise

def init_db():
    DB_FILE.parent.mkdir(parents=True, exist_ok=True)
    _db_execute("""
        CREATE TABLE IF NOT EXISTS model_usage (
            model_name TEXT PRIMARY KEY,
            requests INTEGER DEFAULT 0,
            prompt_tokens INTEGER DEFAULT 0,
            completion_tokens INTEGER DEFAULT 0,
            total_tokens INTEGER DEFAULT 0,
            estimated_cost_usd REAL DEFAULT 0.0,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    _db_execute("""
        CREATE TABLE IF NOT EXISTS ip_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ip TEXT,
            country TEXT,
            flag TEXT,
            timestamp TEXT,
            reason TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    _db_execute("""
        CREATE TABLE IF NOT EXISTS warp_quality (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT,
            success INTEGER,
            latency_ms REAL,
            old_ip TEXT,
            new_ip TEXT
        )
    """)
    _db_execute("""
        CREATE TABLE IF NOT EXISTS active_flow_leases (
            lease_id TEXT PRIMARY KEY,
            expires_at REAL NOT NULL
        )
    """)


def acquire_flow_lease() -> str:
    lease_id = uuid.uuid4().hex
    touch_flow_lease(lease_id)
    return lease_id


def touch_flow_lease(lease_id: str) -> None:
    _db_execute(
        "INSERT OR REPLACE INTO active_flow_leases (lease_id, expires_at) VALUES (?, ?)",
        (lease_id, time.time() + FLOW_LEASE_TTL_SECONDS),
    )


def release_flow_lease(lease_id: str) -> None:
    _db_execute("DELETE FROM active_flow_leases WHERE lease_id = ?", (lease_id,))

def log_ip_rotation_to_db(ip: str, country: str, flag: str, timestamp: str, reason: str):
    try:
        _db_execute(
            "INSERT INTO ip_history (ip, country, flag, timestamp, reason) VALUES (?, ?, ?, ?, ?)",
            (ip, country, flag, timestamp, reason)
        )
    except Exception as e:
        log.error(f"Failed to log IP rotation to DB: {e}")

def load_ip_history_from_db() -> List[Dict[str, any]]:
    if not DB_FILE.exists():
        return []
    try:
        rows = _db_fetchall(
            "SELECT ip, country, flag, timestamp, reason FROM ip_history ORDER BY id DESC LIMIT ?",
            (IP_HISTORY_LIMIT,)
        )
        history = []
        for r in reversed(rows):
            history.append({
                "ip": r[0], "country": r[1], "flag": r[2],
                "timestamp": r[3], "reason": r[4]
            })
        return history
    except Exception as e:
        log.error(f"Error loading IP history from DB: {e}")
        return []

def load_metrics_from_db() -> Dict[str, Dict[str, any]]:
    if not DB_FILE.exists():
        return {}
    try:
        rows = _db_fetchall(
            "SELECT model_name, requests, prompt_tokens, completion_tokens, total_tokens, estimated_cost_usd FROM model_usage"
        )
        stats = {}
        for r in rows:
            stats[r[0]] = {
                "requests": r[1], "prompt_tokens": r[2],
                "completion_tokens": r[3], "total_tokens": r[4],
                "estimated_cost_usd": r[5]
            }
        return stats
    except Exception as e:
        log.error(f"Error loading metrics from DB: {e}")
        return {}

# -----------------------------------------------------------------------------
# Prometheus Metrics
# -----------------------------------------------------------------------------
try:
    prom_requests_total = Counter("proxy_requests_total", "Total proxied requests", ["model", "endpoint"])
    prom_requests_success = Counter("proxy_requests_success", "Successful proxied requests", ["model"])
    prom_requests_rate_limited = Counter("proxy_requests_rate_limited", "Rate-limited requests", ["model"])
    prom_rotation_count = Counter("proxy_rotations_total", "Total WARP rotations")
    prom_active_flows = Gauge("proxy_active_flows", "Currently active streaming flows")
    prom_request_duration = Histogram("proxy_request_duration_seconds", "Request duration", ["model", "endpoint"],
                                       buckets=(0.1, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0, 60.0, 120.0))
    prom_warp_health = Gauge("proxy_warp_health", "WARP health (1=healthy, 0=unhealthy)")
except ValueError:
    prom_requests_total = getattr(REGISTRY, "_names_to_collectors", {}).get("proxy_requests_total")
    prom_requests_success = getattr(REGISTRY, "_names_to_collectors", {}).get("proxy_requests_success")
    prom_requests_rate_limited = getattr(REGISTRY, "_names_to_collectors", {}).get("proxy_requests_rate_limited")
    prom_rotation_count = getattr(REGISTRY, "_names_to_collectors", {}).get("proxy_rotations_total")
    prom_active_flows = getattr(REGISTRY, "_names_to_collectors", {}).get("proxy_active_flows")
    prom_request_duration = getattr(REGISTRY, "_names_to_collectors", {}).get("proxy_request_duration_seconds")
    prom_warp_health = getattr(REGISTRY, "_names_to_collectors", {}).get("proxy_warp_health")

# -----------------------------------------------------------------------------
# curl_cffi Session Pool
# -----------------------------------------------------------------------------
_session_pool: Dict[str, "SessionType"] = {}
_session_pool_lock = threading.Lock()
SessionType = None  # resolved at first use

def _get_session(endpoint: str):
    global SessionType
    if SessionType is None:
        from curl_cffi.requests import Session as SessionType
    with _session_pool_lock:
        if endpoint not in _session_pool:
            kwargs = {}
            if not ENABLE_HTTP2:
                from curl_cffi import CurlHttpVersion
                kwargs["http_version"] = CurlHttpVersion.V1_1
            _session_pool[endpoint] = SessionType(**kwargs)
        return _session_pool[endpoint]

def create_fresh_session(is_stream: bool):
    global SessionType
    if SessionType is None:
        from curl_cffi.requests import Session as SessionType
    kwargs = {}
    if not ENABLE_HTTP2:
        from curl_cffi import CurlHttpVersion
        kwargs["http_version"] = CurlHttpVersion.V1_1
    return SessionType(**kwargs)

def _close_all_sessions():
    with _session_pool_lock:
        for ep, sess in _session_pool.items():
            try:
                sess.close()
            except Exception:
                pass
        _session_pool.clear()

_discovery_stop = threading.Event()

# -----------------------------------------------------------------------------
# Request Queue (drains during rotation)
# -----------------------------------------------------------------------------
_rotation_in_progress = threading.Event()
_request_drain_event = asyncio.Event()
_request_drain_event.set()

async def wait_for_rotation_drain():
    if _rotation_in_progress.is_set():
        await asyncio.wait_for(_request_drain_event.wait(), timeout=15)

def signal_rotation_start():
    _rotation_in_progress.set()
    _request_drain_event.clear()

def signal_rotation_done():
    _rotation_in_progress.clear()
    _request_drain_event.set()

# -----------------------------------------------------------------------------
# Dual-WARP (active/passive tracking)
# -----------------------------------------------------------------------------
_dual_warp = {
    "active_ip": None,
    "standby_ip": None,
    "active_registration": "primary",
}
_dual_warp_lock = threading.Lock()

def swap_warp_registration():
    with _dual_warp_lock:
        _dual_warp["active_registration"] = (
            "standby" if _dual_warp["active_registration"] == "primary" else "primary"
        )
        return _dual_warp["active_registration"]

# -----------------------------------------------------------------------------
# Model pricing reference (USD per 1M tokens)
# -----------------------------------------------------------------------------
MODEL_PRICING = {
    "big-pickle": {"input_per_1m": 0.00, "output_per_1m": 0.00},
    "deepseek-v4-flash-free": {"input_per_1m": 0.15, "output_per_1m": 0.60},
    "mimo-v2.5-free": {"input_per_1m": 0.20, "output_per_1m": 0.80},
    "qwen3.6-plus-free": {"input_per_1m": 0.40, "output_per_1m": 1.20},
    "minimax-m3-free": {"input_per_1m": 0.30, "output_per_1m": 1.00},
    "nemotron-3-ultra-free": {"input_per_1m": 0.25, "output_per_1m": 0.90},
    "ling-3.0-flash-free": {"input_per_1m": 0.15, "output_per_1m": 0.50},
    "laguna-s-2.1-free": {"input_per_1m": 0.20, "output_per_1m": 0.70},
}

_model_usage_lock = threading.Lock()

def track_token_usage(model_name: str, prompt_tokens: int = 0, completion_tokens: int = 0):
    global model_usage_stats
    pricing = MODEL_PRICING.get(model_name, {"input_per_1m": 0.20, "output_per_1m": 0.80})
    prompt_cost = (prompt_tokens / 1_000_000) * pricing["input_per_1m"]
    completion_cost = (completion_tokens / 1_000_000) * pricing["output_per_1m"]
    cost = prompt_cost + completion_cost

    with _model_usage_lock:
        if model_name not in model_usage_stats:
            model_usage_stats[model_name] = {
                "requests": 0, "prompt_tokens": 0, "completion_tokens": 0,
                "total_tokens": 0, "estimated_cost_usd": 0.0
            }
        model_usage_stats[model_name]["requests"] += 1
        model_usage_stats[model_name]["prompt_tokens"] += prompt_tokens
        model_usage_stats[model_name]["completion_tokens"] += completion_tokens
        model_usage_stats[model_name]["total_tokens"] += (prompt_tokens + completion_tokens)
        model_usage_stats[model_name]["estimated_cost_usd"] += cost

    try:
        _db_execute("""
            INSERT INTO model_usage (model_name, requests, prompt_tokens, completion_tokens, total_tokens, estimated_cost_usd)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(model_name) DO UPDATE SET
                requests = requests + excluded.requests,
                prompt_tokens = prompt_tokens + excluded.prompt_tokens,
                completion_tokens = completion_tokens + excluded.completion_tokens,
                total_tokens = total_tokens + excluded.total_tokens,
                estimated_cost_usd = estimated_cost_usd + excluded.estimated_cost_usd,
                updated_at = CURRENT_TIMESTAMP
        """, (model_name, 1, prompt_tokens, completion_tokens, prompt_tokens + completion_tokens, cost))
    except Exception as e:
        log.error(f"Failed to persist metrics to SQLite: {e}")

# -----------------------------------------------------------------------------
# WARP Quality Metrics
# -----------------------------------------------------------------------------
warp_quality_stats = {
    "total_attempts": 0, "successful_rotations": 0, "failed_rotations": 0,
    "last_latency_ms": 0.0, "avg_latency_ms": 0.0
}
_warp_quality_lock = threading.Lock()

def record_warp_rotation(success: bool, latency_ms: float = 0, old_ip: str = "", new_ip: str = ""):
    with _warp_quality_lock:
        warp_quality_stats["total_attempts"] += 1
        if success:
            warp_quality_stats["successful_rotations"] += 1
            warp_quality_stats["last_latency_ms"] = latency_ms
            n = warp_quality_stats["successful_rotations"]
            warp_quality_stats["avg_latency_ms"] = (
                (warp_quality_stats["avg_latency_ms"] * (n - 1) + latency_ms) / n
            )
        else:
            warp_quality_stats["failed_rotations"] += 1
    try:
        _db_execute(
            "INSERT INTO warp_quality (timestamp, success, latency_ms, old_ip, new_ip) VALUES (?, ?, ?, ?, ?)",
            (time.strftime("%Y-%m-%d %H:%M:%S"), 1 if success else 0, latency_ms, old_ip, new_ip)
        )
    except Exception:
        pass

# -----------------------------------------------------------------------------
# Backoff helper
# -----------------------------------------------------------------------------
def compute_backoff_delay(attempt: int, base: float = 1.0, cap: int = BACKOFF_CAP) -> float:
    return min(base * (2 ** (attempt - 1)), cap) + random.uniform(0.5, 1.5)

# -----------------------------------------------------------------------------
# Jinja2 Templates
# -----------------------------------------------------------------------------
_templates = Jinja2Templates(directory=Path(__file__).parent / "templates")

# -----------------------------------------------------------------------------
model_usage_stats: Dict[str, Dict[str, float]] = {}

# Configuration & Dynamic Discovery
# -----------------------------------------------------------------------------
PORT = int(os.environ.get("OPENCODE_ZEN_PORT", "8000"))
HOST = os.environ.get("OPENCODE_ZEN_HOST", "127.0.0.1")
TARGET_ZEN_BASE = os.environ.get("OPENCODE_ZEN_TARGET_BASE", "https://opencode.ai/zen/v1")
TARGET_ZEN_URL = f"{TARGET_ZEN_BASE}/chat/completions"
TARGET_ZEN_ANTHROPIC_URL = f"{TARGET_ZEN_BASE}/messages"
TARGET_ZEN_RESPONSES_URL = f"{TARGET_ZEN_BASE}/responses"

MAX_RETRIES_ON_429 = int(os.environ.get("MAX_RETRIES_ON_429", "8"))
INITIAL_BACKOFF = float(os.environ.get("INITIAL_BACKOFF", "1"))
WARP_ROTATOR_URL = os.environ.get("WARP_ROTATOR_URL", "http://127.0.0.1:8001").rstrip("/")
CORS_ALLOW_ORIGINS = [
    origin.strip()
    for origin in os.environ.get("CORS_ALLOW_ORIGINS", "http://127.0.0.1:8000,http://localhost:8000").split(",")
    if origin.strip()
]

metrics = {
    "total_requests": 0,
    "successful_requests": 0,
    "rate_limited_requests": 0,
    "fallback_triggered": 0,
    "discovered_models_count": 0,
    "start_time": time.time()
}

DEFAULT_FREE_MODELS = [
    {"id": "big-pickle", "name": "Big Pickle", "object": "model", "created": 1787139400, "owned_by": "opencode"},
    {"id": "deepseek-v4-flash-free", "name": "DeepSeek V4 Flash Free", "object": "model", "created": 1787139400, "owned_by": "opencode"},
    {"id": "mimo-v2.5-free", "name": "MiMo V2.5 Free", "object": "model", "created": 1787139400, "owned_by": "opencode"},
    {"id": "hy3-free", "name": "Hy3 Free", "object": "model", "created": 1787139400, "owned_by": "opencode"},
    {"id": "nemotron-3-ultra-free", "name": "Nemotron 3 Ultra Free", "object": "model", "created": 1787139400, "owned_by": "opencode"},
    {"id": "nemotron-3.5-lightning-free", "name": "Nemotron 3.5 Lightning Free", "object": "model", "created": 1787139400, "owned_by": "opencode"},
    {"id": "laguna-s-2.1-free", "name": "Laguna S 2.1 Free", "object": "model", "created": 1787139400, "owned_by": "opencode"},
]

discovered_models: List[Dict[str, Any]] = DEFAULT_FREE_MODELS.copy()
_discovery_lock = threading.Lock()
_cached_rotator_status = {"time": 0.0, "data": None}
_cached_verified_ip: Optional[str] = None

LOG_FORMAT = os.environ.get("LOG_FORMAT", "text").lower()
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
LOG_LEVEL_VALUE = getattr(logging, LOG_LEVEL, logging.INFO)

if LOG_FORMAT == "json":
    _handler = logging.StreamHandler()
    _handler.setFormatter(JSONFormatter())
    logging.basicConfig(level=LOG_LEVEL_VALUE, handlers=[_handler], force=True)
else:
    logging.basicConfig(
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        level=LOG_LEVEL_VALUE,
        datefmt="%Y-%m-%d %H:%M:%S",
        force=True,
    )
log = logging.getLogger("zen_server")

# Dynamic models.dev and Router metadata cache
_models_dev_cache: Dict[str, Dict[str, Any]] = {}
_models_dev_last_fetch: float = 0.0
_models_dev_lock = threading.Lock()
MODELS_DEV_CACHE_TTL = 3600  # 1 hour

ROUTER_MODELS_URL = os.environ.get("ROUTER_MODELS_URL", "").strip()
ROUTER_API_KEY = os.environ.get("ROUTER_API_KEY", "").strip()

def fetch_models_dev_specs() -> Dict[str, Dict[str, Any]]:
    """Fetches live provider-agnostic and provider-specific model metadata dynamically from models.dev."""
    global _models_dev_cache, _models_dev_last_fetch
    now = time.time()
    with _models_dev_lock:
        if _models_dev_cache and (now - _models_dev_last_fetch < MODELS_DEV_CACHE_TTL):
            return _models_dev_cache

    specs: Dict[str, Dict[str, Any]] = {}

    # 1. Fetch provider models from https://models.dev/api.json
    try:
        req = UrlRequest("https://models.dev/api.json", headers={"User-Agent": "Mozilla/5.0"})
        with urlopen(req, timeout=10) as resp:
            api_data = json.loads(resp.read().decode("utf-8"))
            for provider_id, provider in api_data.items():
                models = provider.get("models", {})
                for m_id, m_info in models.items():
                    norm_id = m_id.split("/")[-1].lower()
                    limits = m_info.get("limit") or {}
                    ctx = limits.get("context")
                    out = limits.get("output")
                    entry = {
                        "name": m_info.get("name"),
                        "description": m_info.get("description"),
                        "context": ctx,
                        "max_output": out,
                        "reasoning": bool(m_info.get("reasoning")),
                        "tool_call": bool(m_info.get("tool_call")),
                    }
                    if norm_id not in specs or provider_id == "opencode":
                        specs[norm_id] = entry
                    if m_id.lower() not in specs or provider_id == "opencode":
                        specs[m_id.lower()] = entry
    except Exception as e:
        log.debug("Error fetching models.dev/api.json: %s", e)

    # 2. Fetch global models catalog from https://models.dev/models.json as fallback
    try:
        req = UrlRequest("https://models.dev/models.json", headers={"User-Agent": "Mozilla/5.0"})
        with urlopen(req, timeout=10) as resp:
            models_data = json.loads(resp.read().decode("utf-8"))
            for m_id, m_info in models_data.items():
                norm_id = m_id.split("/")[-1].lower()
                limits = m_info.get("limit") or {}
                ctx = limits.get("context")
                out = limits.get("output")
                if norm_id not in specs:
                    specs[norm_id] = {
                        "name": m_info.get("name"),
                        "description": m_info.get("description"),
                        "context": ctx,
                        "max_output": out,
                        "reasoning": bool(m_info.get("reasoning")),
                        "tool_call": bool(m_info.get("tool_call")),
                    }
    except Exception as e:
        log.debug("Error fetching models.dev/models.json: %s", e)

    with _models_dev_lock:
        if specs:
            _models_dev_cache = specs
            _models_dev_last_fetch = now
        return _models_dev_cache or specs

def fetch_router_model_specs() -> Dict[str, Dict[str, Any]]:
    """Optionally queries a local router (e.g. 9router) for live capability metadata."""
    if not ROUTER_MODELS_URL:
        return {}
    try:
        headers = {"User-Agent": "Mozilla/5.0"}
        if ROUTER_API_KEY:
            headers["Authorization"] = f"Bearer {ROUTER_API_KEY}"
        req = UrlRequest(ROUTER_MODELS_URL, headers=headers)
        with urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            models = data.get("data", [])
            specs = {}
            for m in models:
                m_id = m.get("id", "")
                norm_id = m_id.split("/")[-1].lower()
                caps = m.get("capabilities") or {}
                ctx = m.get("context_length") or caps.get("contextWindow")
                out = m.get("max_completion_tokens") or caps.get("maxOutput")
                if ctx or out:
                    specs[norm_id] = {
                        "context": int(ctx) if ctx else None,
                        "max_output": int(out) if out else None,
                        "reasoning": bool(caps.get("reasoning")),
                        "tool_call": bool(caps.get("tools")),
                    }
            return specs
    except Exception as e:
        log.debug("Could not fetch models metadata from ROUTER_MODELS_URL: %s", e)
        return {}

def format_tokens_label(tokens: Optional[int], suffix: str, default: str) -> str:
    """Formats numeric token values to human-friendly labels (e.g. 1M Context, 64k Max Output)."""
    if not tokens or tokens <= 0:
        return default
    if tokens >= 1000000:
        val = round(tokens / 1000000, 1) if (tokens % 1000000 != 0 and tokens < 10000000) else int(tokens / 1000000)
        return f"{val}M {suffix}"
    if tokens >= 1000:
        return f"{int(tokens / 1000)}k {suffix}"
    return f"{tokens} {suffix}"

def resolve_model_specs(model_id: str, upstream_meta: dict, dynamic_specs: Optional[dict] = None) -> dict:
    """Extracts or infers metadata, context window (input), and max output tokens (generation)."""
    caps = upstream_meta.get("capabilities") or {}
    ctx_val = (
        upstream_meta.get("context_length")
        or upstream_meta.get("max_context_length")
        or upstream_meta.get("context_window")
        or upstream_meta.get("input_token_limit")
        or caps.get("contextWindow")
    )
    out_val = (
        upstream_meta.get("max_output_tokens")
        or upstream_meta.get("max_completion_tokens")
        or upstream_meta.get("max_tokens")
        or upstream_meta.get("output_token_limit")
        or caps.get("maxOutput")
    )
    name_val = upstream_meta.get("name")
    desc_val = upstream_meta.get("description", "")
    reasoning_val = bool(caps.get("reasoning"))
    tool_val = bool(caps.get("tools"))

    m_id_lower = model_id.lower()
    norm_id = m_id_lower.split("/")[-1]

    # Check dynamic specs from models.dev or 9router
    if dynamic_specs:
        match_spec = dynamic_specs.get(m_id_lower) or dynamic_specs.get(norm_id)
        if not match_spec:
            # Check prefix / suffix match
            for k, v in dynamic_specs.items():
                if k == norm_id or norm_id.endswith(k) or k.endswith(norm_id):
                    match_spec = v
                    break
        if match_spec:
            if not ctx_val and match_spec.get("context"):
                ctx_val = match_spec["context"]
            if not out_val and match_spec.get("max_output"):
                out_val = match_spec["max_output"]
            if not name_val and match_spec.get("name"):
                name_val = match_spec["name"]
            if not desc_val and match_spec.get("description"):
                desc_val = match_spec["description"]
            if "reasoning" in match_spec:
                reasoning_val = match_spec["reasoning"]
            if "tool_call" in match_spec:
                tool_val = match_spec["tool_call"]

    ctx_num = int(ctx_val) if (ctx_val and isinstance(ctx_val, (int, float))) else 128000
    out_num = int(out_val) if (out_val and isinstance(out_val, (int, float))) else 8192

    return {
        "name": name_val or model_id.replace("-", " ").title(),
        "description": desc_val,
        "context_tokens": ctx_num,
        "context_label": format_tokens_label(ctx_num, "Context", "128k Context"),
        "max_output_tokens": out_num,
        "output_label": format_tokens_label(out_num, "Max Output", "8k Max Output"),
        "reasoning": reasoning_val,
        "tool_call": tool_val,
    }

def fetch_models_from_server() -> Optional[List[Dict[str, Any]]]:
    """Fetches model list directly from the upstream server and enriches dynamically from models.dev / router."""
    try:
        models_dev_specs = fetch_models_dev_specs()
        router_specs = fetch_router_model_specs()
        dynamic_specs = {**models_dev_specs, **router_specs}

        req = UrlRequest(
            f"{TARGET_ZEN_BASE}/models",
            headers={"Authorization": "Bearer public", "User-Agent": "Mozilla/5.0"}
        )
        with urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            models_data = data.get("data", [])
            if models_data:
                new_models = []
                for m in models_data:
                    m_id = m.get("id", "")
                    m_id_lower = m_id.lower()
                    if "free" in m_id_lower or "big-pickle" in m_id_lower:
                        model_entry = dict(m)
                        specs = resolve_model_specs(m_id, m, dynamic_specs)
                        model_entry["name"] = specs["name"]
                        model_entry["description"] = specs["description"]
                        model_entry["context_tokens"] = specs["context_tokens"]
                        model_entry["context_label"] = specs["context_label"]
                        model_entry["max_output_tokens"] = specs["max_output_tokens"]
                        model_entry["output_label"] = specs["output_label"]
                        model_entry["reasoning"] = specs["reasoning"]
                        model_entry["tool_call"] = specs["tool_call"]
                        new_models.append(model_entry)
                return new_models if new_models else None
    except Exception as e:
        log.debug("Auto-Discovery error fetching models from server: %s", e)
        return None

def discover_models_task():
    global discovered_models
    while not _discovery_stop.is_set():
        new_models = fetch_models_from_server()
        if new_models:
            with _discovery_lock:
                discovered_models = new_models
                metrics["discovered_models_count"] = len(discovered_models)
            log.info(f"Auto-Discovery refreshed: {len(discovered_models)} active model(s) fetched from server.")
        _discovery_stop.wait(MODEL_DISCOVERY_INTERVAL)

@asynccontextmanager
async def lifespan(application: FastAPI):
    global model_usage_stats, discovered_models
    init_db()
    model_usage_stats = load_metrics_from_db()
    _discovery_stop.clear()

    # Initialize in-process WARP rotator background monitor
    try:
        import rotator
        rotator.start_rotator_background_tasks(_discovery_stop)
    except Exception as e:
        log.warning(f"Could not initialize in-process rotator: {e}")

    initial_models = await asyncio.to_thread(fetch_models_from_server)
    if initial_models:
        with _discovery_lock:
            discovered_models = initial_models
            metrics["discovered_models_count"] = len(discovered_models)
        log.info(f"Initial server model sync complete: {len(discovered_models)} active model(s) loaded.")
    threading.Thread(target=discover_models_task, daemon=True).start()
    yield
    _close_all_sessions()
    _discovery_stop.set()

app = FastAPI(title="OpenCode Zen v3.0 Ultra Resilient Proxy", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ALLOW_ORIGINS,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

class FlowContext:
    def __enter__(self):
        global active_flows_count
        with flow_lock:
            active_flows_count += 1
            prom_active_flows.set(active_flows_count)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        global active_flows_count
        with flow_lock:
            active_flows_count = max(0, active_flows_count - 1)
            prom_active_flows.set(active_flows_count)


# Opencode CLI fingerprint (exact User-Agent from official OpenCode client).
OPENCODE_UA = "opencode/1.18.18 ai-sdk/provider-utils/4.0.23 runtime/bun/1.3.14"


def _is_loopback_ip(value: str) -> bool:
    try:
        return ipaddress.ip_address(value.strip()).is_loopback
    except ValueError:
        return False


def build_opencode_headers(raw_request: Request) -> Dict[str, str]:
    """Builds upstream headers matching official OpenCode client fingerprint:
    User-Agent: opencode/1.18.18 ai-sdk/provider-utils/4.0.23 runtime/bun/1.3.14 (or authentic client pass-through)
    x-opencode-session: fresh ses_<hex> (or client pass-through)
    x-opencode-request: fresh req_<hex> (or client pass-through)
    x-opencode-project: /opencode (or client pass-through)
    x-opencode-client: desktop (or client pass-through)
    Authorization: Bearer public (or client valid key)"""
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream, */*",
    }

    # Pass through valid API keys or map dummy/placeholder keys to 'Bearer public'
    client_auth = raw_request.headers.get("authorization", "")
    client_api_key = raw_request.headers.get("x-api-key", "")
    token = ""
    if client_auth.startswith("Bearer "):
        token = client_auth[7:].strip()
    elif client_auth:
        token = client_auth.strip()
    elif client_api_key:
        token = client_api_key.strip()

    if not token or token.lower() in ("any", "none", "null", "test", "dummy", "public", "undefined", "false", "true", "local"):
        headers["Authorization"] = "Bearer public"
    else:
        headers["Authorization"] = f"Bearer {token}"

    # Detect if client is an authentic OpenCode agent / CLI
    client_ua = raw_request.headers.get("user-agent", "")
    if client_ua and "opencode" in client_ua.lower():
        headers["User-Agent"] = client_ua
    else:
        headers["User-Agent"] = OPENCODE_UA

    # Session & Identity headers
    session_id = (
        raw_request.headers.get("x-opencode-session")
        or raw_request.headers.get("x-session-id")
        or raw_request.headers.get("x-session-affinity")
        or f"ses_{uuid.uuid4().hex}"
    )
    headers["x-opencode-session"] = session_id
    headers["x-opencode-client"] = raw_request.headers.get("x-opencode-client", "desktop")
    headers["x-opencode-project"] = raw_request.headers.get("x-opencode-project", "/opencode")
    headers["x-opencode-request"] = raw_request.headers.get("x-opencode-request") or f"req_{uuid.uuid4().hex}"

    real_ip = raw_request.headers.get("x-real-ip")
    if real_ip and not _is_loopback_ip(real_ip):
        headers["x-real-ip"] = real_ip.strip()

    # Pass through only legitimate custom x-opencode-* headers
    for k, v in raw_request.headers.items():
        kl = k.lower()
        if kl.startswith("x-opencode-"):
            if kl in ("x-opencode-session", "x-opencode-client", "x-opencode-project", "x-opencode-request"):
                continue
            headers[k] = v
    return headers


def redact_headers_for_log(headers_dict: Dict[str, str]) -> Dict[str, str]:
    redacted = {}
    for k, v in headers_dict.items():
        kl = k.lower()
        if any(marker in kl.replace("-", "_") for marker in ("authorization", "token", "password", "secret", "cookie")):
            if v.lower() in ("bearer public", "public"):
                redacted[k] = v
            else:
                redacted[k] = f"{v[:10]}...[redacted]" if len(v) > 10 else "[redacted]"
        else:
            redacted[k] = v
    return redacted


def summarize_payload_for_log(payload: dict) -> dict:
    summary = {}
    for k, v in payload.items():
        if k == "messages" and isinstance(v, list):
            summary["messages_count"] = len(v)
            summary["message_roles"] = [m.get("role") for m in v if isinstance(m, dict)][:10]
        elif k == "tools" and isinstance(v, list):
            summary["tools_count"] = len(v)
            summary["tool_names"] = [
                (t.get("function", {}).get("name") if isinstance(t, dict) else str(t))
                for t in v
            ][:10]
        elif k == "system" and isinstance(v, str):
            summary["system_prompt_len"] = len(v)
        elif isinstance(v, (str, int, float, bool, type(None))):
            summary[k] = v
        elif isinstance(v, dict):
            summary[k] = list(v.keys())
        elif isinstance(v, list):
            summary[k] = f"list(len={len(v)})"
        else:
            summary[k] = f"[{type(v).__name__}]"
    return summary


recent_client_requests = deque(maxlen=50)
_recent_requests_lock = threading.Lock()


def record_client_request(endpoint: str, model: str, is_stream: bool, raw_request: Request, payload: dict, status_code: int = 200) -> Optional[dict]:
    """Stores inspected client request headers and payload summary in a bounded ring buffer for the dashboard."""
    try:
        req_id = f"req_{uuid.uuid4().hex[:8]}"
        client_ip = raw_request.client.host if raw_request.client else "unknown"
        headers_dict = dict(raw_request.headers)
        ua = headers_dict.get("user-agent", "unknown")
        session_id = headers_dict.get("x-session-id") or headers_dict.get("x-session-affinity") or headers_dict.get("x-opencode-session") or "-"

        entry = {
            "id": req_id,
            "timestamp": time.strftime("%H:%M:%S"),
            "endpoint": endpoint,
            "model": model,
            "stream": is_stream,
            "status_code": status_code,
            "client_ip": client_ip,
            "user_agent": ua,
            "session_id": session_id,
            "headers": redact_headers_for_log(headers_dict),
            "payload_summary": summarize_payload_for_log(payload),
            "error_detail": None,
        }
        with _recent_requests_lock:
            recent_client_requests.appendleft(entry)
        return entry
    except Exception:
        return None


def extract_response_body(response) -> str:
    """Safely extracts full text from a curl_cffi Response even when stream=True."""
    if response is None:
        return ""
    try:
        if hasattr(response, "text") and response.text:
            return response.text
    except Exception:
        pass
    try:
        if hasattr(response, "content") and response.content:
            return response.content.decode("utf-8", errors="replace")
    except Exception:
        pass
    try:
        if hasattr(response, "iter_content"):
            chunks = []
            for chunk in response.iter_content(chunk_size=4096):
                if chunk:
                    chunks.append(chunk)
            if chunks:
                return b"".join(chunks).decode("utf-8", errors="replace")
    except Exception:
        pass
    return ""


SAFE_UPSTREAM_HEADERS = {
    "content-type",
    "retry-after",
    "x-request-id",
    "x-ratelimit-limit",
    "x-ratelimit-remaining",
    "x-ratelimit-reset",
    "cf-ray",
}
SENSITIVE_LOG_KEYS = {"authorization", "api_key", "apikey", "token", "password", "secret"}


def redact_for_log(value):
    if isinstance(value, dict):
        return {
            key: "[redacted]"
            if any(marker in key.lower().replace("-", "_") for marker in SENSITIVE_LOG_KEYS)
            else redact_for_log(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_for_log(item) for item in value]
    if isinstance(value, str) and len(value) > 1000:
        return value[:1000] + "...[truncated]"
    return value


def log_upstream_response(response, model_name: str, endpoint: str, attempt: int, uses_proxy: bool) -> None:
    headers = {
        key: value
        for key, value in response.headers.items()
        if key.lower() in SAFE_UPSTREAM_HEADERS
    }
    egress_type = "secondary_proxy" if uses_proxy else "warp_egress"
    log.debug(
        "Upstream response model=%s endpoint=%s attempt=%s status=%s egress=%s headers=%s",
        model_name,
        endpoint,
        attempt,
        response.status_code,
        egress_type,
        headers,
    )


def rotate_egress(reason: str) -> tuple[bool, Optional[str]]:
    """Request rotation directly in-memory from rotator module or remote fallback."""
    try:
        import rotator
        success = rotator.rotate_warp(reason=reason)
        _close_all_sessions()
        return success, rotator._current_ip
    except Exception as exc:
        log.warning("In-process rotation failed, attempting HTTP fallback: %s", exc)
        try:
            response = cffi_requests.post(f"{WARP_ROTATOR_URL}/rotate", timeout=35)
            data = response.json() if response.headers.get("content-type", "").startswith("application/json") else {}
            if response.status_code == 200 and data.get("status") == "success":
                _close_all_sessions()
                return True, data.get("verified_ip")
            return False, None
        except Exception as sub_exc:
            log.warning("Rotator HTTP fallback failed: %s", sub_exc)
            return False, None


_last_429_rotation_time = 0.0
_429_rotation_lock: Optional[asyncio.Lock] = None
ROTATION_429_COOLDOWN_SECONDS = float(os.environ.get("ROTATION_429_COOLDOWN_SECONDS", "10.0"))


def _get_429_rotation_lock() -> asyncio.Lock:
    global _429_rotation_lock
    if _429_rotation_lock is None:
        _429_rotation_lock = asyncio.Lock()
    return _429_rotation_lock


async def schedule_rotation_on_429(reason: str = "HTTP 429 Rate Limit"):
    global _last_429_rotation_time
    now = time.monotonic()
    if _rotation_in_progress.is_set() or (now - _last_429_rotation_time < ROTATION_429_COOLDOWN_SECONDS):
        log.debug("Auto-rotation on 429 skipped (cooldown active or rotation in progress).")
        return

    lock = _get_429_rotation_lock()
    async with lock:
        now = time.monotonic()
        if _rotation_in_progress.is_set() or (now - _last_429_rotation_time < ROTATION_429_COOLDOWN_SECONDS):
            return
        _last_429_rotation_time = now

        log.warning("Auto-rotating IP due to upstream rate limit: %s", reason)
        signal_rotation_start()
        try:
            started = time.monotonic()
            result, verified_ip = await asyncio.to_thread(rotate_egress, reason)
            duration_ms = (time.monotonic() - started) * 1000
            record_warp_rotation(result, duration_ms, new_ip=verified_ip or "")
            if result:
                swap_warp_registration()
                log.info("Auto-rotation on 429 succeeded. New verified IP: %s (%.1fms)", verified_ip, duration_ms)
            else:
                log.warning("Auto-rotation on 429 completed without verified IP change.")
        except Exception as exc:
            log.error("Auto-rotation on 429 failed: %s", exc)
        finally:
            signal_rotation_done()


def upstream_rate_limit_response(response, model_name: str) -> JSONResponse:
    category, retry_seconds, payload = classify_upstream_429(response)
    headers = {"X-Rate-Limit-Reason": category}
    if retry_seconds is not None:
        headers["Retry-After"] = str(retry_seconds)
    log.warning(
        "Upstream 429 for model '%s' classified as %s (retry_after=%s); headers=%s payload=%s",
        model_name,
        category,
        retry_seconds,
        {key: value for key, value in response.headers.items() if key.lower() in SAFE_UPSTREAM_HEADERS},
        redact_for_log(payload),
    )
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(schedule_rotation_on_429(f"HTTP 429 ({category}) for {model_name}"))
    except RuntimeError:
        pass
    return JSONResponse(status_code=429, content=payload, headers=headers)

class EmptyStreamError(Exception):
    """Raised when upstream returns an empty or truncated stream without valid content/tool calls."""
    pass

async def stream_response(response, model_name: str, session=None) -> AsyncGenerator[bytes, None]:
    loop = asyncio.get_event_loop()
    global active_flows_count

    with flow_lock:
        active_flows_count += 1
        prom_active_flows.set(active_flows_count)
    lease_id = await asyncio.to_thread(acquire_flow_lease)

    async def keep_flow_lease_alive():
        try:
            while True:
                await asyncio.sleep(FLOW_LEASE_HEARTBEAT_SECONDS)
                await asyncio.to_thread(touch_flow_lease, lease_id)
        except asyncio.CancelledError:
            return

    lease_heartbeat = asyncio.create_task(keep_flow_lease_alive())

    chunk_count = 0
    seen_done = False

    try:
        def get_next_line(iter_lines):
            try:
                return next(iter_lines)
            except StopIteration:
                return "STOP_ITERATION"
            except Exception as exc:
                log.error(f"[STREAM DEBUG] Upstream socket/connection error for '{model_name}': {type(exc).__name__}: {exc}")
                return "SOCKET_ERROR"

        line_iter = response.iter_lines()

        while True:
            item = await loop.run_in_executor(None, get_next_line, line_iter)
            if item == "STOP_ITERATION":
                log.info(f"[STREAM DEBUG] Upstream reached natural StopIteration for '{model_name}'. Total lines: {chunk_count}")
                break
            if item == "SOCKET_ERROR":
                log.warning(f"[STREAM DEBUG] Upstream connection aborted via socket error for '{model_name}'. Lines sent: {chunk_count}")
                break

            line = item
            if line:
                raw_text = line.decode("utf-8", errors="ignore").strip()
                if not raw_text or raw_text.startswith(":"):
                    continue
                if raw_text == "data: [DONE]" or raw_text.startswith("data: [DONE]"):
                    seen_done = True
                    yield b"data: [DONE]\n\n"
                    break
                if raw_text.startswith("data:"):
                    # Discard empty trailing metadata like data: {"choices":[],"cost":"0"}
                    if '"cost":' in raw_text and '"choices":[]' in raw_text.replace(" ", ""):
                        continue
                    chunk_count += 1
                    yield line.strip() + b"\n\n"

        if not seen_done:
            yield b"data: [DONE]\n\n"
        log.info(f"Streaming completed successfully for model '{model_name}' ({chunk_count} lines sent).")
    except EmptyStreamError:
        raise
    except GeneratorExit:
        log.warning(f"[STREAM DEBUG] Client explicitly closed/aborted SSE connection for '{model_name}' after {chunk_count} lines.")
    except Exception as e:
        log.error(f"Stream exception caught for model '{model_name}': {type(e).__name__}: {e}", exc_info=True)
        if not seen_done:
            yield b"data: [DONE]\n\n"
    finally:
        lease_heartbeat.cancel()
        await asyncio.gather(lease_heartbeat, return_exceptions=True)
        await asyncio.to_thread(release_flow_lease, lease_id)
        with flow_lock:
            active_flows_count = max(0, active_flows_count - 1)
            prom_active_flows.set(active_flows_count)
        if session:
            try:
                session.close()
            except Exception:
                pass

@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request):
    return _templates.TemplateResponse(request=request, name="dashboard.html")

@app.get("/metrics-prometheus")
async def metrics_prometheus():
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)

@app.post("/api/rotate")
async def manual_rotate():
    signal_rotation_start()
    try:
        started = time.monotonic()
        result, verified_ip = await asyncio.to_thread(rotate_egress, "Manual API trigger")
        record_warp_rotation(result, (time.monotonic() - started) * 1000, new_ip=verified_ip or "")
        if result:
            swap_warp_registration()
            return {"status": "success", "verified_ip": verified_ip}
        raise HTTPException(status_code=503, detail="WARP rotator did not complete the requested rotation")
    finally:
        signal_rotation_done()

@app.get("/metrics")
async def get_metrics():
    uptime = int(time.time() - metrics["start_time"])

    # Always ensure full historical stats from SQLite DB are included
    db_usage = load_metrics_from_db()
    for m_name, m_data in db_usage.items():
        if m_name not in model_usage_stats:
            model_usage_stats[m_name] = m_data
        else:
            # Sync highest values or keep memory in sync with DB
            model_usage_stats[m_name]["requests"] = max(model_usage_stats[m_name]["requests"], m_data["requests"])
            model_usage_stats[m_name]["prompt_tokens"] = max(model_usage_stats[m_name]["prompt_tokens"], m_data["prompt_tokens"])
            model_usage_stats[m_name]["completion_tokens"] = max(model_usage_stats[m_name]["completion_tokens"], m_data["completion_tokens"])
            model_usage_stats[m_name]["total_tokens"] = max(model_usage_stats[m_name]["total_tokens"], m_data["total_tokens"])
            model_usage_stats[m_name]["estimated_cost_usd"] = max(model_usage_stats[m_name]["estimated_cost_usd"], m_data["estimated_cost_usd"])

    rotator_ip = None
    rotator_location = None
    rotator_rotations = rotation_count
    rotator_history = []

    try:
        import rotator
        if not rotator._current_ip or rotator._current_ip == "Disconnected":
            rotator._current_ip = get_public_ip()
        rotator_ip = rotator._current_ip
        rotator_rotations = rotator.rotation_count
        rotator_history = rotator.ip_history
        if rotator_ip and rotator_ip != "Disconnected":
            rotator_location = get_ip_location(rotator_ip)
    except Exception:
        pass

    # Fallback: local IP lookup
    if not rotator_ip or rotator_ip == "Disconnected":
        rotator_ip = get_public_ip() or "Disconnected"
        rotator_location = get_ip_location(rotator_ip) if rotator_ip != "Disconnected" else {"country": "Unknown", "flag": "🌐"}

    # Fallback: SQLite history
    if not rotator_history:
        rotator_history = load_ip_history_from_db()

    if not rotator_location:
        rotator_location = {"country": "Unknown", "flag": "🌐"}

    return {
        "uptime_seconds": uptime,
        "verified_public_ip": rotator_ip,
        "egress_verification_scope": "shared proxy and WARP network namespace",
        "location": rotator_location,
        "total_rotations": rotator_rotations,
        "metrics": metrics,
        "active_flows": active_flows_count,
        "discovered_models": discovered_models,
        "model_usage": model_usage_stats,
        "ip_history": rotator_history,
        "warp_quality": dict(warp_quality_stats),
        "dual_warp": dict(_dual_warp),
        "recent_requests": list(recent_client_requests),
        "rotation_in_progress": _rotation_in_progress.is_set()
    }


@app.get("/api/recent-requests")
async def get_recent_requests():
    with _recent_requests_lock:
        return {"requests": list(recent_client_requests)}

@app.get("/health")
async def health():
    ip = get_public_ip()
    prom_warp_health.set(1 if ip and ip != "Disconnected" else 0)
    db_ok = False
    try:
        _db_execute("SELECT 1")
        db_ok = True
    except Exception:
        pass
    return {
        "status": "healthy" if db_ok else "degraded",
        "database": "connected" if db_ok else "unreachable",
        "uptime_seconds": int(time.time() - metrics["start_time"]),
        "active_flows": active_flows_count,
        "total_rotations": rotation_count,
        "warp_quality": dict(warp_quality_stats),
    }

@app.get("/v1/models")
async def list_models():
    with _discovery_lock:
        return {
            "object": "list",
            "data": [
                {
                    "id": m.get("id"),
                    "object": m.get("object", "model"),
                    "created": m.get("created", 1787139400),
                    "owned_by": m.get("owned_by", "opencode"),
                }
                for m in discovered_models
            ]
        }

@app.post("/v1/chat/completions")
async def chat_completions(raw_request: Request):
    metrics["total_requests"] += 1
    prom_requests_total.labels(model="chat", endpoint="chat_completions").inc()
    await wait_for_rotation_drain()

    start_time = time.time()
    try:
        payload = await raw_request.json()
    except Exception:
        payload = {}

    current_model = payload.get("model", "deepseek-v4-flash-free")
    is_stream = payload.get("stream", False)
    log.info(f"Received request for model '{current_model}' (Stream: {is_stream} | Has Tools: {'tools' in payload})")

    # Record client headers & payload summary for web dashboard inspector
    req_entry = record_client_request("/v1/chat/completions", current_model, is_stream, raw_request, payload)

    # Ensure end-user identifier is present for models requiring safety_identifier / user (e.g. contributor / vertex models)
    if not payload.get("user"):
        payload["user"] = "opencode-user"
    if not payload.get("safety_identifier"):
        payload["safety_identifier"] = payload.get("user", "opencode-user")

    headers = build_opencode_headers(raw_request)

    for attempt in range(1, MAX_RETRIES_ON_429 + 1):
        session = None
        try:
            proxies = get_next_outbound_proxy()
            session = create_fresh_session(is_stream)
            response = session.post(
                TARGET_ZEN_URL,
                json=payload,
                headers=headers,
                impersonate="chrome124",
                stream=is_stream,
                proxies=proxies,
                timeout=STREAM_TIMEOUT if is_stream else 120
            )
            log_upstream_response(response, current_model, "chat_completions", attempt, proxies is not None)

            if response.status_code == 429:
                metrics["rate_limited_requests"] += 1
                prom_requests_rate_limited.labels(model=current_model).inc()
                category, retry_seconds, payload = classify_upstream_429(response)
                if attempt < MAX_RETRIES_ON_429 and category != "quota":
                    backoff = retry_seconds if (retry_seconds and retry_seconds <= 10) else compute_backoff_delay(attempt, INITIAL_BACKOFF)
                    log.warning("Upstream 429 (%s) for '%s' (attempt %s/%s). Rotating IP and retrying in %.2fs...", category, current_model, attempt, MAX_RETRIES_ON_429, backoff)
                    await schedule_rotation_on_429(f"429 rate limit ({category}) on attempt {attempt}")
                    await wait_for_rotation_drain()
                    await asyncio.sleep(backoff)
                    headers = build_opencode_headers(raw_request)
                    continue
                return upstream_rate_limit_response(response, current_model)

            if response.status_code >= 500:
                delay = compute_backoff_delay(attempt, INITIAL_BACKOFF)
                log.warning("Upstream HTTP %s for '%s'; retrying without egress rotation in %.2fs.", response.status_code, current_model, delay)
                await asyncio.sleep(delay)
                continue

            if response.status_code != 200:
                raw_err_text = extract_response_body(response)
                log.warning("Upstream returned HTTP %s for '%s': %s", response.status_code, current_model, raw_err_text[:500])
                if req_entry:
                    req_entry["status_code"] = response.status_code
                    req_entry["error_detail"] = raw_err_text[:2000]
                try:
                    res_json = json.loads(raw_err_text) if raw_err_text else response.json()
                    return JSONResponse(status_code=response.status_code, content=res_json)
                except Exception:
                    return JSONResponse(
                        status_code=response.status_code,
                        content={"error": {"message": raw_err_text or f"Upstream returned HTTP {response.status_code}", "type": "upstream_error", "code": response.status_code}}
                    )

            metrics["successful_requests"] += 1
            prom_requests_success.labels(model=current_model).inc()
            prom_request_duration.labels(model=current_model, endpoint="chat_completions").observe(time.time() - start_time)

            if is_stream:
                try:
                    stream_gen = stream_response(response, current_model, session=session)
                    track_token_usage(current_model, prompt_tokens=100, completion_tokens=150)
                    return StreamingResponse(
                        stream_gen,
                        media_type="text/event-stream",
                        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"}
                    )
                except EmptyStreamError:
                    log.warning("Empty stream for '%s'; retrying without egress rotation (%s/%s).", current_model, attempt, MAX_RETRIES_ON_429)
                    delay = compute_backoff_delay(attempt, INITIAL_BACKOFF)
                    await asyncio.sleep(delay)
                    continue
            else:
                with FlowContext():
                    try:
                        res_json = await asyncio.to_thread(response.json)
                        usage = res_json.get("usage", {})
                        track_token_usage(
                            current_model,
                            prompt_tokens=usage.get("prompt_tokens", DEFAULT_PROMPT_TOKENS),
                            completion_tokens=usage.get("completion_tokens", DEFAULT_COMPLETION_TOKENS)
                        )
                        return JSONResponse(content=res_json)
                    except Exception:
                        track_token_usage(current_model, prompt_tokens=DEFAULT_PROMPT_TOKENS, completion_tokens=DEFAULT_COMPLETION_TOKENS)
                        return JSONResponse(content={
                            "id": f"chatcmpl-zen-resp-{int(time.time())}",
                            "object": "chat.completion",
                            "created": int(time.time()),
                            "model": current_model,
                            "choices": [{"index": 0, "message": {"role": "assistant", "content": response.text}, "finish_reason": "stop"}]
                        })
                    finally:
                        if session:
                            try:
                                session.close()
                            except Exception:
                                pass

        except Exception as e:
            if session:
                try:
                    session.close()
                except Exception:
                    pass
            log.error(f"[Attempt {attempt}/{MAX_RETRIES_ON_429}] Connection error for model '{current_model}': {type(e).__name__}: {e}")
            if attempt < MAX_RETRIES_ON_429:
                await asyncio.sleep(min(2 ** attempt, BACKOFF_CAP))
            continue

    log.error(f"All {MAX_RETRIES_ON_429} attempts exhausted for model '{current_model}'. Returning 503.")
    return JSONResponse(
        status_code=503,
        content={"error": {"message": f"Upstream unavailable after {MAX_RETRIES_ON_429} attempts. Please retry.", "type": "upstream_error", "code": 503}},
        headers={"Retry-After": "10"}
    )

# -----------------------------------------------------------------------------
# Anthropic API Compatibility Endpoint (/v1/messages)
# -----------------------------------------------------------------------------
@app.post("/v1/messages")
async def anthropic_messages(raw_request: Request):
    metrics["total_requests"] += 1
    prom_requests_total.labels(model="messages", endpoint="anthropic_messages").inc()
    await wait_for_rotation_drain()

    start_time = time.time()
    try:
        body = await raw_request.json()
    except Exception:
        body = {}

    model_name = body.get("model", "deepseek-v4-flash-free")
    is_stream = body.get("stream", False)
    log.info(f"Received Anthropic-format request for model '{model_name}' (Stream: {is_stream})")

    # Record client headers & payload summary for web dashboard inspector
    req_entry = record_client_request("/v1/messages", model_name, is_stream, raw_request, body)

    client_api_key = raw_request.headers.get("x-api-key") or ""
    if not client_api_key:
        auth = raw_request.headers.get("authorization", "")
        if auth.startswith("Bearer "):
            client_api_key = auth[7:]

    # Enforce 'public' key unless a valid non-dummy API key is provided
    effective_api_key = "public"
    if client_api_key and client_api_key.lower() not in ("any", "none", "null", "test", "dummy", "public", "undefined", "false"):
        effective_api_key = client_api_key

    # Ensure end-user identifier is present
    if not body.get("user"):
        body["user"] = "opencode-user"
    if not body.get("safety_identifier"):
        body["safety_identifier"] = body.get("user", "opencode-user")
    if isinstance(body.get("metadata"), dict):
        if not body["metadata"].get("user_id"):
            body["metadata"]["user_id"] = body.get("user", "opencode-user")
    elif "metadata" not in body:
        body["metadata"] = {"user_id": body.get("user", "opencode-user")}

    headers = build_opencode_headers(raw_request)
    headers["x-api-key"] = effective_api_key

    for attempt in range(1, MAX_RETRIES_ON_429 + 1):
        session = None
        try:
            proxies = get_next_outbound_proxy()
            session = create_fresh_session(is_stream)
            response = session.post(
                TARGET_ZEN_ANTHROPIC_URL,
                json=body,
                headers=headers,
                impersonate="chrome124",
                stream=is_stream,
                proxies=proxies,
                timeout=STREAM_TIMEOUT if is_stream else 120,
            )
            log_upstream_response(response, model_name, "messages", attempt, proxies is not None)

            if response.status_code == 429:
                metrics["rate_limited_requests"] += 1
                prom_requests_rate_limited.labels(model=model_name).inc()
                category, retry_seconds, payload = classify_upstream_429(response)
                if attempt < MAX_RETRIES_ON_429 and category != "quota":
                    backoff = retry_seconds if (retry_seconds and retry_seconds <= 10) else compute_backoff_delay(attempt, INITIAL_BACKOFF)
                    log.warning("Upstream 429 (%s) for '%s' (attempt %s/%s). Rotating IP and retrying in %.2fs...", category, model_name, attempt, MAX_RETRIES_ON_429, backoff)
                    await schedule_rotation_on_429(f"429 rate limit ({category}) on attempt {attempt}")
                    await wait_for_rotation_drain()
                    await asyncio.sleep(backoff)
                    headers = build_opencode_headers(raw_request)
                    headers["x-api-key"] = effective_api_key
                    continue
                return upstream_rate_limit_response(response, model_name)

            if response.status_code >= 500:
                delay = compute_backoff_delay(attempt, INITIAL_BACKOFF)
                log.warning("Upstream HTTP %s for '%s'; retrying without egress rotation in %.2fs.", response.status_code, model_name, delay)
                await asyncio.sleep(delay)
                continue

            if response.status_code != 200:
                raw_err_text = extract_response_body(response)
                log.warning("Upstream returned HTTP %s for '%s': %s", response.status_code, model_name, raw_err_text[:500])
                if req_entry:
                    req_entry["status_code"] = response.status_code
                    req_entry["error_detail"] = raw_err_text[:2000]
                try:
                    res_json = json.loads(raw_err_text) if raw_err_text else response.json()
                    return JSONResponse(status_code=response.status_code, content=res_json)
                except Exception:
                    return JSONResponse(
                        status_code=response.status_code,
                        content={"error": {"type": "upstream_error", "message": raw_err_text or f"HTTP {response.status_code}"}}
                    )

            metrics["successful_requests"] += 1
            prom_requests_success.labels(model=model_name).inc()
            prom_request_duration.labels(model=model_name, endpoint="anthropic_messages").observe(time.time() - start_time)

            if is_stream:
                return StreamingResponse(
                    stream_response(response, model_name, session=session),
                    media_type="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
                )
            else:
                with FlowContext():
                    try:
                        res_json = await asyncio.to_thread(response.json)
                        usage = res_json.get("usage", {})
                        track_token_usage(
                            model_name,
                            prompt_tokens=usage.get("input_tokens", DEFAULT_PROMPT_TOKENS),
                            completion_tokens=usage.get("output_tokens", DEFAULT_COMPLETION_TOKENS),
                        )
                        return JSONResponse(content=res_json)
                    except Exception:
                        track_token_usage(model_name, prompt_tokens=DEFAULT_PROMPT_TOKENS, completion_tokens=DEFAULT_COMPLETION_TOKENS)
                        return JSONResponse(content={
                            "id": f"msg-zen-{uuid.uuid4().hex[:12]}",
                            "type": "message",
                            "role": "assistant",
                            "content": [{"type": "text", "text": response.text}],
                            "model": model_name,
                            "stop_reason": "end_turn",
                            "usage": {"input_tokens": DEFAULT_PROMPT_TOKENS, "output_tokens": DEFAULT_COMPLETION_TOKENS}
                        })
                    finally:
                        if session:
                            try:
                                session.close()
                            except Exception:
                                pass

        except Exception as e:
            if session:
                try:
                    session.close()
                except Exception:
                    pass
            log.error(f"[Attempt {attempt}/{MAX_RETRIES_ON_429}] Anthropic endpoint error for model '{model_name}': {type(e).__name__}: {e}")
            if attempt < MAX_RETRIES_ON_429:
                await asyncio.sleep(min(2 ** attempt, BACKOFF_CAP))
            continue

    log.error(f"All {MAX_RETRIES_ON_429} attempts exhausted for Anthropic model '{model_name}'. Returning 503.")
    return JSONResponse(
        status_code=503,
        content={"error": {"message": f"Upstream unavailable after {MAX_RETRIES_ON_429} attempts. Please retry.", "type": "upstream_error", "code": 503}},
        headers={"Retry-After": "10"}
    )

# -----------------------------------------------------------------------------
# OpenAI Responses API Endpoint (/v1/responses)
# -----------------------------------------------------------------------------
@app.post("/v1/responses")
async def responses_endpoint(raw_request: Request):
    metrics["total_requests"] += 1
    prom_requests_total.labels(model="responses", endpoint="responses").inc()
    await wait_for_rotation_drain()

    start_time = time.time()
    try:
        body = await raw_request.json()
    except Exception:
        body = {}

    model_name = body.get("model", "deepseek-v4-flash-free")
    is_stream = body.get("stream", False)
    log.info(f"Received Responses API request for model '{model_name}' (Stream: {is_stream})")

    # Record client headers & payload summary for web dashboard inspector
    req_entry = record_client_request("/v1/responses", model_name, is_stream, raw_request, body)

    # Ensure end-user identifier is present
    if not body.get("user"):
        body["user"] = "opencode-user"
    if not body.get("safety_identifier"):
        body["safety_identifier"] = body.get("user", "opencode-user")

    headers = build_opencode_headers(raw_request)

    for attempt in range(1, MAX_RETRIES_ON_429 + 1):
        session = None
        try:
            proxies = get_next_outbound_proxy()
            session = create_fresh_session(is_stream)
            response = session.post(
                TARGET_ZEN_RESPONSES_URL,
                json=body,
                headers=headers,
                impersonate="chrome124",
                stream=is_stream,
                proxies=proxies,
                timeout=STREAM_TIMEOUT if is_stream else 120,
            )
            log_upstream_response(response, model_name, "responses", attempt, proxies is not None)

            if response.status_code == 429:
                metrics["rate_limited_requests"] += 1
                prom_requests_rate_limited.labels(model=model_name).inc()
                category, retry_seconds, payload = classify_upstream_429(response)
                if attempt < MAX_RETRIES_ON_429 and category != "quota":
                    backoff = retry_seconds if (retry_seconds and retry_seconds <= 10) else compute_backoff_delay(attempt, INITIAL_BACKOFF)
                    log.warning("Upstream 429 (%s) for '%s' (attempt %s/%s). Rotating IP and retrying in %.2fs...", category, model_name, attempt, MAX_RETRIES_ON_429, backoff)
                    await schedule_rotation_on_429(f"429 rate limit ({category}) on attempt {attempt}")
                    await wait_for_rotation_drain()
                    await asyncio.sleep(backoff)
                    headers = build_opencode_headers(raw_request)
                    continue
                return upstream_rate_limit_response(response, model_name)

            if response.status_code >= 500:
                delay = compute_backoff_delay(attempt, INITIAL_BACKOFF)
                log.warning("Upstream HTTP %s for '%s'; retrying without egress rotation in %.2fs.", response.status_code, model_name, delay)
                await asyncio.sleep(delay)
                continue

            if response.status_code != 200:
                raw_err_text = extract_response_body(response)
                log.warning("Upstream returned HTTP %s for '%s': %s", response.status_code, model_name, raw_err_text[:500])
                if req_entry:
                    req_entry["status_code"] = response.status_code
                    req_entry["error_detail"] = raw_err_text[:2000]
                try:
                    res_json = json.loads(raw_err_text) if raw_err_text else response.json()
                    return JSONResponse(status_code=response.status_code, content=res_json)
                except Exception:
                    return JSONResponse(
                        status_code=response.status_code,
                        content={"error": {"type": "upstream_error", "message": raw_err_text or f"HTTP {response.status_code}"}}
                    )

            metrics["successful_requests"] += 1
            prom_requests_success.labels(model=model_name).inc()
            prom_request_duration.labels(model=model_name, endpoint="responses").observe(time.time() - start_time)

            if is_stream:
                return StreamingResponse(
                    stream_response(response, model_name, session=session),
                    media_type="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
                )
            else:
                with FlowContext():
                    try:
                        res_json = await asyncio.to_thread(response.json)
                        return JSONResponse(content=res_json)
                    except Exception:
                        return JSONResponse(content={
                            "id": f"resp-zen-{uuid.uuid4().hex[:12]}",
                            "model": model_name,
                            "output": response.text
                        })
                    finally:
                        if session:
                            try:
                                session.close()
                            except Exception:
                                pass

        except Exception as e:
            if session:
                try:
                    session.close()
                except Exception:
                    pass
            log.error(f"[Attempt {attempt}/{MAX_RETRIES_ON_429}] Responses endpoint error for model '{model_name}': {type(e).__name__}: {e}")
            if attempt < MAX_RETRIES_ON_429:
                await asyncio.sleep(min(2 ** attempt, BACKOFF_CAP))
            continue

    log.error(f"All {MAX_RETRIES_ON_429} attempts exhausted for Responses model '{model_name}'. Returning 503.")
    return JSONResponse(
        status_code=503,
        content={"error": {"message": f"Upstream unavailable after {MAX_RETRIES_ON_429} attempts. Please retry.", "type": "upstream_error", "code": 503}},
        headers={"Retry-After": "10"}
    )

# Global exception handler for standard error format
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    log.error(f"Unhandled error on {request.method} {request.url.path}: {exc}")
    return JSONResponse(
        status_code=500,
        content={"error": {"message": "Internal server error", "type": "internal_error", "code": 500}},
    )

@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": {"message": exc.detail, "type": "http_error", "code": exc.status_code}},
    )

if __name__ == "__main__":
    load_proxy_list()
    log.info(f"Starting OpenCode IP Proxy Server on {HOST}:{PORT}...")
    uvicorn.run(app, host=HOST, port=PORT)
