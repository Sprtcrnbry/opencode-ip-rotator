import asyncio
import json
import logging
import os
import signal
import threading
import time
from typing import AsyncGenerator, Dict, List, Optional
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import StreamingResponse, JSONResponse, HTMLResponse
from pydantic import BaseModel, Field
import uvicorn
from urllib.request import Request as UrlRequest, urlopen
from urllib.error import HTTPError, URLError

from rotator import rotate_warp, flow_lock, active_flows_count, get_public_ip, get_ip_location, rotation_count

import sqlite3
from pathlib import Path

# Proxy Pool / Custom Proxy List Support
# -----------------------------------------------------------------------------
PROXY_FILE = Path(os.environ.get("PROXY_LIST_FILE", "/app/data/proxies.txt"))
_proxy_pool: List[str] = []
_proxy_index = 0
_proxy_lock = threading.Lock()

def load_proxy_list():
    """Loads proxy addresses from proxies.txt or PROXY_LIST environment variable."""
    global _proxy_pool
    proxies = []
    
    # 1. Try reading from proxies.txt file
    if PROXY_FILE.exists():
        try:
            with open(PROXY_FILE, "r", encoding="utf-8") as f:
                lines = [line.strip() for line in f if line.strip() and not line.startswith("#")]
                proxies.extend(lines)
        except Exception as e:
            log.error(f"Error reading proxies.txt: {e}")

    # 2. Try environment variable PROXY_LIST (comma-separated)
    env_proxies = os.environ.get("PROXY_LIST", "").strip()
    if env_proxies:
        proxies.extend([p.strip() for p in env_proxies.split(",") if p.strip()])

    _proxy_pool = list(dict.fromkeys(proxies)) # Unique proxy list
    if _proxy_pool:
        log.info(f"Loaded {len(_proxy_pool)} custom proxies into pool.")

def get_next_outbound_proxy() -> Optional[Dict[str, str]]:
    """Retrieves next proxy from pool using Round-Robin, or falls back to CUSTOM_OUTBOUND_PROXY."""
    global _proxy_index
    with _proxy_lock:
        if _proxy_pool:
            proxy_url = _proxy_pool[_proxy_index % len(_proxy_pool)]
            _proxy_index += 1
            return {"http": proxy_url, "https": proxy_url}
        
        custom_proxy = os.environ.get("CUSTOM_OUTBOUND_PROXY", "").strip()
        if custom_proxy:
            return {"http": custom_proxy, "https": custom_proxy}
        
# SQLite Database setup
DB_FILE = Path(os.environ.get("METRICS_DB_PATH", "/app/data/metrics.db"))

def init_db():
    """Initializes SQLite database and creates metrics & ip_history tables if not exists."""
    DB_FILE.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_FILE))
    cursor = conn.cursor()
    cursor.execute("""
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
    cursor.execute("""
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
    conn.commit()
    conn.close()

def log_ip_rotation_to_db(ip: str, country: str, flag: str, timestamp: str, reason: str):
    """Saves IP rotation event into SQLite DB."""
    try:
        conn = sqlite3.connect(str(DB_FILE))
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO ip_history (ip, country, flag, timestamp, reason) VALUES (?, ?, ?, ?, ?)",
            (ip, country, flag, timestamp, reason)
        )
        conn.commit()
        conn.close()
    except Exception as e:
        log.error(f"Failed to log IP rotation to DB: {e}")

def load_ip_history_from_db() -> List[Dict[str, any]]:
    """Fetches last 20 IP rotations from SQLite DB."""
    if not DB_FILE.exists():
        return []
    try:
        conn = sqlite3.connect(str(DB_FILE))
        cursor = conn.cursor()
        cursor.execute("SELECT ip, country, flag, timestamp, reason FROM ip_history ORDER BY id DESC LIMIT 20")
        rows = cursor.fetchall()
        conn.close()
        
        history = []
        for r in reversed(rows):
            history.append({
                "ip": r[0],
                "country": r[1],
                "flag": r[2],
                "timestamp": r[3],
                "reason": r[4]
            })
        return history
    except Exception as e:
        log.error(f"Error loading IP history from DB: {e}")
        return []

def load_metrics_from_db() -> Dict[str, Dict[str, any]]:
    """Loads historical usage metrics from SQLite DB."""
    if not DB_FILE.exists():
        return {}
    try:
        conn = sqlite3.connect(str(DB_FILE))
        cursor = conn.cursor()
        cursor.execute("SELECT model_name, requests, prompt_tokens, completion_tokens, total_tokens, estimated_cost_usd FROM model_usage")
        rows = cursor.fetchall()
        conn.close()
        
        stats = {}
        for r in rows:
            stats[r[0]] = {
                "requests": r[1],
                "prompt_tokens": r[2],
                "completion_tokens": r[3],
                "total_tokens": r[4],
                "estimated_cost_usd": r[5]
            }
        return stats
    except Exception as e:
        log.error(f"Error loading metrics from DB: {e}")
        return {}

