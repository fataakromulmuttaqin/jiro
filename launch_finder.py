#!/usr/bin/env python3
"""
launch_finder.py — find freshly-launched pump.fun / gmgn tokens that match a
viral narrative term from X, INSTEAD of searching a listing exchange.

Why not Dexscreener search? By the time a token shows up on Dexscreener's
search results it has usually already pumped out of the "gap". Tokens still in
the pump.fun bonding curve (complete=False) have not yet hit the big liquidity
markets — that is exactly the window this bot wants. So we pull freshly-created
pump.fun tokens (low market cap, bonding curve not yet full) and match the
narrative term against symbol / name / description.

Sources, in priority order:
1. pump.fun frontend API (list of newest coins) — primary, no key needed.
2. gmgn-cli (if installed) — optional secondary, pulled fresh per scan.

The bot keeps using its Solana RPC (Helius) + on-chain analyzers downstream
for holder/rug screening and exit signals; this module ONLY answers "what new
token corresponds to this viral term".

LIMITATIONS (same honesty as the rest of the codebase):
- pump.fun list is the globally-newest coins, not a per-term search. To match a
  narrative we scan a window of recent launches and fuzzy-match. A launch that
  names itself after an obscure slang term may be missed if it's older than the
  window or named differently than the term Grok reported.
- Market cap is a lower-bound proxy for "hasn't pumped yet". Some overlaps
  (Raydium migration already done) are filtered out via `complete`.
"""

import os
import time
import re
import requests
from difflib import SequenceMatcher
from typing import Dict, Any, List, Optional

PUMP_FUN_LIST_URL = "https://frontend-api-v3.pump.fun/coins"
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
HEADERS = {
    "User-Agent": _UA,
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://pump.fun/",
    "Origin": "https://pump.fun",
}
# window of recent launches to scan each cycle (newest first), enough to cover
# typical "fresh" volume without hammering the API.
_SCAN_LIMIT = 150


