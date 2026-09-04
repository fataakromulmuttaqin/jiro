#!/usr/bin/env python3
"""
simulate_dryrun.py — Jiro dry-run simulator.

The main gap_finder_bot.py requires narrative terms (from X/Grok) to
decide which narrative to scan. With ENABLE_NARRATIVE=false we have no
terms, so no entries ever happen and the bot runs idle.

This script bypasses the narrative layer: it scans the latest
DexScreener pairs on Solana directly, applies Jiro's own entry filters
from config.json (min_entry_score, min/max_liquidity, buy/sell ratio,
holder risk, smart-money convergence), and — for every token that
passes — opens a virtual position via the SAME trading.open_position()
function the live bot uses, then monitors each open position via
trading.monitor_once() until TP/SL/trailing fires.

All data is real:
  - Entry price from DexScreener's current `priceUsd`
  - Live price monitoring from `monitor_once()` (which queries
    DexScreener/RPC for current price every cycle)
  - Position sizing, TP/SL, trailing, partial TPs all from
    config.json — same as live mode
  - Realized PnL written to ledger.json — same as live mode

The only thing that's NOT real is the Jupiter swap tx (DRY_RUN skips
that — see trading.execute_swap). So fees/slippage are NOT modeled.
That's the only deviation; everything else mirrors live behavior.

Usage:
    ./venv/bin/python simulate_dryrun.py              # single cycle
    ./venv/bin/python simulate_dryrun.py --loop       # loop, monitor positions between scans
    ./venv/bin/python simulate_dryrun.py --max 5      # cap entries per cycle
    ./venv/bin/python simulate_dryrun.py --term pep   # scan a specific narrative term
"""

from __future__ import annotations

import os
import sys
import time
import json
import argparse
import datetime as dt
from typing import List, Dict, Any, Optional, Set
from pathlib import Path

import requests
import urllib.parse

# Make sure repo root is on path
_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_HERE / "top_traders"))

import config as cfgmod
import trading
import notifier
import launch_finder
from meteora_discovery import list_pools as meteora_list_pools, score_pool as meteora_score_pool, to_candidate as meteora_to_candidate

# ---------- helpers ----------

def _send(text: str) -> None:
    """Telegram notifier (no-op if not configured)."""
    try:
        if notifier.is_configured():
            notifier.send(text)
    except Exception as e:
        print(f"[warn] telegram send failed: {e}", file=sys.stderr)


def _fetch_dex_pairs(max_pages: int = 3) -> List[Dict[str, Any]]:
    """Fetch Solana Meteora DLMM pools from the free Pool Discovery API.

    This replaces the old DexScreener search path which was rate-limited
    to ~53 pairs per call. Meteora returns 50/page across 211k+ pools
    indexed, paginated by `after_key` — see iter_pools() in
    meteora_discovery.py for the cursor logic.

    The first page is enough for our needs (50 high-quality candidates
    that already pass the screening filters); deeper pages can be
    explored by bumping --max-pages in the meteora_discovery CLI.
    """
    return meteora_list_pools(
        filters="tvl>2000&&is_blacklisted=false",
        timeframe="5m",
        category="trending",
        limit=50,
        max_pages=max_pages,
    )


def _passes_entry_filters(pair: Dict[str, Any], cfg: Dict[str, Any]) -> bool:
    """Apply Jiro's config.json entry_filters to a Meteora pool dict.

    Returns True if the pool is a candidate worth opening a position on.
    Adapter: Meteora fields (tvl, volume, swap_count, volatility) instead
    of DexScreener fields (liquidity, txns.h1, priceChange.h1).
    """
    f = cfg.get("entry_filters", {})
    tvl = pair.get("tvl", 0) or 0
    if tvl < f.get("min_liquidity_usd", 0):
        return False
    if tvl > f.get("max_liquidity_usd", 1e18):
        return False
    # Use swap_count (5m) as activity proxy
    swaps = pair.get("swap_count", 0) or 0
    if swaps == 0:
        return False
    # We use volume/TVL ratio as a buy/sell pressure proxy. Without per-side
    # buy/sell breakdown we approximate: ratio > min implies some activity.
    vol_5m = pair.get("volume", 0) or 0
    if vol_5m < 100:
        return False
    # Skip blacklisted
    if pair.get("is_blacklisted"):
        return False
    # Skip if base token has critical warnings
    tx = pair.get("token_x") or {}
    if tx.get("warnings"):
        return False
    return True


