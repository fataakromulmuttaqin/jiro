#!/usr/bin/env python3
"""
onchain_analyzer.py — reads real on-chain / market-microstructure signals to
detect dumps EARLIER than a plain price-based stop-loss would.

Signals implemented (all use only free/public data — Solana RPC + Dexscreener,
no paid API required):

1. WHALE DUMP — snapshots the top N token holders (via RPC getTokenLargestAccounts)
   over time. If their combined balance drops sharply within a short window,
   large holders are exiting — often ahead of price fully reflecting it.

2. LIQUIDITY PULL — snapshots pool liquidity (USD) from Dexscreener over time.
   A sudden, sharp drop is the single strongest rug signal there is (LP being
   removed). This should trigger an immediate exit regardless of your price-based
   TP/SL, because price can lag or the pool can become unsellable right after.

3. SELL PRESSURE — uses Dexscreener's own buys/sells transaction counts
   (5m and 1h windows). If sells are heavily outnumbering buys, momentum has
   flipped even if price hasn't cracked your stop-loss yet.

4. FAST DUMP (price velocity) — a fixed % stop-loss reacts the same whether
   price fell over 2 hours or 20 seconds. This tracks price velocity and
   fires early if price is crashing fast, before it necessarily reaches your
   configured stop-loss level.

LIMITATIONS (be honest with yourself about these):
- getTokenLargestAccounts returns the top 20 TOKEN ACCOUNTS, which can include
  the liquidity pool's own vault, not just "real" holder wallets. We can't
  perfectly filter that out with free RPC alone — treat whale-dump as a
  probabilistic signal, not certainty.
- All of this is polling-based (every `monitor_interval_seconds`), not a
  streaming mempool feed — a large dump can still complete between polls.
- None of this can see off-chain coordination (e.g. a dev discord deciding
  to dump) before it hits the chain.
"""

import os
import time
import requests
from typing import Dict, Any, List, Optional, Tuple

import rpc_client

RPC_URL = os.environ.get("RPC_URL", "https://api.mainnet-beta.solana.com")
DEXSCREENER_TOKEN_URL = "https://api.dexscreener.com/latest/dex/tokens"

# in-memory rolling history per mint: {mint: [(timestamp, snapshot_dict), ...]}
_history: Dict[str, List[Tuple[float, Dict[str, Any]]]] = {}
_HISTORY_MAX_AGE_SECONDS = 60 * 30  # keep 30 min of history per mint


def _rpc_call(method: str, params: list) -> Optional[Dict[str, Any]]:
    # delegate to the failover client — tries primary (Helius) first, then
    # any RPC_FALLBACK_URLS in order. returns None only if EVERY provider
    # is unavailable (or all in cooldown).
    return rpc_client.rpc_call(method, params)


def get_top_holders_total(mint: str, top_n: int) -> Optional[int]:
    """Sum of raw token amounts held by the top N largest token accounts."""
    result = _rpc_call("getTokenLargestAccounts", [mint])
    if not result:
        return None
    accounts = result.get("value", [])[:top_n]
    if not accounts:
        return None
    total = 0
    for acc in accounts:
        try:
            total += int(acc.get("amount", 0))
        except (TypeError, ValueError):
            continue
    return total


def get_dex_pair_data(mint: str) -> Optional[Dict[str, Any]]:
    try:
        r = requests.get(f"{DEXSCREENER_TOKEN_URL}/{mint}", timeout=15)
        r.raise_for_status()
        pairs = r.json().get("pairs") or []
        if not pairs:
            return None
        return max(pairs, key=lambda p: (p.get("liquidity", {}).get("usd") or 0))
    except Exception:
        return None


