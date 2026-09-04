#!/usr/bin/env python3
"""
meteora_discovery.py — Jiro data source adapter using Meteora's free
Pool Discovery API (the same one meridian uses).

Why this is much better than DexScreener search for our sim:
  - Free, no auth, no rate limit issues
  - 211,652+ Meteora DLMM pools indexed
  - Native metrics: TVL, volume, fees, swap_count, unique_traders,
    volatility, organic_score, holder risk
  - timeframes: 5m / 1h / 24h (filter feeds the same metrics)
  - Sort by: tvl, volume, fee, organic_score, etc.
  - Filter combinator: && (AND), e.g. "tvl>1000&&swap_count>10"

Reference: meridian's tools/screening.js — this adapter mirrors that
pattern but exposes a clean Python API for the dry-run simulator.

Usage:
    ./venv/bin/python meteora_discovery.py --limit 20
    ./venv/bin/python meteora_discovery.py --list-recent --min-tvl 5000 --min-swaps 50
"""

from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import List, Dict, Any, Optional, Iterator

# Public Meteora endpoints (no auth required)
POOL_DISCOVERY_BASE = "https://pool-discovery-api.datapi.meteora.ag"
DLMM_DATAPI = "https://dlmm.datapi.meteora.ag"
JUP_DATAPI = "https://datapi.jup.ag/v1"

# Default 5m timeframe matches what meridian screener uses
DEFAULT_TIMEFRAME = "5m"
DEFAULT_CATEGORY = "trending"
DEFAULT_PAGE_SIZE = 50

USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"


def _http_get(url: str, timeout: int = 20) -> Dict[str, Any]:
    """GET with browser-like headers. Returns parsed JSON or {}."""
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", errors="ignore"))
    except Exception as e:
        # Surface as a structured error so callers can decide
        return {"_error": str(e), "_url": url}


def iter_pools(
    *,
    filters: str = "tvl>1000",
    timeframe: str = DEFAULT_TIMEFRAME,
    category: str = DEFAULT_CATEGORY,
    page_size: int = DEFAULT_PAGE_SIZE,
    max_pages: Optional[int] = None,
    sleep_s: float = 0.3,
) -> Iterator[Dict[str, Any]]:
    """Yield pools page-by-page from Meteora's discovery API.

    Combines the `after_key` pagination cursor so we can scan tens of
    thousands of pools without manual paging. Stops on error or after
    `max_pages` (None = no cap).
    """
    page_count = 0
    after_key: Optional[str] = None
    while True:
        if max_pages is not None and page_count >= max_pages:
            return
        params = {
            "page_size": page_size,
            "filter_by": filters,
            "timeframe": timeframe,
            "category": category,
        }
        if after_key:
            params["after_key"] = after_key
        url = f"{POOL_DISCOVERY_BASE}/pools?{urllib.parse.urlencode(params)}"
        data = _http_get(url)
        if not data or "_error" in data:
            return
        pools = data.get("data") or []
        for p in pools:
            yield p
        after_key = data.get("after_key")
        if not data.get("has_more") or not after_key:
            return
        page_count += 1
        if sleep_s > 0:
            time.sleep(sleep_s)


def list_pools(
    *,
    filters: str = "tvl>1000",
    timeframe: str = DEFAULT_TIMEFRAME,
    category: str = DEFAULT_CATEGORY,
    limit: int = 50,
    page_size: int = DEFAULT_PAGE_SIZE,
    max_pages: int = 1,
) -> List[Dict[str, Any]]:
    """Flat list of up to `limit` pools. Capped at max_pages to keep the
    call fast — use iter_pools() directly for deeper scans."""
    out: List[Dict[str, Any]] = []
    for p in iter_pools(
        filters=filters, timeframe=timeframe, category=category,
        page_size=page_size, max_pages=max_pages,
    ):
        out.append(p)
        if len(out) >= limit:
            break
    return out


def _mint_of_token_side(pool: Dict[str, Any], side: str = "base") -> Optional[str]:
    """Return the mint address of the base/quote token of the pool.

    Meridian convention: base = token with non-stable symbol (the "trade"
    side); quote = SOL or USDC. The Meteora API doesn't label which is
    base, so we infer: SOL (So11...) or USDC = quote, anything else = base.
    """
    SOL = "So11111111111111111111111111111111111111112"
    STABLES = {
        SOL,
        "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC
        "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",  # USDT
    }
    tx = pool.get("token_x") or {}
    ty = pool.get("token_y") or {}
    tx_addr = tx.get("address") or ""
    ty_addr = ty.get("address") or ""
    if side == "base":
        # The non-stable side
        if tx_addr and tx_addr not in STABLES:
            return tx_addr
        if ty_addr and ty_addr not in STABLES:
            return ty_addr
        return tx_addr or ty_addr or None
    else:  # quote
        if tx_addr in STABLES:
            return tx_addr
        if ty_addr in STABLES:
            return ty_addr
        return None


