#!/usr/bin/env python3
"""
BIAO Sniper — Phone Notification Edition
Polls a Solana wallet for activity and sends push notifications via ntfy.sh

Setup:
  1. Install ntfy app on phone (Android/iOS)
  2. Subscribe to topic: biao-sniper-novakk
  3. Run: python notify.py
"""

import requests
import time
import json
from datetime import datetime

# ── Config ─────────────────────────────────────────────────────────────────────
TARGET_WALLET = "FML7JiQijDKfSSXMgmKyN2E5y3Exv7vbXSc6QCh3jWJu"
HELIUS_KEY = "4a00508b-5fa5-49b1-8fb7-c988a7b16917"
SOLANA_RPC = f"https://mainnet.helius-rpc.com/?api-key={HELIUS_KEY}"
NTFY_TOPIC = "biao-sniper-novakk"
NTFY_URL = f"https://ntfy.sh/{NTFY_TOPIC}"
POLL_INTERVAL = 3  # seconds
PADRE_BASE = "https://trade.padre.gg/trade/solana/"

# Known programs
PUMP_FUN = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
SPL_TOKEN = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022 = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
RAYDIUM_V4 = "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8"
RAYDIUM_CPMM = "CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C"

TOKEN_PROGRAMS = {SPL_TOKEN, TOKEN_2022}
LAUNCH_PROGRAMS = {PUMP_FUN, RAYDIUM_V4, RAYDIUM_CPMM}

seen_signatures = set()


def log(msg, color="white"):
    colors = {"green": "\033[92m", "yellow": "\033[93m", "red": "\033[91m", "cyan": "\033[96m", "white": "\033[0m"}
    c = colors.get(color, "")
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"{c}[{ts}] {msg}\033[0m")


def rpc_call(method, params):
    try:
        resp = requests.post(SOLANA_RPC, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, timeout=10)
        return resp.json().get("result")
    except Exception as e:
        log(f"RPC error: {e}", "red")
        return None


def send_notification(title, message, priority="default", tags="", click_url=None):
    headers = {"Title": title, "Priority": priority, "Tags": tags}
    if click_url:
        headers["Click"] = click_url
        headers["Actions"] = f"view, Buy on Padre.gg, {click_url}"
    try:
        requests.post(NTFY_URL, data=message.encode("utf-8"), headers=headers, timeout=5)
        log(f"Phone notification sent: {title}", "green")
    except Exception as e:
        log(f"Notification error: {e}", "red")


def classify_transaction(sig):
    tx = rpc_call("getTransaction", [sig, {"encoding": "json", "maxSupportedTransactionVersion": 0}])
    if not tx:
        return {"is_launch": False, "type": "unknown", "mint": None}

    meta = tx.get("meta") or {}
    message = tx.get("transaction", {}).get("message", {})
    account_keys = message.get("accountKeys", [])
    accounts = [k if isinstance(k, str) else k.get("pubkey", "") for k in account_keys]
    log_messages = meta.get("logMessages") or []

    # Collect program IDs
    program_ids = set()
    for ix in message.get("instructions", []):
        idx = ix.get("programIdIndex")
        if idx is not None and idx < len(accounts):
            program_ids.add(accounts[idx])
    for inner in (meta.get("innerInstructions") or []):
        for ix in inner.get("instructions", []):
            idx = ix.get("programIdIndex")
            if idx is not None and idx < len(accounts):
                program_ids.add(accounts[idx])

    result = {"is_launch": False, "type": "transaction", "mint": None, "programs": program_ids}

    # Pump.fun
    if PUMP_FUN in program_ids:
        result["is_launch"] = True
        result["type"] = "pump.fun launch"

    # SPL Token InitializeMint
    if program_ids & TOKEN_PROGRAMS:
        for msg in log_messages:
            if "InitializeMint" in msg or "initializeMint" in msg:
                result["is_launch"] = True
                result["type"] = "SPL token creation"
                break

    # Raydium
    if RAYDIUM_V4 in program_ids or RAYDIUM_CPMM in program_ids:
        result["is_launch"] = True
        result["type"] = "Raydium pool creation"

    # Find mint address
    if result["is_launch"]:
        pre_mints = {b.get("mint") for b in (meta.get("preTokenBalances") or [])}
        for bal in (meta.get("postTokenBalances") or []):
            mint = bal.get("mint", "")
            if mint and mint != TARGET_WALLET and mint not in pre_mints:
                result["mint"] = mint
                break
        if not result["mint"]:
            for bal in (meta.get("postTokenBalances") or []):
                mint = bal.get("mint", "")
                if mint and mint != TARGET_WALLET:
                    result["mint"] = mint
                    break

    return result


def main():
    print("\033[96m")
    print("╔══════════════════════════════════════════════╗")
    print("║   BIAO SNIPER — Phone Notification Monitor   ║")
    print("╚══════════════════════════════════════════════╝")
    print(f"\033[0m")
    log(f"Wallet: {TARGET_WALLET}", "cyan")
    log(f"Notifications: {NTFY_URL}", "cyan")
    log(f"Polling every {POLL_INTERVAL}s", "cyan")
    log("─" * 55, "cyan")

    # Send test notification
    send_notification(
        "BIAO Sniper Active",
        f"Monitoring wallet {TARGET_WALLET[:8]}...{TARGET_WALLET[-4:]}\nYou'll get notified on any activity.",
        priority="low",
        tags="eyes"
    )

    # Seed seen signatures so we don't alert on old txns
    log("Loading recent transactions...", "yellow")
    sigs = rpc_call("getSignaturesForAddress", [TARGET_WALLET, {"limit": 20}])
    if sigs:
        for s in sigs:
            seen_signatures.add(s["signature"])
        log(f"Loaded {len(sigs)} existing transactions (won't re-alert)", "yellow")

    log("Monitoring started!", "green")

    while True:
        try:
            sigs = rpc_call("getSignaturesForAddress", [TARGET_WALLET, {"limit": 5}])
            if not sigs:
                time.sleep(POLL_INTERVAL)
                continue

            for sig_info in sigs:
                sig = sig_info["signature"]
                if sig in seen_signatures:
                    continue
                seen_signatures.add(sig)

                # Cap set size
                if len(seen_signatures) > 5000:
                    to_remove = list(seen_signatures)[:2500]
                    for s in to_remove:
                        seen_signatures.discard(s)

                info = classify_transaction(sig)

                if info["is_launch"]:
                    mint = info["mint"] or "UNKNOWN"
                    log(f"🚀 TOKEN LAUNCH: {info['type']} — Mint: {mint}", "green")
                    print("\a")

                    padre_url = f"{PADRE_BASE}{mint}?rk=novakk" if mint != "UNKNOWN" else None
                    send_notification(
                        "🚀 TOKEN LAUNCH DETECTED!",
                        f"Type: {info['type']}\nMint: {mint}\n\nBuy: {padre_url or 'N/A'}",
                        priority="urgent",
                        tags="rocket,moneybag",
                        click_url=padre_url
                    )
                else:
                    log(f"New tx: {sig[:32]}...", "yellow")
                    send_notification(
                        "Wallet Activity",
                        f"New transaction detected\n{sig[:32]}...\nhttps://solscan.io/tx/{sig}",
                        priority="default",
                        tags="bell",
                        click_url=f"https://solscan.io/tx/{sig}"
                    )

        except Exception as e:
            log(f"Error: {e}", "red")

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