# Model pricing reference (USD per 1M tokens)
MODEL_PRICING = {
    "deepseek-v4-flash-free": {"input_per_1m": 0.15, "output_per_1m": 0.60},
    "mimo-v2.5-free": {"input_per_1m": 0.20, "output_per_1m": 0.80},
    "qwen3.6-plus-free": {"input_per_1m": 0.40, "output_per_1m": 1.20},
    "minimax-m3-free": {"input_per_1m": 0.30, "output_per_1m": 1.00},
    "nemotron-3-ultra-free": {"input_per_1m": 0.25, "output_per_1m": 0.90},
    "ling-3.0-flash-free": {"input_per_1m": 0.15, "output_per_1m": 0.50},
    "laguna-s-2.1-free": {"input_per_1m": 0.20, "output_per_1m": 0.70},
}

def track_token_usage(model_name: str, prompt_tokens: int = 0, completion_tokens: int = 0):
    """Tracks request counts, consumed tokens, and persists to SQLite database."""
    if model_name not in model_usage_stats:
        model_usage_stats[model_name] = {
            "requests": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "estimated_cost_usd": 0.0
        }
    
    pricing = MODEL_PRICING.get(model_name, {"input_per_1m": 0.20, "output_per_1m": 0.80})
    prompt_cost = (prompt_tokens / 1_000_000) * pricing["input_per_1m"]
    completion_cost = (completion_tokens / 1_000_000) * pricing["output_per_1m"]
    cost = prompt_cost + completion_cost

    model_usage_stats[model_name]["requests"] += 1
    model_usage_stats[model_name]["prompt_tokens"] += prompt_tokens
    model_usage_stats[model_name]["completion_tokens"] += completion_tokens
    model_usage_stats[model_name]["total_tokens"] += (prompt_tokens + completion_tokens)
    model_usage_stats[model_name]["estimated_cost_usd"] += cost

    # Persist to SQLite
    try:
        conn = sqlite3.connect(str(DB_FILE))
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO model_usage (model_name, requests, prompt_tokens, completion_tokens, total_tokens, estimated_cost_usd)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(model_name) DO UPDATE SET
                requests = requests + excluded.requests,
                prompt_tokens = prompt_tokens + excluded.prompt_tokens,
                completion_tokens = completion_tokens + excluded.completion_tokens,
                total_tokens = total_tokens + excluded.total_tokens,
                estimated_cost_usd = estimated_cost_usd + excluded.estimated_cost_usd,
                updated_at = CURRENT_TIMESTAMP
        """, (
            model_name, 1, prompt_tokens, completion_tokens, prompt_tokens + completion_tokens, cost
        ))
        conn.commit()
        conn.close()
    except Exception as e:
        log.error(f"Failed to persist metrics to SQLite: {e}")

# -----------------------------------------------------------------------------
# Feature 3: Minimalist White Dashboard (Tailwind CSS + shadcn/ui Design System)
# -----------------------------------------------------------------------------
DASHBOARD_HTML = """
<!DOCTYPE html>
<html lang="en" class="h-full bg-slate-50/50">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>OpenCode IP Rotator</title>
    <!-- Tailwind CSS CDN -->
    <script src="https://cdn.tailwindcss.com"></script>
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
    <script>
        tailwind.config = {
            theme: {
                extend: {
                    fontFamily: {
                        sans: ['Inter', 'sans-serif'],
                        mono: ['JetBrains Mono', 'monospace'],
                    }
                }
            }
        }
    </script>
