#!/usr/bin/env python3
"""
Solana Wallet Sniper — Token Launch Detector
Monitors wallet FML7JiQijDKfSSXMgmKyN2E5y3Exv7vbXSc6QCh3jWJu for new token launches.
Run: python monitor.py
Dashboard: http://localhost:8080
"""

import asyncio
import json
import time
import sys
from datetime import datetime, timezone
from typing import Optional

import aiohttp
import websockets
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, StreamingResponse
import uvicorn

# ── Config ─────────────────────────────────────────────────────────────────────
TARGET_WALLET = "FML7JiQijDKfSSXMgmKyN2E5y3Exv7vbXSc6QCh3jWJu"
SOLANA_RPC = "https://api.mainnet-beta.solana.com"
SOLANA_WS = "wss://api.mainnet-beta.solana.com"
WEB_PORT = 8080
POLL_INTERVAL = 5  # seconds

# Known program IDs
PUMP_FUN = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
SPL_TOKEN = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022 = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
RAYDIUM_V4 = "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8"
RAYDIUM_CPMM = "CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C"
METAPLEX_META = "metaqbxxUerdq28cj1RbAWkYQm3ybzjb6a8bt518x1s"

TOKEN_PROGRAMS = {SPL_TOKEN, TOKEN_2022}
LAUNCH_PROGRAMS = {PUMP_FUN, RAYDIUM_V4, RAYDIUM_CPMM}

# ── State ──────────────────────────────────────────────────────────────────────
events: list[dict] = []
sse_clients: list[asyncio.Queue] = []
connection_status = {"ws": "disconnected", "last_poll": None}
seen_signatures: set[str] = set()

# ── Helpers ────────────────────────────────────────────────────────────────────

def log(msg: str, color: str = "white"):
    colors = {"green": "\033[92m", "yellow": "\033[93m", "red": "\033[91m", "cyan": "\033[96m", "white": "\033[0m"}
    c = colors.get(color, "")
    reset = "\033[0m"
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"{c}[{ts}] {msg}{reset}")


def classify_transaction(tx_data: dict) -> dict:
    """Analyze a transaction to determine if it's a token launch."""
    result = {
        "is_token_launch": False,
        "is_pump_fun": False,
        "mint_address": None,
        "programs": [],
        "type": "unknown",
    }

    if not tx_data or "result" not in tx_data:
        return result

    tx = tx_data["result"]
    if tx is None:
        return result

    meta = tx.get("meta", {})
    if meta is None:
        return result

    message = tx.get("transaction", {}).get("message", {})
    account_keys = message.get("accountKeys", [])
    # Handle both string keys and object keys (with pubkey field)
    accounts = []
    for k in account_keys:
        if isinstance(k, str):
            accounts.append(k)
        elif isinstance(k, dict):
            accounts.append(k.get("pubkey", ""))

    instructions = message.get("instructions", [])
    inner_instructions = meta.get("innerInstructions", []) or []
    log_messages = meta.get("logMessages", []) or []

    # Collect all program IDs involved
    program_ids = set()
    for ix in instructions:
        pid_idx = ix.get("programIdIndex")
        if pid_idx is not None and pid_idx < len(accounts):
            program_ids.add(accounts[pid_idx])

    for inner in inner_instructions:
        for ix in inner.get("instructions", []):
            pid_idx = ix.get("programIdIndex")
            if pid_idx is not None and pid_idx < len(accounts):
                program_ids.add(accounts[pid_idx])

    result["programs"] = list(program_ids)

    # Check for pump.fun interaction
    if PUMP_FUN in program_ids:
        result["is_pump_fun"] = True
        result["is_token_launch"] = True
        result["type"] = "pump.fun launch"
        # Try to find mint address from pump.fun logs
        for log_msg in log_messages:
            if "MintTo" in log_msg or "InitializeMint" in log_msg:
                result["is_token_launch"] = True
        # The mint is typically a new account in the transaction
        # Look for token program interactions to find the mint
        _find_mint_from_logs(log_messages, accounts, meta, result)

    # Check for SPL Token mint creation
    elif TOKEN_PROGRAMS & program_ids:
        for log_msg in log_messages:
            if "InitializeMint" in log_msg or "initializeMint" in log_msg:
                result["is_token_launch"] = True
                result["type"] = "SPL token creation"
                _find_mint_from_logs(log_messages, accounts, meta, result)
                break

    # Check for Raydium pool creation
    if (RAYDIUM_V4 in program_ids or RAYDIUM_CPMM in program_ids):
        result["type"] = "Raydium pool creation"
        result["is_token_launch"] = True
        _find_mint_from_logs(log_messages, accounts, meta, result)

    # Fallback: check log messages for any token creation keywords
    if not result["is_token_launch"]:
        for log_msg in log_messages:
            lower = log_msg.lower()
            if any(kw in lower for kw in ["create", "initialize", "mint", "launch"]):
                if TOKEN_PROGRAMS & program_ids or LAUNCH_PROGRAMS & program_ids:
                    result["is_token_launch"] = True
                    result["type"] = "possible token launch"
                    _find_mint_from_logs(log_messages, accounts, meta, result)
                    break

    # If pump.fun and we still don't have a mint, use heuristic:
    # new accounts that aren't the wallet or known programs
    if result["is_token_launch"] and not result["mint_address"]:
        post_token_balances = meta.get("postTokenBalances", []) or []
        for bal in post_token_balances:
            mint = bal.get("mint", "")
            if mint and mint != TARGET_WALLET:
                result["mint_address"] = mint
                break

    # Extra fallback: check pre/post token balance changes for new mints
    if result["is_token_launch"] and not result["mint_address"]:
        pre_mints = {b.get("mint") for b in (meta.get("preTokenBalances") or [])}
        post_mints = {b.get("mint") for b in (meta.get("postTokenBalances") or [])}
        new_mints = post_mints - pre_mints
        for m in new_mints:
            if m and m != TARGET_WALLET:
                result["mint_address"] = m
                break

    return result