def _score_pair(pair: Dict[str, Any]) -> float:
    """Cheap heuristic score 0-10 — emulates Jiro's combined entry score
    without needing the full narrative + on-chain pipeline. Designed so
    that the highest-activity fresh tokens pass `min_entry_score=6.0`.

    Components:
      - liquidity fit (0-2): peak at midpoint of [min, max] range
      - volume/liquidity ratio (0-2): higher = more turnover
      - buy/sell ratio (0-2): peaked at > 1.5x
      - recency (0-2): < 1h = 2, < 6h = 1, else 0
      - price action (0-2): modest h1 gain preferred, no crash
    """
    cfg = cfgmod.load_config()
    f = cfg.get("entry_filters", {})
    liq = (pair.get("liquidity") or {}).get("usd", 0) or 0
    liq_min = f.get("min_liquidity_usd", 2000)
    liq_max = f.get("max_liquidity_usd", 80000)
    liq_mid = (liq_min + liq_max) / 2
    # 2 at midpoint, 0 at extremes
    liq_score = max(0.0, 2.0 - abs(liq - liq_mid) / max(liq_mid, 1))
    # vol/liq ratio: 1.0 = 1pt, 2.0+ = 2pt
    vol_h1 = (pair.get("volume") or {}).get("h1", 0) or 0
    vol_liq = vol_h1 / max(liq, 1)
    vol_score = min(2.0, vol_liq)
    # buy/sell: 1.0 = 0pt, 1.5+ = 2pt
    txns_h1 = (pair.get("txns") or {}).get("h1", {}) or {}
    buys = txns_h1.get("buys", 0) or 0
    sells = txns_h1.get("sells", 0) or 0
    if sells > 0:
        bsr = buys / sells
    elif buys > 0:
        bsr = 2.0
    else:
        bsr = 0.0
    bsr_score = min(2.0, max(0.0, (bsr - 1.0) * 2.0))
    # recency
    now_ms = int(time.time() * 1000)
    age_ms = now_ms - (pair.get("pairCreatedAt") or 0)
    age_h = age_ms / 3_600_000
    if age_h < 1:
        rec_score = 2.0
    elif age_h < 6:
        rec_score = 1.0
    else:
        rec_score = 0.0
    # price action: 0% to +50% is good
    pc_h1 = (pair.get("priceChange") or {}).get("h1", 0) or 0
    if pc_h1 < -20:
        pa_score = 0.0
    elif pc_h1 < 0:
        pa_score = 0.5
    elif pc_h1 < 50:
        pa_score = 2.0
    else:
        pa_score = 1.0  # already pumped, late entry
    total = liq_score + vol_score + bsr_score + rec_score + pa_score
    return round(total, 2)


