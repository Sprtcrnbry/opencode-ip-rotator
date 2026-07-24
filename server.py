import asyncio
import json
import logging
import os
import sys
import threading
import time
from typing import AsyncGenerator, Dict, List, Optional
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import StreamingResponse, JSONResponse, HTMLResponse
from pydantic import BaseModel, Field
import uvicorn
from urllib.request import Request as UrlRequest, urlopen
from urllib.error import HTTPError, URLError

from rotator import rotate_warp, _flow_lock, _active_flows_count, get_public_ip, get_ip_location, rotation_count

import sqlite3
from pathlib import Path

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

MAX_RETRIES_ON_429 = 3
INITIAL_BACKOFF = 2

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
    format="%(asctime)s [Zen-Server] %(levelname)s: %(message)s",
    level=logging.INFO,
    datefmt="%Y-%m-%d %H:%M:%S",
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
        global _active_flows_count
        with _flow_lock:
            _active_flows_count += 1
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        global _active_flows_count
        with _flow_lock:
            _active_flows_count = max(0, _active_flows_count - 1)

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

async def stream_openai_response(response, model_name: str) -> AsyncGenerator[bytes, None]:
    loop = asyncio.get_event_loop()
    with FlowContext():
        try:
            # Helper to fetch next line synchronously in thread executor to prevent blocking asyncio loop
            def get_next_line(line_iter):
                try:
                    return next(line_iter)
                except StopIteration:
                    return None

            line_iter = response.iter_lines()
            
            while True:
                line = await loop.run_in_executor(None, get_next_line, line_iter)
                if line is None:
                    break
                
                line_str = line.decode("utf-8", errors="replace").strip()
                if not line_str:
                    continue
                
                # Direct passthrough for standard SSE lines
                if line_str.startswith("data:"):
                    yield f"{line_str}\n\n".encode("utf-8")
                elif line_str == "[DONE]":
                    yield b"data: [DONE]\n\n"
                else:
                    # Raw JSON / text line handling
                    try:
                        parsed = json.loads(line_str)
                        if isinstance(parsed, dict) and ("choices" in parsed or "id" in parsed or "delta" in parsed):
                            yield f"data: {line_str}\n\n".encode("utf-8")
                        else:
                            chunk = {
                                "id": f"chatcmpl-zen-stream-{int(time.time())}",
                                "object": "chat.completion.chunk",
                                "created": int(time.time()),
                                "model": model_name,
                                "choices": [{"index": 0, "delta": {"content": line_str}, "finish_reason": None}]
                            }
                            yield f"data: {json.dumps(chunk)}\n\n".encode("utf-8")
                    except Exception:
                        chunk = {
                            "id": f"chatcmpl-zen-stream-{int(time.time())}",
                            "object": "chat.completion.chunk",
                            "created": int(time.time()),
                            "model": model_name,
                            "choices": [{"index": 0, "delta": {"content": line_str}, "finish_reason": None}]
                        }
                        yield f"data: {json.dumps(chunk)}\n\n".encode("utf-8")

            yield b"data: [DONE]\n\n"
        except Exception as e:
            log.error(f"Stream exception caught: {e}")
            yield b"data: [DONE]\n\n"

@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard():
    return HTMLResponse(content=DASHBOARD_HTML)

@app.post("/api/rotate")
async def manual_rotate():
    """Triggers IP rotation across microservice containers."""
    try:
        from curl_cffi import requests as cffi_requests
        resp = cffi_requests.post("http://warp-rotator:8001/rotate", timeout=15)
        if resp.status_code == 200:
            return resp.json()
    except Exception as e:
        log.error(f"Microservice rotation trigger error: {e}")
        # Fallback to local
        try:
            rotate_warp(reason="Manual Web Dashboard Trigger")
            return {"status": "success"}
        except Exception:
            pass
    return {"status": "triggered"}

@app.get("/metrics")
async def get_metrics():
    uptime = int(time.time() - metrics["start_time"])
    ip = get_public_ip()
    location = get_ip_location(ip) if ip else {"country": "Unknown", "flag": "🌐"}
    
    # Load persistent IP history from SQLite Database
    history = load_ip_history_from_db()
    if not history:
        try:
            from curl_cffi import requests as cffi_requests
            r = cffi_requests.get("http://warp-rotator:8001/status", timeout=3)
            if r.status_code == 200:
                history = r.json().get("history", [])
        except Exception:
            pass

    return {
        "uptime_seconds": uptime,
        "verified_public_ip": ip,
        "location": location,
        "total_rotations": rotation_count,
        "metrics": metrics,
        "active_flows": _active_flows_count,
        "discovered_models": discovered_models,
        "model_usage": model_usage_stats,
        "ip_history": history
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
            with FlowContext():
                from curl_cffi import requests as cffi_requests
                import random
                
                time.sleep(random.uniform(0.1, 0.3))
                proxies = get_next_outbound_proxy()

                response = cffi_requests.post(
                    TARGET_ZEN_URL,
                    json=payload,
                    headers=headers,
                    impersonate="chrome124",
                    stream=is_stream,
                    proxies=proxies,
                    timeout=120
                )
                metrics["successful_requests"] += 1
                
                if is_stream:
                    track_token_usage(current_model, prompt_tokens=100, completion_tokens=150)
                    return StreamingResponse(
                        stream_openai_response(response, current_model),
                        media_type="text/event-stream"
                    )
                else:
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
            error_msg = str(e)
            if "429" in error_msg or (hasattr(e, "response") and getattr(e.response, "status_code", 0) == 429):
                metrics["rate_limited_requests"] += 1
                import random
                delay = (INITIAL_BACKOFF * (2 ** (attempt - 1))) + random.uniform(0.5, 1.5)
                log.warning(f"HTTP 429 encountered for '{current_model}' (Attempt {attempt}/{MAX_RETRIES_ON_429}). Triggering IP Rotation & Retrying in {delay:.2f}s...")
                
                rotate_warp(reason=f"HTTP 429 on {current_model}")
                time.sleep(delay)
                continue
            else:
                log.error(f"Target Connection Error: {e}")
                time.sleep(1)
                continue

    raise HTTPException(status_code=429, detail="Rate limit persisted after multiple IP rotations.")

# Initialize SQLite database and load historical metrics
init_db()
model_usage_stats = load_metrics_from_db()

threading.Thread(target=discover_models_task, daemon=True).start()

if __name__ == "__main__":
    init_db()
    load_proxy_list()
    log.info(f"Starting OpenCode IP Proxy Server on {HOST}:{PORT}...")
    uvicorn.run(app, host=HOST, port=PORT)