def _find_mint_from_logs(log_messages, accounts, meta, result):
    """Try to extract mint address from various sources."""
    # Check postTokenBalances for mint addresses
    post_balances = meta.get("postTokenBalances", []) or []
    for bal in post_balances:
        mint = bal.get("mint", "")
        owner = bal.get("owner", "")
        if mint and mint != TARGET_WALLET:
            result["mint_address"] = mint
            return

    # Check for new accounts (potential mints)
    pre_balances = meta.get("preTokenBalances") or []
    pre_mints = {b.get("mint") for b in pre_balances}
    for bal in post_balances:
        mint = bal.get("mint")
        if mint and mint not in pre_mints and mint != TARGET_WALLET:
            result["mint_address"] = mint
            return


async def fetch_transaction(session: aiohttp.ClientSession, signature: str) -> Optional[dict]:
    """Fetch full transaction details via RPC."""
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getTransaction",
        "params": [
            signature,
            {"encoding": "json", "maxSupportedTransactionVersion": 0}
        ]
    }
    try:
        async with session.post(SOLANA_RPC, json=payload) as resp:
            return await resp.json()
    except Exception as e:
        log(f"Error fetching tx {signature[:16]}...: {e}", "red")
        return None


async def get_recent_signatures(session: aiohttp.ClientSession, limit: int = 5) -> list:
    """Poll for recent signatures from the target wallet."""
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getSignaturesForAddress",
        "params": [
            TARGET_WALLET,
            {"limit": limit}
        ]
    }
    try:
        async with session.post(SOLANA_RPC, json=payload) as resp:
            data = await resp.json()
            return data.get("result", [])
    except Exception as e:
        log(f"Error polling signatures: {e}", "red")
        return []


def broadcast_event(event: dict):
    """Send event to all SSE clients and store in history."""
    events.append(event)
    if len(events) > 200:
        events.pop(0)
    data = json.dumps(event)
    for q in sse_clients:
        try:
            q.put_nowait(data)
        except asyncio.QueueFull:
            pass