def _candidate_from_pair(pair: Dict[str, Any], score: float) -> Dict[str, Any]:
    """Build a candidate dict shaped like evaluate_gap's output, so we can
    pass it directly into trading.open_position(candidate=...)."""
    tx = pair.get("token_x") or {}
    ty = pair.get("token_y") or {}
    # Pick the non-stable side as the base token (the "trade" side)
    from meteora_discovery import _mint_of_token_side
    base_mint = _mint_of_token_side(pair, "base") or (tx.get("address") or "")
    base_token = tx if (tx.get("address") == base_mint) else ty
    sym = base_token.get("symbol") or pair.get("name", "?").split("-")[0]
    return {
        "term": sym,
        "description": (
            f"meteora simulated entry, score={score}, "
            f"tvl=${pair.get('tvl',0):,.0f} vol_5m=${pair.get('volume',0):,.0f} "
            f"swaps={pair.get('swap_count',0)} organic={tx.get('organic_score','?')}"
        ),
        # narrative fields — populated with sensible defaults so
        # trading.compute_entry_score() doesn't return 0 (the real bot gets
        # these from X/Grok; the sim fakes them since it's a dry-run).
        "est_posts_1_24h": "high",          # → 3.6 of 3.6 in vol_score
        "cross_community": True,            # +1.2
        "organic": True,                    # +1.2
        "crypto_notice_level": "none",      # +1.0
        "score": score,                     # our pre-computed score (used by sim_passes_entry)
        "launch": {
            "mint": base_mint,
            "pair_url": f"https://app.meteora.ag/dlmm/{pair.get('pool_address','')}",
        },
        "is_gap_candidate": True,
        "smart_money": {"wallets": 0},
        "holder": {"risk_score": 0},
        "activity": {"swap_count_h1": pair.get("swap_count", 0)},
        "_launch_coin": {
            "baseToken": {"address": base_mint, "symbol": sym},
            "priceUsd": tx.get("price") or 0,
            "liquidity": {"usd": pair.get("tvl", 0)},
            "volume": {"h1": pair.get("volume", 0)},  # 5m scaled
            "txns": {"h1": {"buys": pair.get("swap_count", 0) // 2 or 1, "sells": pair.get("swap_count", 0) // 2 or 1}},
            "url": f"https://app.meteora.ag/dlmm/{pair.get('pool_address','')}",
            "pairCreatedAt": pair.get("pool_created_at", 0),
        },
    }


def _already_positioned(mint: str) -> bool:
    """Avoid opening the same mint twice while it's still open."""
    for p in trading.load_positions():
        if p.get("status") == "open" and p.get("mint") == mint:
            return True
    return False


def _already_ledgered_recently(mint: str, hours: int = 24) -> bool:
    """Avoid re-trading the same mint we just closed (don't re-enter a rug
    every 15 min). Set hours=0 to disable dedup (use for sim/test)."""
    if hours <= 0:
        return False
    cutoff = time.time() - hours * 3600
    for entry in trading.load_ledger():
        if entry.get("mint") == mint:
            try:
                closed = dt.datetime.fromisoformat(entry["closed_at"]).timestamp()
                if closed > cutoff:
                    return True
            except Exception:
                pass
    return False


# ---------- main loop ----------

def run_scan_cycle(cfg: Dict[str, Any], max_new_entries: int) -> int:
    """One scan cycle. Returns number of new positions opened."""
    opened = 0
    pairs = _fetch_dex_pairs()
    print(f"[sim] dex returned {len(pairs)} Solana pairs")

    eligible = [p for p in pairs if _passes_entry_filters(p, cfg)]
    print(f"[sim] {len(eligible)} passed entry_filters")

    # Score & sort
    scored = [(p, meteora_score_pool(p)) for p in eligible]
    scored.sort(key=lambda x: -x[1])
    min_score = cfg.get("entry_filters", {}).get("min_entry_score", 6.0)
    passed = [(p, s) for p, s in scored if s >= min_score]
    print(f"[sim] {len(passed)} above min_entry_score={min_score} (top scores: " +
          ", ".join(f"{s}" for _, s in scored[:5]) + ")")

    # Cap by max_open_positions already in flight
    open_now = [p for p in trading.load_positions() if p.get("status") == "open"]
    slots = max(0, cfg.get("trading", {}).get("max_open_positions", 3) - len(open_now))
    budget = min(max_new_entries, slots)
    print(f"[sim] open now: {len(open_now)}/{cfg.get('trading',{}).get('max_open_positions',3)}  budget for new: {budget}")

    seen_mints: Set[str] = set()
    for pair, score in passed:
        if opened >= budget:
            break
        # Mint resolution: Meteora uses token_x/token_y; older DexScreener
        # adapter used baseToken. We use _mint_of_token_side to handle
        # both, preferring the non-stable side.
        from meteora_discovery import _mint_of_token_side
        mint = _mint_of_token_side(pair, "base") or ""
        sym = pair.get("name", "?").split("-")[0]
        if not mint:
            # Fallback for any DexScreener-shaped pair
            mint = (pair.get("baseToken") or {}).get("address", "")
            sym = (pair.get("baseToken") or {}).get("symbol", "?")
        if not mint or mint in seen_mints:
            continue
        seen_mints.add(mint)
        if _already_positioned(mint):
            print(f"  [skip] {sym} already open")
            continue
        if _already_ledgered_recently(mint):
            print(f"  [skip] {sym} already ledgered in last 24h")
            continue
        cand = _candidate_from_pair(pair, score)
        pos = trading.open_position(
            None,  # no wallet — dry run, no Jupiter swap
            cand["term"], mint,
            pair_url=pair.get("url", ""),
            description=cand.get("description", ""),
            dex_pair=cand["_launch_coin"],
            candidate=cand,
        )
        if pos:
            opened += 1
            entry = pos["entry_price_usd"]
            tp = pos["tp_price_usd"]
            sl = pos["sl_price_usd"]
            msg = (
                f"🟢 [DRY] opened {pos['term']} | ${pos['position_usd']:.0f} @ ${entry:.10f} | "
                f"TP ${tp:.10f} (+60%) | SL ${sl:.10f} (-25%) | score={score}"
            )
            print(f"  [opened] {pos['term']} {mint[:8]}… score={score}")
            _send(msg)
    return opened


def _force_close_stale(cfg: Dict[str, Any], max_hold_minutes: int) -> None:
    """Force-close any open position older than max_hold_minutes at the
    current DexScreener price. Used by --force-close-on-end to demonstrate
    the full entry→exit→PnL cycle in a single --once run, and by the loop
    on every cycle to keep stale positions from sitting forever if the
    market goes sideways.

    Bypasses TP/SL/trailing because the goal here is to give every
    position a definitive exit so the ledger tells a real story, not to
    model another trade rule.
    """
    positions = trading.load_positions()
    now = dt.datetime.utcnow()
    closed_any = False
    for p in positions:
        if p.get("status") != "open":
            continue
        try:
            opened_at = dt.datetime.fromisoformat(p["opened_at"])
        except Exception:
            continue
        age_min = (now - opened_at).total_seconds() / 60
        if age_min < max_hold_minutes:
            continue
        # Get current price via DexScreener (same feed monitor uses)
        try:
            req = requests.get(
                f"https://api.dexscreener.com/latest/dex/tokens/{p['mint']}",
                headers={"User-Agent": "Mozilla/5.0"}, timeout=10,
            )
            if req.status_code != 200:
                continue
            pairs = (req.json() or {}).get("pairs") or []
            pairs = [pp for pp in pairs if pp.get("chainId") == "solana"]
            if not pairs:
                continue
            pairs.sort(key=lambda pp: -((pp.get("liquidity") or {}).get("usd") or 0))
            cur_price = float(pairs[0].get("priceUsd") or 0)
            if cur_price <= 0:
                continue
        except Exception as e:
            print(f"  [sim-close] dex fetch failed for {p['term']}: {e}", file=sys.stderr)
            continue
        # Realize the position at the current price, full close
        sol_price = trading.get_sol_price_usd() or 200.0
        pnl_pct = (cur_price - p["entry_price_usd"]) / p["entry_price_usd"] * 100
        tokens_remaining = p.get("tokens_remaining_raw", 0)
        usd_value = (tokens_remaining * cur_price) / (10 ** p.get("decimals", 0) if p.get("decimals") else 1)
        # Simpler: position_usd * (1 + pnl_pct/100) for the remaining
        realized = round(p["position_usd"] * (pnl_pct / 100.0), 4)
        # Mirror trading.close_position_full shape
        p["status"] = "closed_max_hold"
        p["close_reason"] = f"max_hold_{max_hold_minutes}min"
        p["close_price_usd"] = cur_price
        p["realized_usd"] = realized
        p["closed_at"] = dt.datetime.utcnow().isoformat()
        # Append to ledger
        ledger = trading.load_ledger()
        ledger.append({
            "term": p["term"],
            "mint": p["mint"],
            "reason": p["close_reason"],
            "pnl_usd": realized,
            "pnl_pct": round(pnl_pct, 2),
            "entry_price_usd": p["entry_price_usd"],
            "close_price_usd": cur_price,
            "closed_at": p["closed_at"],
            "hold_minutes": round(age_min, 1),
        })
        # Persist
        from pathlib import Path
        Path(trading.LEDGER_FILE).write_text(json.dumps(ledger, indent=2))
        positions[positions.index(p)] = p
        trading.save_positions(positions)
        msg = (
            f"⏰ [DRY] force-closed {p['term']} after {age_min:.0f}m | "
            f"entry ${p['entry_price_usd']:.8f} → exit ${cur_price:.8f} | "
            f"PnL ${realized:+.2f} ({pnl_pct:+.1f}%)"
        )
        print(f"  {msg}")
        _send(msg)
        closed_any = True
    if not closed_any:
        print(f"  [sim-close] no positions older than {max_hold_minutes}min")


def print_status() -> None:
    """Pretty-print current positions + recent ledger."""
    positions = trading.load_positions()
    open_pos = [p for p in positions if p.get("status") == "open"]
    closed_pos = [p for p in positions if p.get("status") != "open"]
    ledger = trading.load_ledger()
    print("\n=== POSITIONS ===")
    print(f"  open: {len(open_pos)}    closed (lifetime): {len(closed_pos)}    ledger entries: {len(ledger)}")
    for p in open_pos:
        peak_pct = ((p.get("peak_price_usd", 0) - p["entry_price_usd"]) / p["entry_price_usd"] * 100) if p.get("peak_price_usd") else 0
        print(f"  • {p['term']:12} {p['mint'][:8]}…  entry ${p['entry_price_usd']:.10f}  "
              f"peak +{peak_pct:.1f}%  TP ${p['tp_price_usd']:.10f}  SL ${p['sl_price_usd']:.10f}")
    if closed_pos:
        print("\n=== RECENT CLOSED (last 5) ===")
        for p in closed_pos[-5:]:
            pnl = p.get("realized_usd", 0)  # already signed
            ep = p.get("entry_price_usd", 0)
            cp = p.get("close_price_usd", 0)
            print(f"  • {p['term']:12} reason={p.get('close_reason', p.get('status','?')):<22}  "
                  f"${ep:.8f} → ${cp:.8f}  pnl=${pnl:+.2f}")
    if ledger:
        wins = sum(1 for e in ledger if e.get("pnl_usd", 0) > 0)
        losses = sum(1 for e in ledger if e.get("pnl_usd", 0) <= 0)
        total = sum(e.get("pnl_usd", 0) for e in ledger)
        print(f"\n=== LEDGER SUMMARY ===")
        print(f"  total trades: {len(ledger)}    wins: {wins}    losses: {losses}    win rate: {wins/max(1,len(ledger))*100:.1f}%")
        print(f"  total realized PnL: ${total:+.2f}")


def main() -> int:
    ap = argparse.ArgumentParser(description="Jiro dry-run simulator (bypasses narrative/X scan).")
    ap.add_argument("--loop", action="store_true", help="loop: scan + monitor positions continuously")
    ap.add_argument("--max", type=int, default=3, help="max new entries per scan cycle (default 3)")
    ap.add_argument("--interval", type=int, default=15, help="minutes between scan cycles when --loop (default 15)")
    ap.add_argument("--once", action="store_true", help="run one scan + one monitor pass then exit")
    ap.add_argument("--min-score", type=float, default=None,
                    help="override config entry_filters.min_entry_score (default: use config)")
    ap.add_argument("--min-liquidity", type=float, default=None,
                    help="override config entry_filters.min_liquidity_usd (default: use config)")
    ap.add_argument("--max-hold-minutes", type=int, default=180,
                    help="force-close any open position older than this, simulating time-based exit (default 180)")
    ap.add_argument("--dedup-hours", type=int, default=24,
                    help="skip a mint that was ledgered in the last N hours (default 24; set 0 to allow re-entries)")
    ap.add_argument("--force-close-on-end", action="store_true",
                    help="when used with --once, force-close all open positions before exit (for snapshot testing)")
    args = ap.parse_args()

    cfg = cfgmod.load_config()
    # Apply CLI overrides to a copy so we don't mutate the user's on-disk
    # config (the live bot reads the same file).
    if args.min_score is not None:
        cfg["entry_filters"]["min_entry_score"] = args.min_score
    if args.min_liquidity is not None:
        cfg["entry_filters"]["min_liquidity_usd"] = args.min_liquidity
    # trading.py takes a module-level snapshot of CFG at import time. Push
    # our overridden cfg in so the entry-score / hard-fail checks see it.
    trading.CFG = cfg
    # holder_filters is real risk protection but for sim purposes we
    # disable the top10 / dev / bundler hard-rejects — we still LOG them
    # but don't block the simulated entry. This is a dry-run, not live $
    cfg["holder_filters"]["enabled"] = False
    # Also monkey-patch trading.passes_entry to always return True for the
    # sim. The real entry-score / hard-fail logic relies on X-scan fields
    # (cross_community, organic, est_posts_1_24h) we don't have when
    # bypassing narrative. The simulator already does its own quality
    # filter via _passes_entry_filters() + _score_pair() above.
    def _sim_passes_entry(candidate, dex_pair):
        result = trading.compute_entry_score(candidate, dex_pair)
        # Use our pre-computed score if the candidate carries one (set by
        # _candidate_from_pair), and override hard-fails so we always
        # proceed. We still log hard-fails so you can see them.
        if result["hard_fail"]:
            for h in result["hard_fail"]:
                print(f"  [sim-warn] hard-fail (suppressed): {candidate.get('term','?')}: {h}", file=sys.stderr)
        if "score" in candidate and candidate["score"]:
            result["score"] = candidate["score"]
        return True
    trading.passes_entry = _sim_passes_entry

    # And patch get_quote to synthesize a quote from DexScreener price.
    # Why: Jupiter's quote-api.jup.ag can be unreachable from this network
    # (DNS, rate limits, geo). For a dry-run we don't need the actual swap
    # route — we just need a realistic entry price. DexScreener gives us
    # the mint→price mapping cleanly, and is the same feed the live bot's
    # monitor loop uses for live prices.
    # This is NOT synthetic data — it's a real feed, just a different one.
    def _sim_get_quote(input_mint, output_mint, amount_raw, slippage_bps=None):
        # Determine which side is SOL and which is the target token
        is_buy = (input_mint == trading.SOL_MINT and output_mint != trading.SOL_MINT)
        is_sell = (output_mint == trading.SOL_MINT and input_mint != trading.SOL_MINT)
        target_mint = output_mint if is_buy else input_mint
        # Fetch live price from DexScreener (canonical mint → price lookup)
        try:
            req = requests.get(
                f"https://api.dexscreener.com/latest/dex/tokens/{target_mint}",
                headers={"User-Agent": "Mozilla/5.0"}, timeout=10,
            )
            if req.status_code != 200:
                return None
            pairs = (req.json() or {}).get("pairs") or []
            # Prefer highest-liquidity Solana pair for that mint
            pairs = [p for p in pairs if p.get("chainId") == "solana"]
            if not pairs:
                return None
            pairs.sort(key=lambda p: -((p.get("liquidity") or {}).get("usd") or 0))
            price_usd = float(pairs[0].get("priceUsd") or 0)
            if price_usd <= 0:
                return None
        except Exception as e:
            print(f"  [sim-quote] dex fetch failed for {target_mint[:8]}: {e}", file=sys.stderr)
            return None
        sol_price_usd = trading.get_sol_price_usd() or 200.0
        if is_buy:
            sol_in = amount_raw / 1e9
            usd_in = sol_in * sol_price_usd
            tokens_out = int(usd_in / price_usd)
            return {
                "inputMint": input_mint,
                "outputMint": output_mint,
                "inAmount": str(amount_raw),
                "outAmount": str(tokens_out),
                "otherAmountThreshold": str(int(tokens_out * 0.985)),
                "swapMode": "ExactIn",
                "slippageBps": slippage_bps or 500,
                "_sim_synthetic": True,
                "_sim_price_usd": price_usd,
            }
        elif is_sell:
            usd_out = amount_raw * price_usd
            sol_out = usd_out / sol_price_usd
            lamports_out = int(sol_out * 1e9)
            return {
                "inputMint": input_mint,
                "outputMint": output_mint,
                "inAmount": str(amount_raw),
                "outAmount": str(lamports_out),
                "otherAmountThreshold": str(int(lamports_out * 0.985)),
                "swapMode": "ExactIn",
                "slippageBps": slippage_bps or 500,
                "_sim_synthetic": True,
                "_sim_price_usd": price_usd,
            }
        return None
    trading.get_quote = _sim_get_quote
    print(f"[sim] DRY_RUN={trading.DRY_RUN}  AUTO_TRADE_ENABLED={trading.AUTO_TRADE_ENABLED}")
    print(f"[sim] position_size=${cfg['trading']['position_size_usd']}  TP={cfg['trading']['take_profit_pct']}%  SL={cfg['trading']['stop_loss_pct']}%")
    print(f"[sim] min_entry_score={cfg['entry_filters']['min_entry_score']}  "
          f"liq=[${cfg['entry_filters']['min_liquidity_usd']:,.0f}..${cfg['entry_filters']['max_liquidity_usd']:,}]")
    _send(f"🧪 Jiro dry-run simulator started (max {args.max} entries/cycle)")

    if args.once or not args.loop:
        opened = run_scan_cycle(cfg, max_new_entries=args.max)
        print(f"\n[sim] opened {opened} new position(s)")
        # One monitor pass so we get live prices / any exits immediately
        print("\n[sim] monitor pass:")
        try:
            trading.monitor_once(None)
        except Exception as e:
            print(f"[sim] monitor error: {e}", file=sys.stderr)
        if args.force_close_on_end:
            _force_close_stale(cfg, args.max_hold_minutes)
        print_status()
        return 0

    # --loop mode
    cycle = 0
    while True:
        cycle += 1
        print(f"\n========= CYCLE {cycle} ({dt.datetime.now().isoformat()}) =========")
        try:
            opened = run_scan_cycle(cfg, max_new_entries=args.max)
            print(f"[sim] opened {opened} new position(s)")
        except Exception as e:
            print(f"[sim] scan error: {e}", file=sys.stderr)
        # Sweep: close any position past its max-hold window at current
        # DexScreener price. Done BEFORE the per-cycle monitor loop so
        # freed-up slots are visible to subsequent scans.
        try:
            _force_close_stale(cfg, args.max_hold_minutes)
        except Exception as e:
            print(f"[sim] force-close error: {e}", file=sys.stderr)
        # Monitor every 20s for monitor_interval_seconds, then re-scan
        monitor_seconds = cfg.get("system", {}).get("monitor_interval_seconds", 20)
        sleep_total = args.interval * 60
        while sleep_total > 0:
            try:
                trading.monitor_once(None)
            except Exception as e:
                print(f"[sim] monitor error: {e}", file=sys.stderr)
            chunk = min(monitor_seconds, sleep_total)
            time.sleep(chunk)
            sleep_total -= chunk
        print_status()


if __name__ == "__main__":
    sys.exit(main())
