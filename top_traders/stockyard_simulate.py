#!/usr/bin/env python3
"""
stockyard_simulate.py — Jiro read-only simulator for Stockyard tokens.

The main simulate_dryrun.py simulates Solana memecoin trading via
Jupiter routing. Stockyard is Robinhood/Base chain (EVM) — different
infrastructure entirely. We don't try to fake a swap.

What this DOES simulate:
  - Read each stockyard token's full state (price, volume, traders,
    top wallets, change% across windows)
  - Score "smart money" wallets by win rate (sell_30d > 0 indicates
    they took profit) + total volume
  - Show what Jiro's cabal_seeds integration picks up

It's a metrics dashboard, not a PnL simulator — but it shows real
data so we can compare stockyard tokens side-by-side.

Usage:
    ./venv/bin/python stockyard_simulate.py
    ./venv/bin/python stockyard_simulate.py --ticker AAPL
    ./venv/bin/python stockyard_simulate.py --top 5
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import List, Dict, Any

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))

import stockyard
import chart_zone


def load_tokens() -> List[Dict[str, Any]]:
    """Load all harvested stockyard token files."""
    # Stockyard writes to top_traders/stockyard/output/ (matches
    # smart_wallet_sync's STOCKYARD_OUTPUT path).
    out_dir = _HERE / "stockyard" / "output"
    tokens = []
    if not out_dir.exists():
        return tokens
    for p in sorted(out_dir.glob("*_traders.json")):
        try:
            data = json.load(open(p))
            tokens.append(data)
        except Exception:
            continue
    return tokens


def load_chartzone_tokens(chain: str = "solana") -> List[Dict[str, Any]]:
    out_dir = _HERE / "chart_zone" / "output" / chain
    if not out_dir.exists():
        return []
    return [json.load(open(p)) for p in sorted(out_dir.glob("*_traders.json"))]


def smart_money_score(token: Dict[str, Any]) -> List[Dict[str, Any]]:
    """For each top trader, compute a 'smart money' score:
    - sells / (buys + sells)        : close rate
    - volume / age in years       : activity density
    - position_type == "Long"      : bias (Long = more confident)
    Higher = better signal.
    """
    out = []
    for t in token.get("top_traders", []):
        ex = t.get("extras", {})
        buys = t.get("buy_30d", 0) or 0
        sells = t.get("sell_30d", 0) or 0
        vol = t.get("volume_30d", 0) or 0
        age = ex.get("wallet_age_years", 0) or 0
        total_tx = buys + sells
        close_rate = (sells / total_tx) if total_tx > 0 else 0
        activity_density = vol / max(age, 0.1)
        pos_bonus = 1.0 if ex.get("position_type") == "Long" else 0.0
        score = (close_rate * 30) + min(activity_density / 1000, 5) + pos_bonus
        out.append({
            "wallet": t.get("wallet_address", ""),
            "label": t.get("label", "?"),
            "buys": buys,
            "sells": sells,
            "volume_usd": vol,
            "liquidity_usd": ex.get("liquidity_provided_usd", 0),
            "wallet_age_years": age,
            "position_type": ex.get("position_type", "?"),
            "close_rate": round(close_rate, 3),
            "score": round(score, 2),
        })
    out.sort(key=lambda r: -r["score"])
    return out


def render_stockyard_table(tokens: List[Dict[str, Any]], top: int) -> str:
    """Pretty-print stockyard tokens by volume + smart-money density."""
    out = []
    out.append(f"\n=== STOCKYARD ({len(tokens)} tokens) ===\n")
    out.append(f"{'ticker':<8} {'price':>10} {'chg%':>7} {'traders':>8} {'top1_score':>10} {'vol_24h':>14}")
    out.append("-" * 70)
    rows = []
    for t in tokens:
        ticker = t.get("ticker", "?").upper()
        try:
            price = float(t.get("price_usd", 0) or 0)
        except (TypeError, ValueError):
            price = 0
        try:
            chg = float(t.get("change_pct", 0) or 0)
        except (TypeError, ValueError):
            chg = 0
        traders = t.get("traders", 0) or 0
        try:
            vol = float(t.get("volume_usd_24h", 0) or 0)
        except (TypeError, ValueError):
            vol = 0
        smart = smart_money_score(t)
        top1 = smart[0]["score"] if smart else 0
        rows.append((ticker, price, chg, traders, top1, vol, t))
    # Sort by smart-money top1 * volume (rough combined signal)
    rows.sort(key=lambda r: -(r[4] * (r[5] ** 0.5)))
    for ticker, price, chg, traders, top1, vol, t in rows[:top]:
        out.append(
            f"{ticker:<8} ${price:>9,.2f} {chg:>6.1f}% {traders:>8} {top1:>10.2f} ${vol:>13,.0f}"
        )
    return "\n".join(out)


def render_chartzone_table(tokens: List[Dict[str, Any]], top: int) -> str:
    out = []
    out.append(f"\n=== CHART.ZONE /markets/solana ({len(tokens)} tokens) ===\n")
    out.append(f"{'pos':>4} {'name':<28} {'url':<58}")
    out.append("-" * 95)
    for t in tokens[:top]:
        out.append(
            f"{t.get('position', 0):>4} {t.get('name', '?')[:26]:<28} "
            f"https://chart.zone/solana/{t.get('mint', t.get('address', '?'))[:50]:<48}"
        )
    return "\n".join(out)


def render_cabal_seeds_status() -> str:
    """Show how many of the discovered smart-money wallets are now in
    cabal_seeds (cross-reference)."""
    seeds_path = _HERE.parent / "cabal_seeds.json"
    meta_path = _HERE.parent / "cabal_seeds.meta.json"
    if not seeds_path.exists():
        return "(no cabal_seeds.json)"
    seeds = json.load(open(seeds_path))
    meta = json.load(open(meta_path)) if meta_path.exists() else {}

    by_source = {}
    for addr, m in meta.items():
        s = m.get("source", "?")
        by_source[s] = by_source.get(s, 0) + 1
    out = [f"\n=== CABAL_SEEDS STATUS ===",
           f"  total entries: {len(seeds)}",
           f"  by source: {by_source}"]
    return "\n".join(out)


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Stockyard/chart.zone read-only simulator")
    ap.add_argument("--ticker", help="show top traders for a specific ticker")
    ap.add_argument("--top", type=int, default=15, help="how many rows to show")
    args = ap.parse_args()

    tokens = load_tokens()
    cz_tokens = load_chartzone_tokens("solana")

    if args.ticker:
        # Deep-dive on one ticker. get_top_traders returns RAW stockyard
        # maker rows (s, a, l, v, tx, age, lp). Convert them to the
        # harvester's normalized shape so smart_money_score() works.
        raw_makers = stockyard.get_top_traders(args.ticker, limit=20, source="map")
        if not raw_makers:
            print(f"[!] ticker '{args.ticker}' not found")
            return 1
        makers = [{
            "wallet_address": m.get("a", ""),
            "label": m.get("s", "?"),
            "buy_30d": m.get("tx", 0) or 0,
            "sell_30d": 0,
            "volume_30d": m.get("v", 0) or 0,
            "pnl_30d": None,
            "extras": {
                "liquidity_provided_usd": m.get("l", 0) or 0,
                "wallet_age_years": m.get("age", 0) or 0,
                "position_type": m.get("lp", "?"),
            },
        } for m in raw_makers]
        smart = smart_money_score({"top_traders": makers})
        print(f"\n=== TOP TRADERS for ${args.ticker.upper()} (sorted by smart-money score) ===")
        print(f"{'#':>3} {'label':<10} {'wallet':<14} {'buys':>5} {'sells':>5} {'vol':>10} {'age':>5} {'pos':<10} {'score':>6}")
        print("-" * 90)
        for i, m in enumerate(smart, 1):
            print(
                f"{i:>3} {m['label'][:10]:<10} {m['wallet'][:12]:<14} "
                f"{m['buys']:>5} {m['sells']:>5} ${m['volume_usd']:>9,.0f} "
                f"{m['wallet_age_years']:>5.1f} {m['position_type'][:10]:<10} {m['score']:>6.2f}"
            )
        return 0

    print(render_stockyard_table(tokens, args.top))
    print(render_chartzone_table(cz_tokens, args.top))
    print(render_cabal_seeds_status())
    return 0


if __name__ == "__main__":
    sys.exit(main())