async def process_signature(session: aiohttp.ClientSession, sig: str):
    """Process a single transaction signature."""
    if sig in seen_signatures:
        return
    seen_signatures.add(sig)
    # Cap seen set size
    if len(seen_signatures) > 5000:
        to_remove = list(seen_signatures)[:2500]
        for s in to_remove:
            seen_signatures.discard(s)

    tx_data = await fetch_transaction(session, sig)
    info = classify_transaction(tx_data)
    ts = datetime.now(timezone.utc).isoformat()

    event = {
        "signature": sig,
        "timestamp": ts,
        "is_token_launch": info["is_token_launch"],
        "type": info["type"],
        "mint_address": info["mint_address"],
        "programs": info["programs"],
        "is_pump_fun": info["is_pump_fun"],
    }

    if info["is_token_launch"]:
        mint = info["mint_address"] or "UNKNOWN"
        log(f"🚀 TOKEN LAUNCH DETECTED! Type: {info['type']}", "green")
        log(f"   Mint: {mint}", "green")
        if mint != "UNKNOWN":
            log(f"   Buy on Padre.gg: https://trade.padre.gg/{mint}", "green")
            log(f"   Solscan: https://solscan.io/token/{mint}", "green")
        print("\a")  # System bell
    else:
        log(f"Transaction: {sig[:24]}... (programs: {', '.join(p[:8] for p in info['programs'][:3])})", "yellow")

    broadcast_event(event)


# ── WebSocket Monitor ──────────────────────────────────────────────────────────

async def websocket_monitor():
    """Subscribe to wallet logs via Solana WebSocket."""
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                log("Connecting to Solana WebSocket...", "cyan")
                async with websockets.connect(SOLANA_WS, ping_interval=20, ping_timeout=60) as ws:
                    # Subscribe to logs mentioning our wallet
                    sub_msg = json.dumps({
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "logsSubscribe",
                        "params": [
                            {"mentions": [TARGET_WALLET]},
                            {"commitment": "confirmed"}
                        ]
                    })
                    await ws.send(sub_msg)
                    resp = await ws.recv()
                    sub_data = json.loads(resp)
                    log(f"WebSocket subscribed (id: {sub_data.get('result', '?')})", "green")
                    connection_status["ws"] = "connected"
                    broadcast_event({"type": "status", "ws": "connected", "timestamp": datetime.now(timezone.utc).isoformat()})

                    async for message in ws:
                        try:
                            data = json.loads(message)
                            if "params" not in data:
                                continue
                            value = data["params"]["result"]["value"]
                            sig = value.get("signature", "")
                            logs = value.get("logs", [])
                            err = value.get("err")

                            if err:
                                continue  # Skip failed transactions

                            if sig:
                                await process_signature(session, sig)
                        except (KeyError, TypeError) as e:
                            pass

            except (websockets.exceptions.ConnectionClosed, ConnectionError, OSError) as e:
                connection_status["ws"] = "disconnected"
                log(f"WebSocket disconnected: {e}. Reconnecting in 3s...", "red")
                broadcast_event({"type": "status", "ws": "disconnected", "timestamp": datetime.now(timezone.utc).isoformat()})
                await asyncio.sleep(3)
            except Exception as e:
                connection_status["ws"] = "error"
                log(f"WebSocket error: {e}. Reconnecting in 5s...", "red")
                await asyncio.sleep(5)


# ── Polling Fallback ───────────────────────────────────────────────────────────

async def polling_monitor():
    """Fallback: poll for recent transactions every POLL_INTERVAL seconds."""
    await asyncio.sleep(3)  # Let WebSocket connect first
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                sigs = await get_recent_signatures(session, limit=5)
                connection_status["last_poll"] = datetime.now(timezone.utc).isoformat()
                for sig_info in sigs:
                    sig = sig_info.get("signature", "")
                    if sig and sig not in seen_signatures:
                        await process_signature(session, sig)
            except Exception as e:
                log(f"Polling error: {e}", "red")
            await asyncio.sleep(POLL_INTERVAL)


# ── FastAPI Web Dashboard ─────────────────────────────────────────────────────

app = FastAPI()

