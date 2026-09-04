#!/usr/bin/env python3
"""
stockyard.py — Jiro data source adapter for stockyard.rhps.fun.

Stockyard is a Robinhood/Base chain memecoin tracker (a "pump.fun for
Robinhood" type platform). It exposes two free public JSON endpoints that
give us token + trader data without any auth:

  /api/map.json   — 451KB snapshot. Rows[] of tokens with:
    t            ticker
    name         full name + chain
    a            token contract address (0x... on Base)
    p            price (USD)
    sl           supply sold
    sv           sold volume (USD)
    ml           max supply
    mv           market value
    n            number of traders
    c            current % change
    cs           4-window change history (e.g. 5m/1h/6h/24h)
    vs           4-window volume history (USD)
    m[]          array of top traders per token, each with:
      s          symbol
      a          wallet address (0x...)
      l          liquidity provided
      v          USD volume traded
      tx         tx count
      cs/vs      change/volume per window
      mc         market cap at entry
      age        wallet age in years
      u          dexscreener URL
      lp         "Long" / "Pair.fund" position type
    orph         orphan tx count
  /data/page.json — same data + extra fields (mmc=FDV, dom=dominance, top=top_liq, st=state)

Both are "free no auth" but note the platform's purpose: these are
ROBINHOOD-CHAIN tokens (0x addresses), not Solana. Use it as a
complementary feed alongside jiro's primary Solana top_traders.

Usage:
    ./venv/bin/python stockyard.py --list-tokens
    ./venv/bin/python stockyard.py --top-traders <ticker> --limit 10
    ./venv/bin/python stockyard.py --snapshot
"""

from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import List, Dict, Any, Optional

API_BASE = "https://stockyard.rhps.fun"
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"

DEFAULT_ENDPOINTS = {
    "map":  f"{API_BASE}/api/map.json",
    "page": f"{API_BASE}/data/page.json",
}


def _http_get(url: str, timeout: int = 20) -> Dict[str, Any]:
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", errors="ignore"))
    except Exception as e:
        return {"_error": str(e), "_url": url}


def fetch_snapshot(source: str = "map") -> List[Dict[str, Any]]:
    """Fetch the full token list. `source` is "map" (lighter, faster) or
    "page" (richer schema with extra fields)."""
    url = DEFAULT_ENDPOINTS.get(source, DEFAULT_ENDPOINTS["map"])
    data = _http_get(url, timeout=30)
    if not data or "_error" in data:
        return []
    return data.get("rows") or []


def list_tokens(
    *,
    min_traders: int = 1,
    min_change_pct: Optional[float] = None,
    max_change_pct: Optional[float] = None,
    min_volume_usd: Optional[float] = None,
    has_traders: bool = True,
    limit: int = 50,
) -> List[Dict[str, Any]]:
    """Filter and return top tokens. `min_change_pct` is the current
    change field (c). Useful for "what's pumping right now" or
    "what's dumping" queries."""
    rows = fetch_snapshot("map")
    out: List[Dict[str, Any]] = []
    for r in rows:
        if r.get("n", 0) < min_traders:
            continue
        c = r.get("c")
        if c is not None:
            if min_change_pct is not None and c < min_change_pct:
                continue
            if max_change_pct is not None and c > max_change_pct:
                continue
        # Latest volume = vs[-1] (4th element typically = 24h)
        latest_vol = 0
        vs = r.get("vs") or []
        if vs:
            latest_vol = vs[-1] or 0
        if min_volume_usd is not None and latest_vol < min_volume_usd:
            continue
        if has_traders and not r.get("m"):
            continue
        out.append(r)
        if len(out) >= limit:
            break
    return out


def get_top_traders(ticker: str, limit: int = 20, source: str = "map") -> List[Dict[str, Any]]:
    """Return the top trader rows for a given ticker symbol."""
    rows = fetch_snapshot(source)
    for r in rows:
        if r.get("t", "").lower() == ticker.lower():
            makers = r.get("m") or []
            # Sort by USD volume desc
            makers.sort(key=lambda m: -(m.get("v") or 0))
            return makers[:limit]
    return []