</head>
<body class="h-full text-slate-900 font-sans antialiased selection:bg-slate-900 selection:text-white">
    <!-- Navbar -->
    <header class="bg-white/80 backdrop-blur-md border-b border-slate-200/80 sticky top-0 z-50">
        <div class="max-w-5xl mx-auto px-4 sm:px-6 h-14 flex items-center justify-between">
            <div class="flex items-center space-x-3">
                <div class="w-7 h-7 bg-slate-900 text-white rounded-md flex items-center justify-center font-semibold text-xs tracking-wider">
                    OP
                </div>
                <div class="flex items-center space-x-2">
                    <span class="font-semibold text-sm tracking-tight text-slate-900">opencode-ip-rotator</span>
                    <span class="text-[10px] bg-slate-100 text-slate-600 font-mono font-medium px-2 py-0.5 rounded border border-slate-200">v3.0</span>
                </div>
            </div>
            
            <div class="flex items-center space-x-3">
                <div class="flex items-center space-x-1.5 bg-emerald-50 text-emerald-700 border border-emerald-200/60 text-xs px-2.5 py-1 rounded-full font-medium">
                    <span class="w-1.5 h-1.5 bg-emerald-500 rounded-full animate-pulse"></span>
                    <span class="text-[11px]">Active</span>
                </div>
                <button id="rotate-btn" onclick="rotateNow()" class="inline-flex items-center justify-center bg-slate-900 hover:bg-slate-800 text-white text-xs font-medium h-8 px-3 rounded-md transition-all shadow-sm active:scale-95">
                    Rotate IP
                </button>
            </div>
        </div>
    </header>

    <!-- Main Workspace Container -->
    <main class="max-w-5xl mx-auto px-4 sm:px-6 py-8 space-y-6">
        
        <!-- Metrics Cards Grid -->
        <div class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-3.5">
            <!-- Verified Public IP -->
            <div class="bg-white border border-slate-200/80 rounded-xl p-4 shadow-sm space-y-1">
                <p class="text-[11px] font-medium text-slate-400 uppercase tracking-wider">Verified IP</p>
                <div class="flex items-center space-x-2">
                    <span id="flag" class="text-base">🌐</span>
                    <span id="ip" class="text-base font-bold font-mono text-slate-900 tracking-tight">Loading...</span>
                </div>
                <p id="location" class="text-xs text-slate-500 font-medium truncate pt-0.5">Detecting location...</p>
            </div>

            <!-- Total Requests -->
            <div class="bg-white border border-slate-200/80 rounded-xl p-4 shadow-sm space-y-1">
                <p class="text-[11px] font-medium text-slate-400 uppercase tracking-wider">Total Calls</p>
                <p id="requests" class="text-xl font-bold font-mono text-slate-900">0</p>
                <p class="text-xs text-slate-400">Proxied AI completions</p>
            </div>

            <!-- Rotations & 429 Bypassed -->
            <div class="bg-white border border-slate-200/80 rounded-xl p-4 shadow-sm space-y-1">
                <p class="text-[11px] font-medium text-slate-400 uppercase tracking-wider">Rotations / 429s</p>
                <div class="flex items-baseline space-x-1.5 font-mono">
                    <span id="rotations" class="text-xl font-bold text-slate-900">0</span>
                    <span class="text-slate-300 text-sm">/</span>
                    <span id="ratelimits" class="text-base font-semibold text-amber-600">0</span>
                </div>
                <p class="text-xs text-slate-400">WARP cycles / Bypassed</p>
            </div>

            <!-- Total Saved Value -->
            <div class="bg-emerald-50/40 border border-emerald-200/60 rounded-xl p-4 shadow-sm space-y-1">
                <p class="text-[11px] font-medium text-emerald-700 uppercase tracking-wider">Est. Saved Value</p>
                <p id="total-saved" class="text-xl font-bold font-mono text-emerald-600">$0.0000</p>
                <p class="text-xs text-emerald-600/80">Equivalent OpenCode cost</p>
            </div>
        </div>

        <!-- Model Usage & Cost Table -->
        <div class="bg-white border border-slate-200/80 rounded-xl shadow-sm overflow-hidden">
            <div class="px-5 py-3.5 border-b border-slate-100 flex justify-between items-center bg-slate-50/50">
                <div>
                    <h2 class="text-xs font-semibold text-slate-900 uppercase tracking-wider">Model Usage Breakdown</h2>
                </div>
                <span id="model-count-badge" class="bg-white text-slate-600 text-[11px] font-mono font-medium px-2 py-0.5 rounded border border-slate-200">0 Active</span>
            </div>

            <div class="overflow-x-auto">
                <table class="w-full text-left text-xs">
                    <thead class="bg-slate-50/60 text-slate-400 font-medium uppercase border-b border-slate-100 text-[10px] tracking-wider">
                        <tr>
                            <th class="px-5 py-2.5">Model</th>
                            <th class="px-5 py-2.5">Requests</th>
                            <th class="px-5 py-2.5">Prompt Tokens</th>
                            <th class="px-5 py-2.5">Completion Tokens</th>
                            <th class="px-5 py-2.5">Total Tokens</th>
                            <th class="px-5 py-2.5 text-right">Saved USD</th>
                        </tr>
                    </thead>
                    <tbody id="usage-table-body" class="divide-y divide-slate-100 font-mono text-slate-700">
                        <tr>
                            <td colspan="6" class="px-5 py-6 text-center text-slate-400 italic font-sans text-xs">No active completions logged yet. Send prompts from OpenCode to track live metrics.</td>
                        </tr>
                    </tbody>
                </table>
            </div>

            <!-- Model Usage Pagination Footer -->
            <div class="px-5 py-2.5 border-t border-slate-100 bg-slate-50/40 flex items-center justify-between text-xs text-slate-500 font-sans">
                <span id="model-page-info">Page 1 of 1</span>
                <div class="flex items-center space-x-1">
                    <button id="model-prev-btn" onclick="changeModelPage(-1)" class="px-2 py-1 border border-slate-200 rounded text-[11px] bg-white hover:bg-slate-50 disabled:opacity-40 disabled:cursor-not-allowed">Previous</button>
                    <button id="model-next-btn" onclick="changeModelPage(1)" class="px-2 py-1 border border-slate-200 rounded text-[11px] bg-white hover:bg-slate-50 disabled:opacity-40 disabled:cursor-not-allowed">Next</button>
                </div>
            </div>
        </div>

        <!-- IP Rotation Log Table -->
        <div class="bg-white border border-slate-200/80 rounded-xl shadow-sm overflow-hidden">
            <div class="px-5 py-3.5 border-b border-slate-100 bg-slate-50/50">
                <h2 class="text-xs font-semibold text-slate-900 uppercase tracking-wider">IP Rotation Log</h2>
            </div>
            <div class="overflow-x-auto">
                <table class="w-full text-left text-xs">
                    <thead class="bg-slate-50/60 text-slate-400 font-medium uppercase border-b border-slate-100 text-[10px] tracking-wider">
                        <tr>
                            <th class="px-5 py-2.5">Time</th>
                            <th class="px-5 py-2.5">Assigned IP</th>
                            <th class="px-5 py-2.5">Country</th>
                            <th class="px-5 py-2.5 text-right">Reason</th>
                        </tr>
                    </thead>
                    <tbody id="ip-history-body" class="divide-y divide-slate-100 font-mono text-slate-700">
                        <tr>
                            <td colspan="4" class="px-5 py-6 text-center text-slate-400 italic font-sans text-xs">No rotation history recorded.</td>
                        </tr>
                    </tbody>
                </table>
            </div>

            <!-- IP Rotation Pagination Footer -->
            <div class="px-5 py-2.5 border-t border-slate-100 bg-slate-50/40 flex items-center justify-between text-xs text-slate-500 font-sans">
                <span id="ip-page-info">Page 1 of 1</span>
                <div class="flex items-center space-x-1">
                    <button id="ip-prev-btn" onclick="changeIpPage(-1)" class="px-2 py-1 border border-slate-200 rounded text-[11px] bg-white hover:bg-slate-50 disabled:opacity-40 disabled:cursor-not-allowed">Previous</button>
                    <button id="ip-next-btn" onclick="changeIpPage(1)" class="px-2 py-1 border border-slate-200 rounded text-[11px] bg-white hover:bg-slate-50 disabled:opacity-40 disabled:cursor-not-allowed">Next</button>
                </div>
            </div>
        </div>
    </main>

    <script>
        const PAGE_SIZE = 5;
        let currentModelPage = 1;
        let currentIpPage = 1;
        let cachedModelKeys = [];
        let cachedUsageData = {};
        let cachedIpHistory = [];

        function renderModelTable() {
            const tableBody = document.getElementById('usage-table-body');
            const totalPages = Math.max(1, Math.ceil(cachedModelKeys.length / PAGE_SIZE));
            if (currentModelPage > totalPages) currentModelPage = totalPages;

            const start = (currentModelPage - 1) * PAGE_SIZE;
            const pageKeys = cachedModelKeys.slice(start, start + PAGE_SIZE);

            if (pageKeys.length > 0) {
                tableBody.innerHTML = pageKeys.map(model => {
                    const item = cachedUsageData[model];
                    const cost = item.estimated_cost_usd || 0;
                    return `
                        <tr class="hover:bg-slate-50/60 transition-colors">
                            <td class="px-5 py-2.5 font-sans font-medium text-slate-900">${model}</td>
                            <td class="px-5 py-2.5">${item.requests}</td>
                            <td class="px-5 py-2.5 text-slate-400">${item.prompt_tokens.toLocaleString()}</td>
                            <td class="px-5 py-2.5 text-slate-400">${item.completion_tokens.toLocaleString()}</td>
                            <td class="px-5 py-2.5 font-bold text-slate-900">${item.total_tokens.toLocaleString()}</td>
                            <td class="px-5 py-2.5 text-right font-bold text-emerald-600">$${cost.toFixed(4)}</td>
                        </tr>
                    `;
                }).join('');
            } else {
                tableBody.innerHTML = `<tr><td colspan="6" class="px-5 py-6 text-center text-slate-400 italic font-sans text-xs">No active completions logged yet. Send prompts from OpenCode to track live metrics.</td></tr>`;
            }

            document.getElementById('model-page-info').innerText = `Page ${currentModelPage} of ${totalPages}`;
            document.getElementById('model-prev-btn').disabled = (currentModelPage <= 1);
            document.getElementById('model-next-btn').disabled = (currentModelPage >= totalPages);
        }

        function renderIpTable() {
            const ipHistoryBody = document.getElementById('ip-history-body');
            const reversedHistory = cachedIpHistory.slice().reverse();
            const totalPages = Math.max(1, Math.ceil(reversedHistory.length / PAGE_SIZE));
            if (currentIpPage > totalPages) currentIpPage = totalPages;

            const start = (currentIpPage - 1) * PAGE_SIZE;
            const pageItems = reversedHistory.slice(start, start + PAGE_SIZE);

            if (pageItems.length > 0) {
                ipHistoryBody.innerHTML = pageItems.map(h => `
                    <tr class="hover:bg-slate-50/60 transition-colors">
                        <td class="px-5 py-2.5 text-slate-400">${h.timestamp}</td>
                        <td class="px-5 py-2.5 font-bold text-slate-900">${h.ip}</td>
                        <td class="px-5 py-2.5 font-sans">${h.flag} ${h.country}</td>
                        <td class="px-5 py-2.5 text-right text-slate-400 font-sans">${h.reason}</td>
                    </tr>
                `).join('');
            } else {
                ipHistoryBody.innerHTML = `<tr><td colspan="4" class="px-5 py-6 text-center text-slate-400 italic font-sans text-xs">No rotation history recorded.</td></tr>`;
            }

            document.getElementById('ip-page-info').innerText = `Page ${currentIpPage} of ${totalPages}`;
            document.getElementById('ip-prev-btn').disabled = (currentIpPage <= 1);
            document.getElementById('ip-next-btn').disabled = (currentIpPage >= totalPages);
        }

        function changeModelPage(delta) {
            currentModelPage += delta;
            renderModelTable();
        }

        function changeIpPage(delta) {
            currentIpPage += delta;
            renderIpTable();
        }

        async function fetchMetrics() {
            try {
                const res = await fetch('/metrics');
                const data = await res.json();
                
                document.getElementById('ip').innerText = data.verified_public_ip || 'Disconnected';
                
                if (data.location) {
                    document.getElementById('location').innerText = `${data.location.country || ''} ${data.location.city ? '(' + data.location.city + ')' : ''}`;
                    if (data.location.flag) {
                        document.getElementById('flag').innerText = data.location.flag;
                    }
                }

                // Cache IP History & Render Page
                cachedIpHistory = data.ip_history || [];
                renderIpTable();

                document.getElementById('rotations').innerText = data.total_rotations;
                document.getElementById('requests').innerText = data.metrics.total_requests;
                document.getElementById('ratelimits').innerText = data.metrics.rate_limited_requests;

                const models = data.discovered_models || [];
                document.getElementById('model-count-badge').innerText = `${models.length} Models`;

                cachedUsageData = data.model_usage || {};
                cachedModelKeys = Object.keys(cachedUsageData);

                let grandTotalUsd = 0;
                cachedModelKeys.forEach(m => { grandTotalUsd += (cachedUsageData[m].estimated_cost_usd || 0); });
                document.getElementById('total-saved').innerText = `$${grandTotalUsd.toFixed(4)}`;

                renderModelTable();
            } catch (e) {}
        }

        async function rotateNow() {
            const btn = document.getElementById('rotate-btn');
            const originalText = btn.innerHTML;
            btn.disabled = true;
            btn.innerHTML = `
                <svg class="animate-spin -ml-1 mr-1.5 h-3.5 w-3.5 text-white inline-block" xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24">
                    <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
                    <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
                </svg>
                Rotating...
            `;
            showToast("⏳ Requesting guaranteed fresh WARP IP...", "info");

            try {
                const res = await fetch('/api/rotate', { method: 'POST' });
                
                let attempts = 0;
                const pollInterval = setInterval(async () => {
                    attempts++;
                    await fetchMetrics();
                    if (attempts >= 6) {
                        clearInterval(pollInterval);
                        btn.disabled = false;
                        btn.innerHTML = originalText;
                        showToast(`✅ Verified IP: ${document.getElementById('ip').innerText}`, "success");
                    }
                }, 1000);

            } catch (e) {
                btn.disabled = false;
                btn.innerHTML = originalText;
                showToast("❌ Failed to rotate IP", "error");
            }
        }

        function showToast(message, type = "info") {
            let toast = document.getElementById('custom-toast');
            if (!toast) {
                toast = document.createElement('div');
                toast.id = 'custom-toast';
                toast.className = 'fixed bottom-5 right-5 z-50 px-4 py-2.5 rounded-lg shadow-md border text-xs font-medium font-sans transition-all transform duration-200';
                document.body.appendChild(toast);
            }
            
            if (type === "success") {
                toast.className = 'fixed bottom-5 right-5 z-50 px-4 py-2.5 rounded-lg shadow-md border text-xs font-medium font-sans transition-all transform duration-200 bg-slate-900 text-emerald-400 border-slate-800 opacity-100 translate-y-0';
            } else if (type === "error") {
                toast.className = 'fixed bottom-5 right-5 z-50 px-4 py-2.5 rounded-lg shadow-md border text-xs font-medium font-sans transition-all transform duration-200 bg-slate-900 text-rose-400 border-slate-800 opacity-100 translate-y-0';
            } else {
                toast.className = 'fixed bottom-5 right-5 z-50 px-4 py-2.5 rounded-lg shadow-md border text-xs font-medium font-sans transition-all transform duration-200 bg-slate-900 text-slate-200 border-slate-800 opacity-100 translate-y-0';
            }
            
            toast.innerText = message;
            setTimeout(() => {
                toast.classList.add('opacity-0', 'translate-y-1');
            }, 3500);
        }

        setInterval(fetchMetrics, 3000);
        fetchMetrics();
    </script>