HTML_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>BIAO Sniper</title>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body {
    background: #0a0a0f;
    color: #e0e0e0;
    font-family: 'JetBrains Mono', 'Fira Code', 'Consolas', monospace;
    min-height: 100vh;
  }
  .header {
    background: linear-gradient(135deg, #1a1a2e 0%, #16213e 100%);
    border-bottom: 2px solid #00ff88;
    padding: 20px 30px;
    display: flex;
    align-items: center;
    justify-content: space-between;
  }
  .header h1 {
    font-size: 24px;
    color: #00ff88;
    text-shadow: 0 0 20px rgba(0,255,136,0.3);
  }
  .header .wallet {
    font-size: 12px;
    color: #888;
    margin-top: 4px;
  }
  .status-badge {
    padding: 6px 16px;
    border-radius: 20px;
    font-size: 13px;
    font-weight: bold;
  }
  .status-connected { background: #00ff8833; color: #00ff88; border: 1px solid #00ff88; }
  .status-disconnected { background: #ff444433; color: #ff4444; border: 1px solid #ff4444; }
  .status-connecting { background: #ffaa0033; color: #ffaa00; border: 1px solid #ffaa00; }

  .main { padding: 20px 30px; max-width: 1200px; margin: 0 auto; }

  .alert-banner {
    display: none;
    background: linear-gradient(135deg, #00ff8822, #00cc6622);
    border: 2px solid #00ff88;
    border-radius: 12px;
    padding: 24px;
    margin-bottom: 24px;
    text-align: center;
    animation: pulse 1s ease-in-out infinite alternate;
  }
  .alert-banner.active { display: block; }
  @keyframes pulse {
    from { box-shadow: 0 0 20px rgba(0,255,136,0.2); }
    to { box-shadow: 0 0 40px rgba(0,255,136,0.5); }
  }
  .alert-banner h2 { color: #00ff88; font-size: 28px; margin-bottom: 12px; }
  .alert-banner .mint { color: #fff; font-size: 14px; margin-bottom: 16px; word-break: break-all; }

  .buy-btn {
    display: inline-block;
    background: linear-gradient(135deg, #00ff88, #00cc66);
    color: #000;
    font-size: 22px;
    font-weight: bold;
    padding: 16px 48px;
    border-radius: 12px;
    text-decoration: none;
    margin: 8px;
    transition: transform 0.15s, box-shadow 0.15s;
    box-shadow: 0 0 30px rgba(0,255,136,0.3);
  }
  .buy-btn:hover {
    transform: scale(1.05);
    box-shadow: 0 0 50px rgba(0,255,136,0.5);
  }

  .secondary-links {
    margin-top: 12px;
    display: flex;
    gap: 12px;
    justify-content: center;
    flex-wrap: wrap;
  }
  .secondary-links a {
    color: #aaa;
    text-decoration: none;
    padding: 6px 14px;
    border: 1px solid #333;
    border-radius: 8px;
    font-size: 12px;
    transition: all 0.15s;
  }
  .secondary-links a:hover { color: #fff; border-color: #666; }

  .stats {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
    gap: 16px;
    margin-bottom: 24px;
  }
  .stat-card {
    background: #12121a;
    border: 1px solid #222;
    border-radius: 10px;
    padding: 16px;
  }
  .stat-card .label { color: #666; font-size: 11px; text-transform: uppercase; letter-spacing: 1px; }
  .stat-card .value { color: #fff; font-size: 22px; margin-top: 4px; }

  .events-section h3 {
    color: #888;
    font-size: 14px;
    text-transform: uppercase;
    letter-spacing: 1px;
    margin-bottom: 12px;
  }

  .event-card {
    background: #12121a;
    border: 1px solid #222;
    border-radius: 8px;
    padding: 14px 18px;
    margin-bottom: 8px;
    display: flex;
    align-items: center;
    gap: 14px;
    transition: border-color 0.2s;
  }
  .event-card:hover { border-color: #444; }
  .event-card.launch {
    border-color: #00ff88;
    background: linear-gradient(135deg, #00ff8808, #12121a);
  }
  .event-dot {
    width: 10px;
    height: 10px;
    border-radius: 50%;
    flex-shrink: 0;
  }
  .event-dot.launch { background: #00ff88; box-shadow: 0 0 8px #00ff88; }
  .event-dot.tx { background: #555; }
  .event-info { flex: 1; min-width: 0; }
  .event-type { font-size: 13px; font-weight: bold; }
  .event-sig { font-size: 11px; color: #555; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .event-time { font-size: 11px; color: #444; flex-shrink: 0; }
  .event-card.launch .event-type { color: #00ff88; }
  .event-links { display: flex; gap: 6px; flex-shrink: 0; }
  .event-links a { color: #666; font-size: 11px; text-decoration: none; }
  .event-links a:hover { color: #00ff88; }

  .empty-state {
    text-align: center;
    padding: 60px 20px;
    color: #444;
  }
  .empty-state .icon { font-size: 48px; margin-bottom: 16px; }
  .empty-state p { font-size: 14px; }

  @keyframes fadeIn {
    from { opacity: 0; transform: translateY(-8px); }
    to { opacity: 1; transform: translateY(0); }
  }
  .event-card { animation: fadeIn 0.3s ease; }
</style>
</head>
<body>

<div class="header">
  <div>
    <h1>BIAO SNIPER</h1>
    <div class="wallet">Watching: """ + TARGET_WALLET + """</div>
  </div>
  <div id="statusBadge" class="status-badge status-connecting">Connecting...</div>
</div>

<div class="main">
  <div id="alertBanner" class="alert-banner">
    <h2>TOKEN LAUNCH DETECTED</h2>
    <div class="mint" id="alertMint"></div>
    <a id="buyBtn" class="buy-btn" href="#" target="_blank">BUY ON PADRE.GG</a>
    <div class="secondary-links" id="alertLinks"></div>
  </div>

  <div class="stats">
    <div class="stat-card">
      <div class="label">Transactions Seen</div>
      <div class="value" id="txCount">0</div>
    </div>
    <div class="stat-card">
      <div class="label">Token Launches</div>
      <div class="value" id="launchCount">0</div>
    </div>
    <div class="stat-card">
      <div class="label">Monitoring Since</div>
      <div class="value" id="startTime">-</div>
    </div>
    <div class="stat-card">
      <div class="label">Last Activity</div>
      <div class="value" id="lastActivity">-</div>
    </div>
  </div>

  <div class="events-section">
    <h3>Live Activity Feed</h3>
    <div id="eventsList">
      <div class="empty-state">
        <div class="icon">📡</div>
        <p>Waiting for transactions from target wallet...</p>
      </div>
    </div>
  </div>
</div>

<script>
const startTime = new Date();
document.getElementById('startTime').textContent = startTime.toLocaleTimeString();

let txCount = 0;
let launchCount = 0;
let latestLaunchMint = null;

// Request notification permission
if ('Notification' in window && Notification.permission === 'default') {
  Notification.requestPermission();
}

// Audio context for alert beep
function playAlertSound() {
  try {
    const ctx = new (window.AudioContext || window.webkitAudioContext)();
    // Play 3 urgent beeps
    [0, 0.2, 0.4].forEach(delay => {
      const osc = ctx.createOscillator();
      const gain = ctx.createGain();
      osc.connect(gain);
      gain.connect(ctx.destination);
      osc.frequency.value = 1200;
      osc.type = 'square';
      gain.gain.value = 0.3;
      osc.start(ctx.currentTime + delay);
      osc.stop(ctx.currentTime + delay + 0.15);
    });
  } catch(e) {}
}

function updateStatus(status) {
  const badge = document.getElementById('statusBadge');
  if (status === 'connected') {
    badge.className = 'status-badge status-connected';
    badge.textContent = 'Connected';
  } else if (status === 'disconnected') {
    badge.className = 'status-badge status-disconnected';
    badge.textContent = 'Disconnected';
  } else {
    badge.className = 'status-badge status-connecting';
    badge.textContent = 'Connecting...';
  }
}

function showLaunchAlert(mint) {
  if (!mint || mint === 'UNKNOWN') return;
  latestLaunchMint = mint;
  const banner = document.getElementById('alertBanner');
  banner.classList.add('active');
  document.getElementById('alertMint').textContent = mint;
  document.getElementById('buyBtn').href = 'https://trade.padre.gg/' + mint;
  document.getElementById('alertLinks').innerHTML =
    '<a href="https://solscan.io/token/' + mint + '" target="_blank">Solscan</a>' +
    '<a href="https://birdeye.so/token/' + mint + '?chain=solana" target="_blank">Birdeye</a>' +
    '<a href="https://pump.fun/' + mint + '" target="_blank">Pump.fun</a>' +
    '<a href="https://jup.ag/swap/SOL-' + mint + '" target="_blank">Jupiter</a>';
}

function addEvent(ev) {
  const list = document.getElementById('eventsList');
  // Remove empty state
  const empty = list.querySelector('.empty-state');
  if (empty) empty.remove();

  const isLaunch = ev.is_token_launch;
  const card = document.createElement('div');
  card.className = 'event-card' + (isLaunch ? ' launch' : '');

  const time = new Date(ev.timestamp).toLocaleTimeString();
  const sigShort = ev.signature ? ev.signature.substring(0, 32) + '...' : '';
  const mint = ev.mint_address || '';

  let linksHtml = '';
  if (isLaunch && mint) {
    linksHtml = '<div class="event-links">' +
      '<a href="https://trade.padre.gg/' + mint + '" target="_blank">[Padre]</a> ' +
      '<a href="https://solscan.io/token/' + mint + '" target="_blank">[Solscan]</a>' +
      '</div>';
  } else if (ev.signature) {
    linksHtml = '<div class="event-links">' +
      '<a href="https://solscan.io/tx/' + ev.signature + '" target="_blank">[tx]</a>' +
      '</div>';
  }

  card.innerHTML =
    '<div class="event-dot ' + (isLaunch ? 'launch' : 'tx') + '"></div>' +
    '<div class="event-info">' +
      '<div class="event-type">' + (isLaunch ? 'TOKEN LAUNCH — ' + (ev.type || '') : 'Transaction') + '</div>' +
      '<div class="event-sig">' + (isLaunch && mint ? 'Mint: ' + mint : sigShort) + '</div>' +
    '</div>' +
    linksHtml +
    '<div class="event-time">' + time + '</div>';

  list.insertBefore(card, list.firstChild);

  // Update counters
  txCount++;
  document.getElementById('txCount').textContent = txCount;
  document.getElementById('lastActivity').textContent = time;

  if (isLaunch) {
    launchCount++;
    document.getElementById('launchCount').textContent = launchCount;
    showLaunchAlert(mint);
    playAlertSound();

    // Browser notification
    if ('Notification' in window && Notification.permission === 'granted') {
      new Notification('TOKEN LAUNCH DETECTED!', {
        body: 'Mint: ' + (mint || 'Unknown') + '\\nType: ' + (ev.type || 'unknown'),
        icon: '🚀',
        requireInteraction: true
      });
    }
  }
}

// SSE connection
function connectSSE() {
  const evtSource = new EventSource('/events');
  evtSource.onmessage = function(e) {
    try {
      const data = JSON.parse(e.data);
      if (data.type === 'status') {
        updateStatus(data.ws);
      } else if (data.signature) {
        addEvent(data);
      }
    } catch(err) {}
  };
  evtSource.onerror = function() {
    updateStatus('disconnected');
    evtSource.close();
    setTimeout(connectSSE, 3000);
  };
  evtSource.onopen = function() {
    updateStatus('connected');
  };
}

connectSSE();
</script>
</body>
</html>"""


@app.get("/", response_class=HTMLResponse)
async def index():
    return HTML_PAGE


@app.get("/events")
async def sse(request: Request):
    """Server-Sent Events endpoint for live updates."""
    queue: asyncio.Queue = asyncio.Queue(maxsize=100)
    sse_clients.append(queue)

    async def event_generator():
        try:
            # Send current status
            yield f"data: {json.dumps({'type': 'status', 'ws': connection_status['ws']})}\n\n"
            # Send recent history
            for ev in events[-20:]:
                yield f"data: {json.dumps(ev)}\n\n"
            # Stream new events
            while True:
                if await request.is_disconnected():
                    break
                try:
                    data = await asyncio.wait_for(queue.get(), timeout=15.0)
                    yield f"data: {data}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            sse_clients.remove(queue)

    return StreamingResponse(event_generator(), media_type="text/event-stream")


@app.get("/status")
async def status():
    return {
        "wallet": TARGET_WALLET,
        "ws_status": connection_status["ws"],
        "last_poll": connection_status["last_poll"],
        "events_count": len(events),
        "launches": sum(1 for e in events if e.get("is_token_launch")),
    }


# ── Main ───────────────────────────────────────────────────────────────────────

async def start_monitors():
    """Start WebSocket and polling monitors as background tasks."""
    await asyncio.sleep(0.5)
    log(f"Monitoring wallet: {TARGET_WALLET}", "cyan")
    log(f"Expected token: BIAO", "cyan")
    log(f"Dashboard: http://localhost:{WEB_PORT}", "cyan")
    log("─" * 60, "cyan")
    asyncio.create_task(websocket_monitor())
    asyncio.create_task(polling_monitor())


@app.on_event("startup")
async def on_startup():
    asyncio.create_task(start_monitors())


if __name__ == "__main__":
    print("\033[96m")
    print("╔══════════════════════════════════════════════╗")
    print("║        BIAO SNIPER — Token Launch Detector   ║")
    print("╚══════════════════════════════════════════════╝")
    print(f"\033[0m")
    uvicorn.run(app, host="0.0.0.0", port=WEB_PORT, log_level="warning")
