#!/usr/bin/env python3
"""
harvest_top_traders.py — Jiro source harvester.

Aggregates the highest-value top-trader signals from ALL of jiro's
external feeds (api_adapter, gmgn_scraper, onchain, Meteora, stockyard,
chart.zone) and writes per-source output JSON files that
smart_wallet_sync.py can ingest.

Why this layer:
  - smart_wallet_sync expects a stable per-source output schema; each
    source's adapter (api_adapter, onchain, etc.) writes to its own
    `output/{mint}_traders.json`.
  - This harvester does the SAME for the two newest cross-chain feeds
    (stockyard, chart.zone) so they can plug in with zero changes to
    the sync layer.
  - It also batches collection: instead of running 5+ separate
    one-off harvests, you run this once and all output dirs are
    refreshed.

Usage:
    ./venv/bin/python harvest_top_traders.py
    ./venv/bin/python harvest_top_traders.py --source stockyard --source chart_zone
    ./venv/bin/python harvest_top_traders.py --top-tokens 30
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))

import stockyard
import chart_zone

# Each source writes its own _traders.json to:
#   top_traders/{source}/output/
# That path matches what smart_wallet_sync's _load_traders_from_dir() expects.
OUTPUT_ROOT = _HERE  # top_traders/
STOCKYARD_OUT = OUTPUT_ROOT / "stockyard" / "output"
CHARTZONE_OUT = OUTPUT_ROOT / "chart_zone" / "output"
ALL_SOURCES = ("stockyard", "chart_zone")


# ----------------------------------------------------------------------------
# Stockyard harvest
# ----------------------------------------------------------------------------

def _to_trader_row(t: Dict[str, Any], token_ticker: str) -> Dict[str, Any]:
    """Stockyard per-token trader row → jiro's standard trader row."""
    addr = t.get("a", "")
    return {
        "wallet_address": addr,
        "label": t.get("s", "?"),          # trader's symbol/handle
        "buy_30d": int(t.get("tx", 0) or 0),
        "sell_30d": 0,                     # stockyard doesn't split buy/sell
        "volume_30d": float(t.get("v", 0) or 0),
        "pnl_30d": None,                    # not exposed
        "last_active_timestamp": None,       # not exposed
        "source": "stockyard",
        "extras": {
            "liquidity_provided_usd": float(t.get("l", 0) or 0),
            "market_cap_at_entry_usd": float(t.get("mc", 0) or 0),
            "wallet_age_years": float(t.get("age", 0) or 0),
            "position_type": t.get("lp", "?"),
            "dexscreener_url": t.get("u", ""),
            "token_ticker": token_ticker,
        },
    }


def harvest_stockyard(top_n_tokens: int = 20, top_n_traders: int = 15) -> List[Path]:
    """Pull top N tokens from stockyard (by 24h volume), then top N
    traders for each. Writes per-token output files into
    top_traders/output/stockyard/{token_ticker}_traders.json.
    """
    STOCKYARD_OUT.mkdir(parents=True, exist_ok=True)
    # Clear stale
    for f in STOCKYARD_OUT.glob("*_traders.json"):
        f.unlink()
    rows = stockyard.list_tokens(min_volume_usd=1000, limit=top_n_tokens)
    print(f"[harvest:stockyard] {len(rows)} tokens indexed")
    saved: List[Path] = []
    for r in rows:
        ticker = r.get("t", "?").lower()
        if not ticker or ticker == "?":
            continue
        # 0x EVM addresses — note: cabal_seeds uses base58 Solana addrs,
        # so these will only match if we extend the format check.
        addr = r.get("a", "")
        makers = stockyard.get_top_traders(ticker, limit=top_n_traders, source="map")
        out = {
            "mint": addr,
            "ticker": ticker,
            "token_name": r.get("name", ""),
            "chain": "robinhood",
            "fetched_at": int(time.time()),
            "source": "stockyard",
            "price_usd": r.get("p", 0) or 0,
            "traders": r.get("n", 0) or 0,
            "change_pct": r.get("c", 0) or 0,
            "volume_usd_24h": (r.get("vs") or [0])[-1] or 0,
            "top_traders": [_to_trader_row(m, ticker) for m in makers],
        }
        out_path = STOCKYARD_OUT / f"{ticker}_traders.json"
        out_path.write_text(json.dumps(out, indent=2, default=str))
        saved.append(out_path)
    print(f"[harvest:stockyard] wrote {len(saved)} files to {STOCKYARD_OUT}")
    return saved


# ----------------------------------------------------------------------------
# chart.zone harvest
# ----------------------------------------------------------------------------