def record_snapshot(mint: str, cfg_onchain: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Fetch fresh data and append it to this mint's history. Returns the
    snapshot taken (or None if data was unavailable this cycle)."""
    pair = get_dex_pair_data(mint)
    if pair is None:
        return None

    price = pair.get("priceUsd")
    liquidity = (pair.get("liquidity") or {}).get("usd")
    txns_m5 = (pair.get("txns") or {}).get("m5") or {}
    txns_h1 = (pair.get("txns") or {}).get("h1") or {}

    whale_total = get_top_holders_total(mint, cfg_onchain.get("whale_check_top_n_holders", 10))

    snapshot = {
        "price_usd": float(price) if price else None,
        "liquidity_usd": float(liquidity) if liquidity else None,
        "buys_m5": txns_m5.get("buys", 0),
        "sells_m5": txns_m5.get("sells", 0),
        "buys_h1": txns_h1.get("buys", 0),
        "sells_h1": txns_h1.get("sells", 0),
        "whale_top_n_total": whale_total,
    }

    now = time.time()
    hist = _history.setdefault(mint, [])
    hist.append((now, snapshot))
    # trim old entries
    cutoff = now - _HISTORY_MAX_AGE_SECONDS
    _history[mint] = [(t, s) for (t, s) in hist if t >= cutoff]

    return snapshot


def _snapshot_at_or_before(mint: str, seconds_ago: float) -> Optional[Dict[str, Any]]:
    hist = _history.get(mint, [])
    if not hist:
        return None
    target_time = time.time() - seconds_ago
    # find the latest snapshot that is still <= target_time (i.e. old enough)
    candidates = [(t, s) for (t, s) in hist if t <= target_time]
    if not candidates:
        return None
    return max(candidates, key=lambda x: x[0])[1]


def check_liquidity_pull(mint: str, cfg_onchain: Dict[str, Any]) -> Optional[str]:
    window_s = cfg_onchain.get("liquidity_check_window_minutes", 5) * 60
    threshold_pct = cfg_onchain.get("liquidity_pull_threshold_pct", 20)

    hist = _history.get(mint, [])
    if len(hist) < 2:
        return None
    current = hist[-1][1]["liquidity_usd"]
    past = _snapshot_at_or_before(mint, window_s)
    if current is None or past is None or past.get("liquidity_usd") is None:
        return None
    prev_liq = past["liquidity_usd"]
    if prev_liq <= 0:
        return None
    drop_pct = (prev_liq - current) / prev_liq * 100
    if drop_pct >= threshold_pct:
        return f"liquidity dropped {drop_pct:.1f}% in ~{window_s//60:.0f}min (${prev_liq:.0f} -> ${current:.0f})"
    return None


def check_whale_dump(mint: str, cfg_onchain: Dict[str, Any]) -> Optional[str]:
    window_s = cfg_onchain.get("whale_check_window_minutes", 10) * 60
    threshold_pct = cfg_onchain.get("whale_dump_threshold_pct", 15)

    hist = _history.get(mint, [])
    if len(hist) < 2:
        return None
    current = hist[-1][1]["whale_top_n_total"]
    past = _snapshot_at_or_before(mint, window_s)
    if current is None or past is None or past.get("whale_top_n_total") is None:
        return None
    prev_total = past["whale_top_n_total"]
    if prev_total <= 0:
        return None
    drop_pct = (prev_total - current) / prev_total * 100
    if drop_pct >= threshold_pct:
        return f"top holders' combined balance dropped {drop_pct:.1f}% in ~{window_s//60:.0f}min"
    return None


def check_sell_pressure(mint: str, cfg_onchain: Dict[str, Any]) -> Optional[str]:
    trigger = cfg_onchain.get("sell_pressure_ratio_trigger", 0.35)
    hist = _history.get(mint, [])
    if not hist:
        return None
    snap = hist[-1][1]
    buys, sells = snap.get("buys_m5", 0), snap.get("sells_m5", 0)
    total = buys + sells
    if total < 5:  # not enough recent activity to judge
        return None
    ratio = buys / total
    if ratio <= trigger:
        return f"sell pressure: only {ratio*100:.0f}% of last {total} trades (5m) were buys"
    return None


def check_fast_dump(mint: str, cfg_onchain: Dict[str, Any]) -> Optional[str]:
    window_s = cfg_onchain.get("fast_dump_window_seconds", 180)
    threshold_pct = cfg_onchain.get("fast_dump_price_drop_pct", 12)

    hist = _history.get(mint, [])
    if len(hist) < 2:
        return None
    current = hist[-1][1]["price_usd"]
    past = _snapshot_at_or_before(mint, window_s)
    if current is None or past is None or past.get("price_usd") is None:
        return None
    prev_price = past["price_usd"]
    if prev_price <= 0:
        return None
    drop_pct = (prev_price - current) / prev_price * 100
    if drop_pct >= threshold_pct:
        return f"price dropped {drop_pct:.1f}% in ~{window_s}s (fast dump)"
    return None


def evaluate_exit_signals(mint: str, cfg_onchain: Dict[str, Any]) -> List[str]:
    """Record a fresh snapshot, then check all enabled on-chain signals.
    Returns a list of human-readable reasons to exit immediately (empty = all clear).
    Any single signal firing is treated as a reason for an EMERGENCY exit,
    independent of whether price has hit the normal TP/SL yet."""
    if not cfg_onchain.get("enabled", True):
        return []

    snap = record_snapshot(mint, cfg_onchain)
    if snap is None:
        return []  # no data this cycle, don't false-trigger

    reasons = []
    for check_fn in (check_liquidity_pull, check_whale_dump, check_sell_pressure, check_fast_dump):
        result = check_fn(mint, cfg_onchain)
        if result:
            reasons.append(result)
    return reasons


def reset_history(mint: str) -> None:
    """Call after closing a position so a re-entry on the same mint starts clean."""
    _history.pop(mint, None)
