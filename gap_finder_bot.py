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
import launch_finder

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

_HERE = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(_HERE, "seen_terms.json")

CFG = cfgmod.load_config()


# ----------------------------------------------------------------------------
# LAUNCH CHECK — find the freshly-launched token matching a narrative term
# (pump.fun / gmgn, NOT dexscreener — a token already on Dexscreener has
# usually already pumped out of the gap).
# ----------------------------------------------------------------------------

def evaluate_gap(candidate: Dict[str, Any], coins: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    term = candidate.get("term", "").strip()
    launch_cfg = CFG.get("launch_finder", {})
    launch = launch_finder.search_launch_for_term(term, launch_cfg, coins=coins)

    is_gap = (
        candidate.get("cross_community") is True
        and candidate.get("organic") is True
        and candidate.get("crypto_notice_level") in ("none", "early_whispers")
        and launch["found"]
    )

    # keep the raw matched coin so the open flow can rebuild a dex_pair for
    # scoring without hitting a listing exchange (token may not be on one yet)
    out = {**candidate, "launch": launch, "is_gap_candidate": is_gap}
    if launch.get("mint"):
        out["_launch_coin"] = launch.get("_coin") or {}
    return out


# ----------------------------------------------------------------------------
# ALERTING
# ----------------------------------------------------------------------------

def send_telegram(text: str) -> None:
    # thin pass-through to the notifier module — keeps a single Telegram
    # implementation in one place (notifier.py) and lets the bot run
    # gracefully with no token set (notifier.send becomes a no-op).
    notifier.send(text)


def _gc_emoji(b: bool) -> str:
    return "✅" if b else "❌"


def format_alert(c: Dict[str, Any]) -> str:
    """Build a rich, human-readable gap alert (Telegram, HTML parse_mode).

    Sections: header + narrative read → gap analysis → matched token →
    on-chain sizing → action. Uses <b>/<code> (HTML) since the notifier now
    defaults to parse_mode=HTML (Markdown 400s on a lone '$' in prices)."""
    launch = c["launch"]
    term = c.get("term", "")
    age_min = None
    if launch.get("created_timestamp"):
        age_min = max(0, int((time.time() - launch["created_timestamp"] / 1000) / 60))

    # narrative strength estimate (0-10-ish, mirror of compute_entry_score)
    vol = {"hundreds": 3, "thousands": 6, "tens of thousands": 9}.get(
        str(c.get("est_posts_1_24h") or ""), 2)
    n_score = vol * 0.4 + (1.2 if c.get("cross_community") else 0) + \
              (1.2 if c.get("organic") else 0) + \
              (1.0 if str(c.get("crypto_notice_level")) == "none" else
               0.5 if str(c.get("crypto_notice_level")) == "early_whispers" else 0)

    mc = float(launch.get("market_cap_usd") or 0)

    # divergence: is the token tiny vs the narrative heat? (the edge we want)
    div = ""
    if mc > 0:
        if mc < 10_000:
            div = "🔥 very early — MC jauh di bawah naratif yang ramai"
        elif mc < 50_000:
            div = "📈 early window — masih ada ruang sebelum ramai"
        else:
            div = "⚠️ MC mulai menengah — gap menyempit"

    lines = [
        f"🕳️ <b>GAP  DETECTED</b> — “{term}”",
        f"━━━━━━━━━━━━━━━━━━━━",
        "",
        f"<b>Naratif</b>",
        f"  {c.get('description','') or '—'}",
        f"  kategori: <code>{c.get('category')}</code>",
        f"  obrolan 24j: <b>{c.get('est_posts_1_24h')}</b>  ·  skor naratif: <b>{n_score:.1f}/10</b>",
        f"  {_gc_emoji(bool(c.get('cross_community')))} cross-community  ·  {_gc_emoji(bool(c.get('organic')))} organic",
        f"  CT notice: <code>{c.get('crypto_notice_level')}</code>",
        "",
    ]

    if launch["found"]:
        sym = launch.get("symbol")
        lines += [
            f"🆕 <b>Token di pump.fun</b>",
            f"  <code>{sym}</code> — {launch.get('name') or term}",
            f"  MC: <b>${mc:,.0f}</b>  ·  umur: <b>~{age_min} min</b>",
            f"  match naratif: <b>{launch.get('match_score')}</b>",
            f"  {div}",
            f"  mint: <code>{launch['mint']}</code>",
            f"  creator: <code>{launch.get('creator') or '?'}</code>",
            "",
            f"📌 <b>Next</b>: holder screen + smart-money & entry score dijalankan "
            f"otomatis sebelum buka posisi (dry-run saat ini).",
            f"🖱️ <a href=\"{launch['pair_url']}\">buka di pump.fun</a>",
            f"🔍 verify juga: gmgn.ai / dexscreener",
        ]
    else:
        lines += [
            f"⏳ <b>Belum ada token fresh yang cocok</b> untuk “{term}”.",
            f"Jiro tetap pantau — begitu naratif ini pecah jadi launch pump.fun "
            f"yang match (MC rendah), alert baru keluar lagi di siklus berikutnya.",
        ]

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

    # Fetch fresh launches ONCE per cycle (all candidates share the same
    # recent-launch window — avoids N separate API calls).
    coins = launch_finder.fetch_recent_launches()
    print(f"  [launch-finder] {len(coins)} fresh launch(es) in window")

    seen = load_seen()
    new_gaps = []
    for c in candidates:
        term = c.get("term", "").strip()
        if not term or term.lower() in seen:
            continue
        result = evaluate_gap(c, coins=coins)
        seen.add(term.lower())
        if result["is_gap_candidate"]:
            new_gaps.append(result)
    save_seen(seen)

    wallet = None
    if trading.AUTO_TRADE_ENABLED and trading.SOLANA_PRIVATE_KEY:
        wallet = trading.Wallet(trading.SOLANA_PRIVATE_KEY)

    if not new_gaps:
        print("  -> no gap candidates with a matching fresh token this cycle")
    else:
        for g in new_gaps:
            alert = format_alert(g)
            print("\n" + alert + "\n" + "-" * 60)
            send_telegram(alert)

            launch = g["launch"]
            mint = launch.get("mint")
            if not mint:
                print("  (no fresh token found — nothing to buy, watch for launch)")
                continue

            if not trading.AUTO_TRADE_ENABLED:
                print("  (auto-trade off — set AUTO_TRADE_ENABLED=true + SOLANA_PRIVATE_KEY to act on this)")
                continue

            pos = trading.open_position(
                wallet, g["term"], mint,
                pair_url=launch.get("pair_url", ""),
                description=g.get("description", ""),
                dex_pair=launch_finder.to_dex_pair(g.get("_launch_coin") or {}),
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
