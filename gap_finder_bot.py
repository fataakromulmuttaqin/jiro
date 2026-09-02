#!/usr/bin/env python3
"""
GAP FINDER BOT v2
==================
Pipeline:
  1. narrative.scan_for_candidates() — Grok scans X for viral, off-crypto,
     organic, cross-community narratives CT hasn't tokenized yet.
  2. For each candidate, check Dexscreener: does a (probably weak/early)
     token already exist for this term? That's the "gap" — normies are
     meming it, CT hasn't fully piled in.
  3. If AUTO_TRADE_ENABLED, run it through trading.compute_entry_score()
     (narrative strength + on-chain momentum + liquidity/impact filters)
     before opening a position — not every gap is worth entering.
  4. Every scan cycle, also re-check narrative health on OPEN positions
     (trading.recheck_open_positions_narrative) so the monitor loop can
     react if a narrative is dying even before price fully shows it.

Run modes:
  python3 gap_finder_bot.py                       # single scan, alert only
  python3 gap_finder_bot.py --loop                 # repeated scans, alert only
  python3 gap_finder_bot.py --loop --with-monitor   # scans + TP/SL/on-chain
                                                     # position monitor, same process

SECRETS (env vars):
  XAI_API_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID,
  SOLANA_PRIVATE_KEY, RPC_URL, AUTO_TRADE_ENABLED, DRY_RUN
  (see trading.py and narrative.py docstrings)

TUNABLES: edit config.json (see config.py) — position size, TP/SL %,
trailing stop, partial take-profit ladder, entry filters, on-chain
exit-signal thresholds, poll/monitor intervals.
"""

import os
import sys
import json
import time
import argparse
import datetime as dt
from typing import List, Dict, Any, Optional

import requests

import config as cfgmod
import narrative
import trading
import notifier
import smart_money
import bot_controller

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

DEXSCREENER_SEARCH_URL = "https://api.dexscreener.com/latest/dex/search"
DEXSCREENER_PAIR_URL = "https://api.dexscreener.com/latest/dex/pairs/solana"

_HERE = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(_HERE, "seen_terms.json")

CFG = cfgmod.load_config()


# ----------------------------------------------------------------------------
# ON-CHAIN GAP CHECK
# ----------------------------------------------------------------------------

def check_dexscreener(term: str) -> Dict[str, Any]:
    try:
        r = requests.get(DEXSCREENER_SEARCH_URL, params={"q": term}, timeout=15)
        r.raise_for_status()
        pairs = r.json().get("pairs") or []
    except Exception as e:
        return {"found": False, "error": str(e), "matches": 0, "max_liquidity_usd": 0,
                "max_volume_24h_usd": 0, "raw_pairs": []}

    if not pairs:
        return {"found": False, "matches": 0, "max_liquidity_usd": 0, "max_volume_24h_usd": 0, "raw_pairs": []}

    max_liq = max((p.get("liquidity", {}).get("usd") or 0) for p in pairs)
    max_vol = max((p.get("volume", {}).get("h24") or 0) for p in pairs)
    top_pair = max(pairs, key=lambda p: (p.get("liquidity", {}).get("usd") or 0))
    return {
        "found": True,
        "matches": len(pairs),
        "max_liquidity_usd": round(max_liq, 2),
        "max_volume_24h_usd": round(max_vol, 2),
        "top_pair_url": top_pair.get("url"),
        "raw_pairs": pairs,
    }


def evaluate_gap(candidate: Dict[str, Any]) -> Dict[str, Any]:
    term = candidate.get("term", "").strip()
    dex = check_dexscreener(term)

    is_gap = (
        candidate.get("cross_community") is True
        and candidate.get("organic") is True
        and candidate.get("crypto_notice_level") in ("none", "early_whispers")
        and dex["matches"] <= 2
        and dex.get("max_liquidity_usd", 0) < CFG["entry_filters"]["max_liquidity_usd"]
    )

    return {**candidate, "dexscreener": dex, "is_gap_candidate": is_gap}


# ----------------------------------------------------------------------------
# ALERTING
# ----------------------------------------------------------------------------

def send_telegram(text: str) -> None:
    # thin pass-through to the notifier module — keeps a single Telegram
    # implementation in one place (notifier.py) and lets the bot run
    # gracefully with no token set (notifier.send becomes a no-op).
    notifier.send(text)


def format_alert(c: Dict[str, Any]) -> str:
    dex = c["dexscreener"]
    lines = [
        f"🕳️ *GAP CANDIDATE*: `{c.get('term')}`",
        f"_{c.get('description','')}_",
        f"category: {c.get('category')} | est posts (1-24h): {c.get('est_posts_1_24h')}",
        f"cross-community: {c.get('cross_community')} | organic: {c.get('organic')} | "
        f"CT notice: {c.get('crypto_notice_level')}",
        f"Dexscreener: matches={dex.get('matches')} max_liq=${dex.get('max_liquidity_usd')} "
        f"max_vol24h=${dex.get('max_volume_24h_usd')}",
    ]
    if dex.get("top_pair_url"):
        lines.append(f"existing weak pair (if any): {dex['top_pair_url']}")
    lines.append(f"👉 verify manually on pump.fun / gmgn.ai / dexscreener before acting")
    return "\n".join(lines)


# ----------------------------------------------------------------------------
# STATE (avoid re-alerting the same term every cycle)
# ----------------------------------------------------------------------------

def load_seen() -> set:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f:
                return set(json.load(f))
        except Exception:
            return set()
    return set()


def save_seen(seen: set) -> None:
    with open(STATE_FILE, "w") as f:
        json.dump(sorted(seen), f)


# ----------------------------------------------------------------------------
# MINT RESOLUTION (dexscreener search doesn't give raw pair objects to callers
# outside evaluate_gap, so pull the mint straight from the cached raw_pairs)
# ----------------------------------------------------------------------------