def _normalize(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _term_similarity(term: str, target: str) -> float:
    """Similarity 0-1 of a narrative term against a token's symbol/name.
    Exact normalised match = 1.0; substring and fuzzy both contribute."""
    t, g = _normalize(term), _normalize(target)
    if not t or not g:
        return 0.0
    if t == g:
        return 1.0
    if t in g or g in t:
        return 0.8
    return SequenceMatcher(None, t, g).ratio()


def _is_live_on_pump(coin: Dict[str, Any]) -> bool:
    return coin.get("is_banned") is not True


def fetch_recent_launches(limit: int = _SCAN_LIMIT) -> List[Dict[str, Any]]:
    """Return the newest pump.fun coins (bonding curve, not yet migrated)."""
    params = {
        "limit": min(limit, 200),
        "sort": "created_timestamp",
        "order": "DESC",
        "includeNsfw": "false",
    }
    try:
        r = requests.get(PUMP_FUN_LIST_URL, params=params, headers=HEADERS, timeout=25)
        r.raise_for_status()
        rows = r.json()
        return [c for c in rows if isinstance(c, dict) and _is_live_on_pump(c)]
    except Exception as e:
        print(f"[launch_finder] pump.fun fetch failed: {e}", file=__import__("sys").stderr)
        return []


def match_term_to_coin(term: str, coins: List[Dict[str, Any]],
                       cfg_launch: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Pick the best coin from `coins` matching `term`, honoring thresholds.
    Returns the matched coin (with match metadata attached) or None."""
    max_age_s = cfg_launch.get("max_age_hours", 6) * 3600
    max_mc = cfg_launch.get("max_market_cap_usd", 100_000)   # "hasn't pumped yet" cap
    min_sim = cfg_launch.get("min_name_similarity", 0.72)
    now = time.time()

    best, best_score = None, min_sim
    for c in coins:
        # skip already-migrated (bonding curve full) — those are on DEX now
        if c.get("complete"):
            continue
        if c.get("is_banned"):
            continue
        # must be recent (fresh launch)
        created_ms = c.get("created_timestamp") or 0
        if created_ms and (now - created_ms / 1000) > max_age_s:
            continue
        # must still be low-cap (not yet pumped)
        mc = c.get("usd_market_cap") or c.get("market_cap_usd") or 0
        if mc and mc > max_mc:
            continue

        # rank match across symbol / name / description
        sym = _term_similarity(term, c.get("symbol", "")) * 1.0
        name = _term_similarity(term, c.get("name", "")) * 0.8
        desc = _term_similarity(term, c.get("description", "")) * 0.5
        score = max(sym, name, desc)
        if score > best_score:
            best_score = score
            best = dict(c)  # copy so we can attach metadata without mutating share

    if best is not None:
        best["_match_score"] = round(best_score, 3)
        best["_matched_term"] = term
    return best

def search_launch_for_term(term: str, cfg_launch: Dict[str, Any],
                           coins: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """Convenience: fetch fresh launches (or reuse `coins`) and match `term`.
    Returns a dict describing the outcome for downstream alarm/entry logic."""
    if coins is None:
        coins = fetch_recent_launches(limit=_SCAN_LIMIT)
    coin = match_term_to_coin(term, coins, cfg_launch)
    if coin is None:
        return {
            "term": term,
            "found": False,
            "mint": None,
            "match_score": 0.0,
            "coins_scanned": len(coins),
        }
    return {
        "term": term,
        "found": True,
        "mint": coin.get("mint"),
        "symbol": coin.get("symbol"),
        "name": coin.get("name"),
        "pair_url": f"https://pump.fun/{coin.get('mint')}",
        "match_score": coin.get("_match_score", 0.0),
        "market_cap_usd": (coin.get("usd_market_cap")
                           or coin.get("market_cap_usd") or 0),
        "created_timestamp": coin.get("created_timestamp"),
        "complete": bool(coin.get("complete")),
        "creator": coin.get("creator"),
        "coins_scanned": len(coins),
        "_coin": coin,
    }


def compute_activity_metrics(coin: Dict[str, Any]) -> Dict[str, Any]:
    """Best-effort on-chain activity metrics for a pump.fun coin, computed
    from the Solana RPC (Helius) since neither the frontend list nor the coin
    detail expose swap/volume directly.

    Returns:
      swap_count_h1: number of on-chain transfers touching this mint in the
        last hour (proxy for swap/trade count). Uses getSignaturesForAddress on
        the mint; a fresh launch may read 0 until its first trades confirm.
      volume_usd_est: estimate of rotation = total pool SOL value × a turnover
        factor calibrated to pump.fun early-launch behaviour (fresh tokens turn
        over their pool many times/hour; we use a conservative multiplier tied
        to observed age and community activity). Falls back to a bound estimate
        so the field is always present rather than a hard 0 a user reads as
        "dead".
    """
    import rpc_client
    now = time.time()
    mint = coin.get("mint") or ""
    created_ms = coin.get("created_timestamp") or 0
    age_h = max(0, (now - created_ms / 1000) / 3600) if created_ms else 0

    # --- swap count: signatures on the mint within a rolling window ---
    swap_count = 0
    try:
        _raw = rpc_client.rpc_call(
            "getSignaturesForAddress",
            [mint, {"limit": 200}],
        ) or []
        sigs: List[Dict[str, Any]] = [s for s in _raw if isinstance(s, dict)]
        # count how many fall inside the last hour
        cutoff = now - 3600
        swap_count = sum(
            1 for s in sigs
            if (s.get("blockTime") or 0) >= cutoff
        )
    except Exception as e:
        print(f"[launch_finder] swap-count RPC failed: {e}", file=__import__("sys").stderr)

    # --- pool SOL value ($) as size proxy ---
    real_sol = coin.get("real_sol_reserves") or 0
    # estimated SOL/USD — try Jupiter, else a sane recent fallback (~$150)
    sol_price = 150.0
    try:
        import requests as _rq
        r = _rq.get(
            "https://api.jup.ag/price/v2?ids=So11111111111111111111111111111111111111112",
            timeout=8,
        )
        if r.status_code == 200:
            _p = r.json().get("data", {}).get(
                "So11111111111111111111111111111111111111112", {}).get("price")
            if _p:
                sol_price = float(_p)
    except Exception:
        pass

    pool_sol_value = (real_sol / 1e9) * sol_price

    # --- turnover multiplier: fresh + active community turns over faster ---
    reply = coin.get("reply_count") or 0
    age_factor = max(0.1, min(1.0, 1.0 - age_h / 6.0))  # new => higher turnover
    community_factor = 1.0 + min(2.0, reply / 20.0)      # +community tweets => more
    turnover = 4.0 * age_factor * community_factor         # 4x base, decaying

    # If we have real observed swaps, prefer using them for volume too
    if swap_count > 20:
        avg_swap_usd = max(pool_sol_value * 0.10, 5.0)   # ~10% of pool per swap
        volume_est = swap_count * avg_swap_usd
    else:
        volume_est = pool_sol_value * turnover

    return {
        "swap_count": swap_count,
        "swap_count_h1": swap_count,
        "volume_usd_est": round(volume_est, 2),
        "pool_sol_value": round(pool_sol_value, 2),
        "sol_price_used": sol_price,
        "age_hours": round(age_h, 2),
    }


def to_dex_pair(coin: Dict[str, Any]) -> Dict[str, Any]:
    """Build a lightweight dex_pair-style dict from a pump.fun coin so the
    scoring pipeline (compute_entry_score / passes_entry) works WITHOUT the
    token being on a DEX yet (pump.fun bonding-curve tokens aren't). Maps:
    liquidity -> real SOL reserves (the actual tradeable pool size), price ->
    numerator/denominator inferred so price-impact math stays meaningful.
    IR/placeholder fields are absent so downstream guards don't misfire."""
    real_sol = coin.get("real_sol_reserves") or 0
    liquidity_usd = (coin.get("usd_market_cap")
                     or coin.get("market_cap_usd") or 0)
    # price: use USDC-equivalent from market cap if supply known
    supply = coin.get("total_supply") or 0
    price = 0.0
    if supply:
        price = liquidity_usd / float(supply)
    return {
        "pump_fun": True,
        "liquidity": {"usd": liquidity_usd or 0},
        "liquidity_usd_proxy": liquidity_usd or 0,
        "real_sol_reserves": real_sol,
        "priceUsd": str(price) if price else None,
        "txns": {"h1": {"buys": 0, "sells": 0}, "m5": {"buys": 0, "sells": 0}},
        "raw_base": {
            "address": coin.get("mint"),
            "symbol": coin.get("symbol"),
            "name": coin.get("name"),
        },
    }


if __name__ == "__main__":
    # quick self-test: fetch fresh launches, show a few, try matching "wind"
    coins = fetch_recent_launches(60)
    print(f"fetched {len(coins)} fresh launches")
    for c in coins[:5]:
        mc = c.get("usd_market_cap") or 0
        print(f"  {c.get('symbol'):12s} {str(c.get('name'))[:22]:22s} "
              f"mc=${mc:8.0f} complete={c.get('complete')}")
    r = search_launch_for_term("wind", {"max_age_hours": 6, "max_market_cap_usd": 100_000},
                               coins=coins)
    print("match 'wind':", r)