def to_candidate(token: Dict[str, Any], trader: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Reshape a Stockyard token (+ optional trader) into a Jiro-style candidate.
    Note: addresses are 0x (Robinhood/Base), not Solana base58. Caller
    must filter by chain before treating these as Solana tokens.
    """
    return {
        "term": token.get("t", "?"),
        "description": (
            f"stockyard {token.get('name','?')}  "
            f"ticker={token.get('t')}  "
            f"price=${token.get('p',0)}  "
            f"traders={token.get('n',0)}  "
            f"change={token.get('c',0)}%  "
            f"vol_24h=${(token.get('vs') or [0])[-1]:,.0f}"
        ),
        "score": 0.0,
        "launch": {
            "mint": token.get("a", ""),
            "pair_url": f"https://stockyard.rhps.fun/{token.get('a','')}",
        },
        "is_gap_candidate": True,
        "chain": "robinhood",
        "address": token.get("a", ""),
        "price_usd": token.get("p", 0),
        "traders": token.get("n", 0),
        "change_pct": token.get("c", 0),
        "volume_usd": (token.get("vs") or [0])[-1] or 0,
        "supply_sold": token.get("sl", 0),
        "max_supply": token.get("ml", 0),
        "market_value": token.get("mv", 0),
        "orphan_tx": token.get("orph", 0),
        "smart_money": {"wallets": len(token.get("m") or [])},
        "trader": trader,
        "source": "stockyard",
    }


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------

def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Stockyard token + trader feed")
    ap.add_argument("--list-tokens", action="store_true",
                    help="list top tokens (filter with --min-volume-usd etc)")
    ap.add_argument("--top-traders", metavar="TICKER",
                    help="show top traders for a specific ticker (e.g. COST)")
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--min-volume-usd", type=float, default=None)
    ap.add_argument("--min-change-pct", type=float, default=None,
                    help="only show tokens with current change >= this %% (pumps)")
    ap.add_argument("--max-change-pct", type=float, default=None,
                    help="only show tokens with current change <= this %% (dumps)")
    ap.add_argument("--snapshot", action="store_true",
                    help="save full snapshot to ./stockyard_snapshot.json")
    ap.add_argument("--source", default="map", choices=("map", "page"))
    args = ap.parse_args()

    if args.snapshot:
        rows = fetch_snapshot(args.source)
        Path("stockyard_snapshot.json").write_text(json.dumps(rows, indent=2, default=str))
        print(f"[ok] saved {len(rows)} rows to stockyard_snapshot.json")
        return 0

    if args.top_traders:
        makers = get_top_traders(args.top_traders, limit=args.limit, source=args.source)
        if not makers:
            print(f"[!] ticker '{args.top_traders}' not found or no traders")
            return 1
        print(f"\n=== TOP {len(makers)} TRADERS for ${args.top_traders.upper()} ===")
        print(f"{'symbol':<10} {'wallet':<12} {'liq':>10} {'vol':>10} {'tx':>4} {'age':>5} {'pos':<10}")
        print("-" * 75)
        for m in makers:
            sym = m.get("s", "?")
            addr = (m.get("a", "")[:10] + "…") if m.get("a") else "?"
            liq = m.get("l", 0)
            vol = m.get("v", 0)
            tx = m.get("tx", 0)
            age = m.get("age", 0)
            lp = m.get("lp", "?")
            print(f"{sym:<10} {addr:<12} ${liq:>9,.0f} ${vol:>9,.0f} {tx:>4} {age:>5.1f} {lp:<10}")
        return 0

    if args.list_tokens or True:
        rows = list_tokens(
            min_volume_usd=args.min_volume_usd,
            min_change_pct=args.min_change_pct,
            max_change_pct=args.max_change_pct,
            limit=args.limit,
        )
        print(f"stockyard returned {len(rows)} tokens")
        if not rows:
            return 0
        print(f"\n{'ticker':<8} {'name':<25} {'price':>10} {'chg%':>7} {'traders':>8} {'vol_24h':>12}")
        print("-" * 80)
        for r in rows:
            t = r.get("t", "?")
            name = (r.get("name", "")[:23] + "…") if len(r.get("name", "")) > 25 else r.get("name", "")
            try:
                price = float(r.get("p", 0) or 0)
            except (TypeError, ValueError):
                price = 0.0
            try:
                chg = float(r.get("c", 0) or 0)
            except (TypeError, ValueError):
                chg = 0.0
            try:
                n = int(r.get("n", 0) or 0)
            except (TypeError, ValueError):
                n = 0
            try:
                vol = float((r.get("vs") or [0])[-1] or 0)
            except (TypeError, ValueError):
                vol = 0.0
            print(f"{t:<8} {name:<25} ${price:>9,.2f} {chg:>6.1f}% {n:>8} ${vol:>11,.0f}")

    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