def _extract_first_address_mint(detail: Dict[str, Any], chain: str, addr: str) -> str:
    """chart.zone addresses are chain-native. For Solana they're base58
    mints; for EVM chains (base, bsc, eth) they're 0x... We return the
    raw address so the smart-wallet layer can decide what to do."""
    return addr


def harvest_chart_zone(chain: str = "solana", top_n_tokens: int = 30) -> List[Path]:
    """Pull top N trending tokens from chart.zone's /markets/{chain}
    endpoint (richer than scraping /markets/{chain} HTML). Then
    writes per-token output files with market data + address.

    Per-token 'top traders' list is rendered client-side via
    Birdeye/DexScreener embed on the token detail page, not exposed
    via the public API. To capture per-token traders from chart.zone
    would need a browser harness on the token detail page — a
    follow-up task.
    """
    import urllib.request
    out_dir = CHARTZONE_OUT / chain
    out_dir.mkdir(parents=True, exist_ok=True)
    for f in out_dir.glob("*_traders.json"):
        f.unlink()
    # Use the public markets API for richer fields (priceUsd, marketCap,
    # fdv, volume24h/1h, priceChange5m/1h/24h, txns24h).
    url = (
        f"https://chart.zone/api/chart-zone/markets?"
        f"chain={chain}&view=trending&issuer=all&launchpad=all&limit={top_n_tokens}"
    )
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.loads(r.read())
    except Exception as e:
        print(f"[harvest:chart_zone:{chain}] markets API failed: {e}", file=sys.stderr)
        return []
    markets = data.get("markets", [])
    # Filter for the requested chain only (defense — the API ignores chain
    # query sometimes and returns base pairs).
    markets = [m for m in markets if m.get("chainId") == chain][:top_n_tokens]
    print(f"[harvest:chart_zone:{chain}] {len(markets)} markets from API")
    saved: List[Path] = []
    for i, m in enumerate(markets, 1):
        base = m.get("base") or {}
        mint = base.get("address", "")
        if not mint:
            continue
        txns = m.get("txns24h") or {}
        out = {
            "mint": mint,
            "chain": chain,
            "fetched_at": int(time.time()),
            "source": "chart_zone",
            "name": f"{base.get('symbol', '?')} / {m.get('quote',{}).get('symbol','?')}",
            "position": i,
            "dex_id": m.get("dexId", ""),
            "pool_address": m.get("poolAddress", ""),
            "url": f"https://chart.zone/{chain}/{mint}",
            "price_usd": m.get("priceUsd"),
            "price_quote": m.get("priceQuote"),
            "liquidity_usd": m.get("liquidityUsd"),
            "market_cap_usd": m.get("marketCapUsd"),
            "fdv_usd": m.get("fdvUsd"),
            "volume_24h": m.get("volume24h"),
            "volume_1h": m.get("volume1h"),
            "price_change_5m_pct": m.get("priceChange5m"),
            "price_change_1h_pct": m.get("priceChange1h"),
            "price_change_24h_pct": m.get("priceChange24h"),
            "txns_24h_buys": txns.get("buys"),
            "txns_24h_sells": txns.get("sells"),
            "created_at": m.get("createdAt"),
            "labels": m.get("labels", []),
            "implementation": m.get("implementation", ""),
            "data_source": m.get("source", ""),
            "launchpad_id": m.get("launchpadId", ""),
            "top_traders": [],
        }
        short = mint[:16]
        out_path = out_dir / f"{short}_traders.json"
        out_path.write_text(json.dumps(out, indent=2, default=str))
        saved.append(out_path)
    print(f"[harvest:chart_zone:{chain}] wrote {len(saved)} files to {out_dir}")
    return saved


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------

def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Harvest top-trader data from all sources")
    ap.add_argument("--source", action="append", choices=ALL_SOURCES,
                    help=f"which sources to harvest (default: all). choices: {ALL_SOURCES}")
    ap.add_argument("--chain", default="solana",
                    help="chain for chart_zone harvest (default: solana)")
    ap.add_argument("--top-tokens", type=int, default=20,
                    help="how many tokens to harvest per source (default 20)")
    ap.add_argument("--top-traders", type=int, default=15,
                    help="how many top traders per token (default 15)")
    args = ap.parse_args()

    enabled = set(args.source) if args.source else set(ALL_SOURCES)
    print(f"[harvest] enabled sources: {sorted(enabled)}")
    t0 = time.time()
    if "stockyard" in enabled:
        harvest_stockyard(top_n_tokens=args.top_tokens, top_n_traders=args.top_traders)
    if "chart_zone" in enabled:
        harvest_chart_zone(chain=args.chain, top_n_tokens=args.top_tokens)
    print(f"[harvest] done in {time.time() - t0:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