</body>
</html>
"""

# -----------------------------------------------------------------------------
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

metrics = {
    "total_requests": 0,
    "successful_requests": 0,
    "rate_limited_requests": 0,
    "fallback_triggered": 0,
    "discovered_models_count": 0,
    "start_time": time.time()
}

DEFAULT_FREE_MODELS = [
    {"id": "deepseek-v4-flash-free", "name": "DeepSeek V4 Flash Free"},
    {"id": "mimo-v2.5-free", "name": "MiMo V2.5 Free"},
    {"id": "qwen3.6-plus-free", "name": "Qwen 3.6 Plus Free"},
    {"id": "minimax-m3-free", "name": "MiniMax M3 Free"},
    {"id": "nemotron-3-ultra-free", "name": "Nemotron 3 Ultra Free"},
]

discovered_models: List[Dict[str, str]] = DEFAULT_FREE_MODELS.copy()
_discovery_lock = threading.Lock()

logging.basicConfig(
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    level=logging.INFO,
    datefmt="%Y-%m-%d %H:%M:%S",
    force=True,
)
log = logging.getLogger("zen_server")

app = FastAPI(title="OpenCode Zen v3.0 Ultra Resilient Proxy")

def discover_models_task():
    global discovered_models
    while True:
        try:
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
                        if "free" in m_id.lower() or "zen" in m_id.lower():
                            new_models.append({"id": m_id, "name": m_id.replace("-", " ").title()})
                    
                    if new_models:
                        with _discovery_lock:
                            discovered_models = new_models
                            metrics["discovered_models_count"] = len(discovered_models)
                        log.info(f"Auto-Discovery refreshed: {len(discovered_models)} active model(s) fetched.")
        except Exception as e:
            log.debug(f"Auto-Discovery fallback active: {e}")
        time.sleep(300)

class FlowContext:
    def __enter__(self):
        global active_flows_count
        with flow_lock:
            active_flows_count += 1
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        global active_flows_count
        with flow_lock:
            active_flows_count = max(0, active_flows_count - 1)

class ChatMessage(BaseModel):
    role: str
    content: str

class ChatCompletionRequest(BaseModel):
    model: str = Field(default="deepseek-v4-flash-free")
    messages: List[ChatMessage]
    stream: Optional[bool] = False
    temperature: Optional[float] = 0.7
    max_tokens: Optional[int] = None

def get_realistic_headers() -> Dict[str, str]:
    return {
        "Content-Type": "application/json",
        "Authorization": "Bearer public",
        "Accept": "application/json, text/event-stream, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
        "Origin": "https://opencode.ai",
        "Referer": "https://opencode.ai/",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
    }

async def stream_response(response, model_name: str) -> AsyncGenerator[bytes, None]:
    """
    Raw byte passthrough SSE stream generator.
    CRITICAL: FlowContext is held for the ENTIRE duration of streaming so that
    rotate_warp() in rotator.py sees active_flows_count > 0 and skips IP rotation
    while a response is still being streamed to the client.
    """
    loop = asyncio.get_event_loop()
    global active_flows_count

    with flow_lock:
        active_flows_count += 1

    try:
        def get_next_chunk(iter_content):
            try:
                return next(iter_content)
            except StopIteration:
                return None

        chunk_iter = response.iter_content(chunk_size=4096)

        while True:
            chunk = await loop.run_in_executor(None, get_next_chunk, chunk_iter)
            if chunk is None or not chunk:
                break
            yield chunk

        yield b"\ndata: [DONE]\n\n"
    except GeneratorExit:
        pass
    except Exception as e:
        log.error(f"Stream exception caught: {e}")
        yield b"\ndata: [DONE]\n\n"
    finally:
        with flow_lock:
            active_flows_count = max(0, active_flows_count - 1)

@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard():
    return HTMLResponse(content=DASHBOARD_HTML)

@app.post("/api/rotate")
async def manual_rotate():
    """Triggers IP rotation via local rotator or microservice container."""
    try:
        rotate_warp(reason="Manual Web Dashboard Trigger")
        return {"status": "success"}
    except Exception:
        try:
            from curl_cffi import requests as cffi_requests
            resp = cffi_requests.post("http://warp-rotator:8001/rotate", timeout=5)
            if resp.status_code == 200:
                return resp.json()
        except Exception:
            pass
    return {"status": "triggered"}

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

    # Fetch live data from warp-rotator microservice (offloaded to executor — blocking call)
    rotator_ip = None
    rotator_location = None
    rotator_rotations = rotation_count
    rotator_history = []

    def fetch_rotator_status():
        try:
            from curl_cffi import requests as cffi_requests
            r = cffi_requests.get("http://warp-rotator:8001/status", impersonate="chrome124", timeout=4)
            if r.status_code == 200:
                return r.json()
        except Exception as e:
            log.debug(f"warp-rotator status fetch error: {e}")
        return None

    loop = asyncio.get_event_loop()
    rdata = await loop.run_in_executor(None, fetch_rotator_status)

    if rdata:
        rotator_ip = rdata.get("current_ip")
        rotator_rotations = rdata.get("rotations", rotation_count)
        rotator_history = rdata.get("history", [])
        if rotator_ip:
            def fetch_location():
                return get_ip_location(rotator_ip)
            rotator_location = await loop.run_in_executor(None, fetch_location)

    # Fallback: local IP lookup
    if not rotator_ip:
        def fetch_local_ip():
            ip = get_public_ip()
            loc = get_ip_location(ip) if ip else {"country": "Unknown", "flag": "🌐"}
            return ip, loc
        rotator_ip, rotator_location = await loop.run_in_executor(None, fetch_local_ip)

    # Fallback: SQLite history
    if not rotator_history:
        rotator_history = load_ip_history_from_db()

    if not rotator_location:
        rotator_location = {"country": "Unknown", "flag": "🌐"}

    return {
        "uptime_seconds": uptime,
        "verified_public_ip": rotator_ip,
        "location": rotator_location,
        "total_rotations": rotator_rotations,
        "metrics": metrics,
        "active_flows": active_flows_count,
        "discovered_models": discovered_models,
        "model_usage": model_usage_stats,
        "ip_history": rotator_history
    }

@app.get("/health")
async def health():
    return {
        "status": "healthy",
        "uptime_seconds": int(time.time() - metrics["start_time"]),
        "active_flows": active_flows_count,
        "total_rotations": rotation_count,
    }

@app.get("/v1/models")
async def list_models():
    with _discovery_lock:
        return {
            "object": "list",
            "data": [
                {
                    "id": m["id"],
                    "object": "model",
                    "created": 1700000000,
                    "owned_by": "opencode"
                }
                for m in discovered_models
            ]
        }

@app.post("/v1/chat/completions")
async def chat_completions(raw_request: Request):
    metrics["total_requests"] += 1
    
    try:
        payload = await raw_request.json()
    except Exception:
        payload = {}

    current_model = payload.get("model", "deepseek-v4-flash-free")
    is_stream = payload.get("stream", False)
    log.info(f"Received request for model '{current_model}' (Stream: {is_stream} | Has Tools: {'tools' in payload})")
    
    headers = get_realistic_headers()
    for k, v in raw_request.headers.items():
        if k.lower().startswith("x-opencode-"):
            headers[k] = v

    for attempt in range(1, MAX_RETRIES_ON_429 + 1):
        try:
            from curl_cffi import requests as cffi_requests
            import random

            time.sleep(random.uniform(0.1, 0.3))
            proxies = get_next_outbound_proxy()

            # FlowContext is NOT used here for streaming — the generator manages it internally
            # For non-streaming we use it to block rotation during the entire request
            response = cffi_requests.post(
                TARGET_ZEN_URL,
                json=payload,
                headers=headers,
                impersonate="chrome127",
                stream=is_stream,
                proxies=proxies,
                timeout=120
            )

            # Check HTTP status code for rate-limit or upstream error
            if response.status_code == 429 or response.status_code >= 500:
                metrics["rate_limited_requests"] += 1
                err_text = response.text.lower()

                # Distinguish Model/Provider-level limits vs. IP-level blocks
                is_model_specific_limit = any(k in err_text for k in ["model_rate_limit", "quota_exceeded", "per_model_limit", "credit_balance", "insufficient_quota"])

                delay = (INITIAL_BACKOFF * (2 ** (attempt - 1))) + random.uniform(0.5, 1.5)

                if is_model_specific_limit:
                    log.warning(f"Model-level limit for '{current_model}'. Skipping IP rotation to prevent socket teardown. Retrying in {delay:.2f}s...")
                    time.sleep(delay)
                    continue
                else:
                    log.warning(f"HTTP {response.status_code} (IP/Network block) for '{current_model}'. Triggering IP Rotation & Retrying in {delay:.2f}s...")
                    rotate_warp(reason=f"HTTP {response.status_code} on {current_model}")
                    time.sleep(delay)
                    continue

            metrics["successful_requests"] += 1

            if is_stream:
                track_token_usage(current_model, prompt_tokens=100, completion_tokens=150)
                # stream_response manages FlowContext internally via finally block
                return StreamingResponse(
                    stream_response(response, current_model),
                    media_type="text/event-stream",
                    headers={
                        "Cache-Control": "no-cache",
                        "X-Accel-Buffering": "no",
                        "Connection": "keep-alive",
                    }
                )
            else:
                with FlowContext():
                    try:
                        res_json = response.json()
                        usage = res_json.get("usage", {})
                        track_token_usage(
                            current_model,
                            prompt_tokens=usage.get("prompt_tokens", 50),
                            completion_tokens=usage.get("completion_tokens", 100)
                        )
                        return JSONResponse(content=res_json)
                    except Exception:
                        track_token_usage(current_model, prompt_tokens=50, completion_tokens=100)
                        return {
                            "id": f"chatcmpl-zen-resp-{int(time.time())}",
                            "object": "chat.completion",
                            "created": int(time.time()),
                            "model": current_model,
                            "choices": [
                                {
                                    "index": 0,
                                    "message": {"role": "assistant", "content": response.text},
                                    "finish_reason": "stop"
                                }
                            ]
                        }
                        
        except Exception as e:
            log.error(f"[Attempt {attempt}/{MAX_RETRIES_ON_429}] Connection error for model '{current_model}': {type(e).__name__}: {e}")
            if attempt < MAX_RETRIES_ON_429:
                time.sleep(min(2 ** attempt, 30))
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
    """Native Anthropic API proxy — forwards to /zen/v1/messages without format conversion."""
    metrics["total_requests"] += 1

    try:
        body = await raw_request.json()
    except Exception:
        body = {}

    model_name = body.get("model", "deepseek-v4-flash-free")
    is_stream = body.get("stream", False)
    log.info(f"Received Anthropic-format request for model '{model_name}' (Stream: {is_stream})")

    # Forward the client's x-api-key (used by Zen API for Anthropic auth)
    client_api_key = raw_request.headers.get("x-api-key") or ""
    if not client_api_key:
        auth = raw_request.headers.get("authorization", "")
        if auth.startswith("Bearer "):
            client_api_key = auth[7:]

    headers = get_realistic_headers()
    headers["x-api-key"] = client_api_key or "public"

    for k, v in raw_request.headers.items():
        kl = k.lower()
        if kl.startswith("x-opencode-") or kl.startswith("anthropic-"):
            headers[k] = v

    for attempt in range(1, MAX_RETRIES_ON_429 + 1):
        try:
            from curl_cffi import requests as cffi_requests
            import random

            time.sleep(random.uniform(0.1, 0.3))
            proxies = get_next_outbound_proxy()

            response = cffi_requests.post(
                TARGET_ZEN_ANTHROPIC_URL,
                json=body,
                headers=headers,
                impersonate="chrome127",
                stream=is_stream,
                proxies=proxies,
                timeout=120,
            )

            if response.status_code in (429, 500) or response.status_code >= 500:
                metrics["rate_limited_requests"] += 1
                delay = (INITIAL_BACKOFF * (2 ** (attempt - 1))) + random.uniform(0.5, 1.5)
                log.warning(f"HTTP {response.status_code} on '{model_name}'. Rotating IP & retrying in {delay:.2f}s...")
                rotate_warp(reason=f"HTTP {response.status_code} on {model_name}")
                time.sleep(delay)
                continue

            metrics["successful_requests"] += 1

            if is_stream:
                return StreamingResponse(
                    stream_response(response, model_name),
                    media_type="text/event-stream",
                    headers={
                        "Cache-Control": "no-cache",
                        "X-Accel-Buffering": "no",
                    },
                )
            else:
                with FlowContext():
                    try:
                        res_json = response.json()
                        usage = res_json.get("usage", {})
                        track_token_usage(
                            model_name,
                            prompt_tokens=usage.get("input_tokens", 50),
                            completion_tokens=usage.get("output_tokens", 100),
                        )
                        return JSONResponse(content=res_json)
                    except Exception:
                        track_token_usage(model_name, prompt_tokens=50, completion_tokens=100)
                        return JSONResponse(content=response.text)

        except Exception as e:
            log.error(f"[Attempt {attempt}/{MAX_RETRIES_ON_429}] Anthropic endpoint error for model '{model_name}': {type(e).__name__}: {e}")
            if attempt < MAX_RETRIES_ON_429:
                time.sleep(min(2 ** attempt, 30))
            continue

    log.error(f"All {MAX_RETRIES_ON_429} attempts exhausted for Anthropic model '{model_name}'. Returning 503.")
    return JSONResponse(
        status_code=503,
        content={"error": {"message": f"Upstream unavailable after {MAX_RETRIES_ON_429} attempts. Please retry.", "type": "upstream_error", "code": 503}},
        headers={"Retry-After": "10"}
    )

@app.post("/v1/responses")
async def responses_endpoint(raw_request: Request):
    """Proxy for OpenAI Responses API (/zen/v1/responses)."""
    metrics["total_requests"] += 1

    try:
        body = await raw_request.json()
    except Exception:
        body = {}

    model_name = body.get("model", "deepseek-v4-flash-free")
    is_stream = body.get("stream", False)
    log.info(f"Received Responses API request for model '{model_name}' (Stream: {is_stream})")

    headers = get_realistic_headers()
    for k, v in raw_request.headers.items():
        if k.lower().startswith("x-opencode-"):
            headers[k] = v

    for attempt in range(1, MAX_RETRIES_ON_429 + 1):
        try:
            from curl_cffi import requests as cffi_requests
            import random

            time.sleep(random.uniform(0.1, 0.3))
            proxies = get_next_outbound_proxy()

            response = cffi_requests.post(
                TARGET_ZEN_RESPONSES_URL,
                json=body,
                headers=headers,
                impersonate="chrome127",
                stream=is_stream,
                proxies=proxies,
                timeout=120,
            )

            if response.status_code in (429, 500) or response.status_code >= 500:
                metrics["rate_limited_requests"] += 1
                delay = (INITIAL_BACKOFF * (2 ** (attempt - 1))) + random.uniform(0.5, 1.5)
                log.warning(f"HTTP {response.status_code} on '{model_name}'. Rotating IP & retrying in {delay:.2f}s...")
                rotate_warp(reason=f"HTTP {response.status_code} on {model_name}")
                time.sleep(delay)
                continue

            metrics["successful_requests"] += 1

            if is_stream:
                return StreamingResponse(
                    stream_response(response, model_name),
                    media_type="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
                )
            else:
                with FlowContext():
                    try:
                        res_json = response.json()
                        return JSONResponse(content=res_json)
                    except Exception:
                        return JSONResponse(content=response.text)

        except Exception as e:
            log.error(f"[Attempt {attempt}/{MAX_RETRIES_ON_429}] Responses endpoint error for model '{model_name}': {type(e).__name__}: {e}")
            if attempt < MAX_RETRIES_ON_429:
                time.sleep(min(2 ** attempt, 30))
            continue

    log.error(f"All {MAX_RETRIES_ON_429} attempts exhausted for Responses model '{model_name}'. Returning 503.")
    return JSONResponse(
        status_code=503,
        content={"error": {"message": f"Upstream unavailable after {MAX_RETRIES_ON_429} attempts. Please retry.", "type": "upstream_error", "code": 503}},
        headers={"Retry-After": "10"}
    )

# Initialize SQLite database and load historical metrics
init_db()
model_usage_stats = load_metrics_from_db()

threading.Thread(target=discover_models_task, daemon=True).start()

_shutdown_requested = False

def _handle_signal(signum, frame):
    global _shutdown_requested
    if _shutdown_requested:
        return
    _shutdown_requested = True
    log.warning(f"Received signal {signum}, initiating graceful shutdown...")

signal.signal(signal.SIGTERM, _handle_signal)
signal.signal(signal.SIGINT, _handle_signal)

if __name__ == "__main__":
    load_proxy_list()
    log.info(f"Starting OpenCode IP Proxy Server on {HOST}:{PORT}...")
    uvicorn.run(app, host=HOST, port=PORT)