def to_candidate(pool: Dict[str, Any]) -> Dict[str, Any]:
    """Reshape a Meteora pool into Jiro's candidate format for trading.

    Key mapped fields:
      - symbol/name from token_x (base) or token_y if x is stable
      - mint from the non-stable side
      - liquidity → liquidity_usd
      - volume → volume_h1 (note: depends on timeframe param)
      - swap_count → txns.h1
      - organic_score → ml_score
      - volatility → price volatility (5m)
    """
    tx = pool.get("token_x") or {}
    ty = pool.get("token_y") or {}
    base_mint = _mint_of_token_side(pool, "base") or ""
    base_token = tx if (tx.get("address") and tx.get("address") != _mint_of_token_side(pool, "quote")) else ty
    symbol = base_token.get("symbol") or pool.get("name", "?").split("-")[0]
    timeframe = "5m"  # this is what we requested; the API label is loose
    return {
        "term": symbol,
        "description": (
            f"meteora pool {pool.get('name','?')}  "
            f"tvl=${pool.get('tvl',0):,.0f}  "
            f"vol={timeframe}=${pool.get('volume',0):,.0f}  "
            f"swaps={pool.get('swap_count',0)}  "
            f"organic={pool.get('token_x',{}).get('organic_score','?')}"
        ),
        "score": 0.0,  # filled in by scoring function
        "launch": {
            "mint": base_mint,
            "pair_address": pool.get("pool_address", ""),
            "pair_url": f"https://app.meteora.ag/dlmm/{pool.get('pool_address','')}",
        },
        "is_gap_candidate": True,
        "liquidity_usd": pool.get("tvl", 0),
        "volume_usd": pool.get("volume", 0),
        "swap_count": pool.get("swap_count", 0),
        "unique_traders": pool.get("unique_traders", 0),
        "fee_tvl_ratio": pool.get("fee_tvl_ratio", 0),
        "volatility": pool.get("volatility", 0),
        "price_change_pct": pool.get("pool_price_change_pct", 0),
        "organic_score": tx.get("organic_score", 0),
        "holders": tx.get("holders", 0),
        "dev_balance_pct": tx.get("dev_balance_pct", 0),
        "is_blacklisted": pool.get("is_blacklisted", False),
        "source": "meteora_discovery",
        "_pool": pool,  # keep raw for debugging
    }


def score_pool(pool: Dict[str, Any]) -> float:
    """Cheap 0-10 score that mirrors Jiro's combine(entry_filters, on-chain).

    Components:
      - liquidity fit 0-2:    peak at $5k–$20k range
      - vol/liquidity 0-2:    higher turnover = better
      - swap activity 0-2:    50+ swaps in window = 2
      - organic 0-2:          higher organic_score_label = better
      - volatility 0-2:       moderate volatility = best (extreme = rug/flat)
    """
    tvl = pool.get("tvl", 0) or 0
    if tvl <= 0:
        return 0.0
    # liquidity fit — peak at $10k
    if 3000 <= tvl <= 80000:
        liq_score = 2.0
    elif 1000 <= tvl <= 200000:
        liq_score = 1.0
    else:
        liq_score = 0.0
    # vol/tvl
    vol = pool.get("volume", 0) or 0
    vol_liq = vol / tvl
    vol_score = min(2.0, vol_liq)
    # swap activity
    swaps = pool.get("swap_count", 0) or 0
    if swaps >= 50:
        swap_score = 2.0
    elif swaps >= 10:
        swap_score = 1.0
    else:
        swap_score = 0.0
    # organic (0-100 scale)
    org = (pool.get("token_x") or {}).get("organic_score", 0) or 0
    org_score = min(2.0, org / 30)
    # volatility: sweet spot is 0.5-3.0, extreme either way = bad
    vol5 = pool.get("volatility", 0) or 0
    if 0.5 <= vol5 <= 3.0:
        vol_score2 = 2.0
    elif 0.1 <= vol5 <= 5.0:
        vol_score2 = 1.0
    else:
        vol_score2 = 0.0
    return round(liq_score + vol_score + swap_score + org_score + vol_score2, 2)


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------

def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Meteora pool discovery adapter")
    ap.add_argument("--limit", type=int, default=20, help="number of pools to return")
    ap.add_argument("--min-tvl", type=float, default=1000)
    ap.add_argument("--min-swaps", type=int, default=10)
    ap.add_argument("--timeframe", default=DEFAULT_TIMEFRAME, choices=("5m", "1h", "24h"))
    ap.add_argument("--category", default=DEFAULT_CATEGORY, choices=("trending", "new", "volume"))
    ap.add_argument("--max-pages", type=int, default=5)
    ap.add_argument("--list-recent", action="store_true", help="just print top pools as a table")
    ap.add_argument("--save", type=str, default=None, help="optional path to write full JSON")
    args = ap.parse_args()

    # Build a strict filter
    flt_parts = [f"tvl>{args.min_tvl}", f"swap_count>={args.min_swaps}"]
    flt_parts.append("is_blacklisted=false")
    flt_parts.append("base_token_has_critical_warnings=false")
    filters = "&&".join(flt_parts)

    pools = list_pools(
        filters=filters,
        timeframe=args.timeframe,
        category=args.category,
        limit=args.limit,
        max_pages=args.max_pages,
    )
    print(f"meteora returned {len(pools)} pools (filters: {filters})")
    if not pools:
        return 1

    if args.list_recent:
        print(f"\n{'symbol':<14} {'pool':<20} {'tvl':>10} {'vol_5m':>10} {'swaps':>7} {'unique':>7} {'org':>5} {'volat':>6}")
        print("-" * 90)
        for p in pools:
            tx = p.get("token_x", {})
            symbol = (tx.get("symbol") or p.get("name", "?")).split("-")[0][:14]
            print(
                f"{symbol:<14} {p.get('name','')[:20]:<20} "
                f"${p.get('tvl', 0):>9,.0f} ${p.get('volume', 0):>9,.0f} "
                f"{p.get('swap_count', 0):>7} {p.get('unique_traders', 0):>7} "
                f"{(tx.get('organic_score') or 0):>5} {p.get('volatility', 0):>6.2f}"
            )

    if args.save:
        Path(args.save).write_text(json.dumps(pools, indent=2, default=str))
        print(f"\n[ok] saved {len(pools)} pools to {args.save}")

    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