def _extract_top_pair_and_mint(dex_result: Dict[str, Any]) -> (Optional[Dict[str, Any]], Optional[str]):
    raw_pairs = dex_result.get("raw_pairs") or []
    if not raw_pairs:
        return None, None
    top_pair = max(raw_pairs, key=lambda p: (p.get("liquidity", {}).get("usd") or 0))
    base = top_pair.get("baseToken", {})
    return top_pair, base.get("address")


# ----------------------------------------------------------------------------
# MAIN SCAN CYCLE
# ----------------------------------------------------------------------------

def run_once(scan_count: int) -> None:
    # --- keep smart-money watchlist warm BEFORE scanning candidates ---
    # Cheap (~N watched wallets × getSignaturesForAddress), and means
    # compute_entry_score() can do an in-memory convergence check instead
    # of re-fetching anything per candidate.
    try:
        new_buys = smart_money.poll_watchlist()
        if new_buys:
            summary = "; ".join(f"{b['label'] or b['wallet'][:8]}→{b['mint'][:8]}" for b in new_buys)
            print(f"  [smart-money] {len(new_buys)} new buy(s) detected: {summary}")
    except Exception as e:
        print(f"[warn] smart_money.poll_watchlist failed: {e}", file=sys.stderr)

    print(f"[{dt.datetime.now().isoformat()}] asking Grok for off-crypto viral candidates...")
    try:
        candidates = narrative.scan_for_candidates()
    except Exception as e:
        print(f"[error] Grok call failed: {e}", file=sys.stderr)
        candidates = []

    print(f"  -> {len(candidates)} candidate(s) from Grok")

    seen = load_seen()
    new_gaps = []
    for c in candidates:
        term = c.get("term", "").strip()
        if not term or term.lower() in seen:
            continue
        result = evaluate_gap(c)
        seen.add(term.lower())
        if result["is_gap_candidate"]:
            new_gaps.append(result)
    save_seen(seen)

    wallet = None
    if trading.AUTO_TRADE_ENABLED and trading.SOLANA_PRIVATE_KEY:
        wallet = trading.Wallet(trading.SOLANA_PRIVATE_KEY)

    if not new_gaps:
        print("  -> no new gap candidates this cycle")
    else:
        for g in new_gaps:
            alert = format_alert(g)
            print("\n" + alert + "\n" + "-" * 60)
            send_telegram(alert)

            dex = g["dexscreener"]
            top_pair, mint = _extract_top_pair_and_mint(dex)
            if not mint:
                print("  (no existing token yet — nothing to buy, watch for launch)")
                continue

            if not trading.AUTO_TRADE_ENABLED:
                print("  (auto-trade off — set AUTO_TRADE_ENABLED=true + SOLANA_PRIVATE_KEY to act on this)")
                continue

            pos = trading.open_position(
                wallet, g["term"], mint,
                pair_url=dex.get("top_pair_url", ""),
                description=g.get("description", ""),
                dex_pair=top_pair,
                candidate=g,
            )
            if pos:
                send_telegram(f"🟢 opened {g['term']} | ${pos['position_usd']} @ ${pos['entry_price_usd']:.8f} | "
                              f"TP ${pos['tp_price_usd']:.8f} | SL ${pos['sl_price_usd']:.8f}")

    # periodic narrative-health recheck on whatever is currently open —
    # this is what lets the monitor loop react to a dying narrative even
    # before price fully reflects it
    recheck_every = CFG["system"]["narrative_recheck_every_n_scans"]
    if recheck_every > 0 and scan_count % recheck_every == 0:
        try:
            trading.recheck_open_positions_narrative()
        except Exception as e:
            print(f"[warn] narrative recheck failed: {e}", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--loop", action="store_true", help="run continuously")
    parser.add_argument("--with-monitor", action="store_true",
                         help="also run the TP/SL/on-chain position monitor in this same loop")
    args = parser.parse_args()

    if not args.loop:
        run_once(scan_count=1)
        return

    # Remote control (Telegram) — lets us /stop, /start, /status, /config set
    # without SSH. Safe no-op if token/chat not configured.
    bot_controller.start_control_thread()

    poll_minutes = cfgmod.load_config()["system"]["poll_interval_minutes"]
    print(f"Looping: scan every {poll_minutes} min" +
          (f", monitor every {cfgmod.load_config()['system']['monitor_interval_seconds']}s" if args.with_monitor else "") +
          ". Ctrl+C to stop.")

    wallet = trading.Wallet(trading.SOLANA_PRIVATE_KEY) if trading.SOLANA_PRIVATE_KEY else None
    scan_count = 0
    last_scan_at = 0.0

    while True:
        # /stop check — clean shutdown at the next cycle boundary.
        if bot_controller.should_stop():
            print("[bot_control] stop requested — shutting down cleanly.")
            if notifier.is_configured():
                notifier.send("🛑 Jiro stopped (via Telegram command).")
            break

        cfg_now = cfgmod.load_config()  # pick up live config.json edits each tick
        poll_seconds = cfg_now["system"]["poll_interval_minutes"] * 60
        monitor_seconds = cfg_now["system"]["monitor_interval_seconds"]

        now = time.time()
        if now - last_scan_at >= poll_seconds:
            scan_count += 1
            run_once(scan_count)
            last_scan_at = time.time()

        if args.with_monitor:
            try:
                trading.monitor_once(wallet)
            except Exception as e:
                print(f"[error] monitor pass failed: {e}", file=sys.stderr)
            time.sleep(monitor_seconds)
        else:
            # no monitor running, no need to wake up faster than the scan cadence
            time.sleep(min(poll_seconds, 60))


if __name__ == "__main__":
    main()
