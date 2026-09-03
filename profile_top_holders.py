#!/usr/bin/env python3
"""
profile_top_holders.py — end-to-end pipeline runner.

Jiro Sniper Net — given a token mint, profile the top N holders' PnL and
funding sources, then emit a JSON document ready for the website.

This is the cron entry point:
    python3 profile_top_holders.py <MINT> [--top-n N] [--out path.json]

Designed to be SAFE on Helius free tier:
- Top 5 holders × ~50 sigs each ≈ 250 RPC calls ≈ ~300 credits
- One token analysis ≈ 600 credits max (5 holders × ~100 sigs for fund_flow)
- Way under 100K credits/mo. Run every 15min for 100 tokens/mo = safe.

OUTPUT SCHEMA (snipnet_to_website):
{
  "mint": str,
  "ts": int,
  "top_holders": [  # PnL profiles per holder
    {wallet, label, buys_sol, sells_sol, realized_pnl_sol, win, roi_pct, ...}
  ],
  "funders": {  # funder wallet → list of funded wallets
    "funder_addr": ["wallet1", "wallet2", ...]
  },
  "summary": {
    "n_holders": int,
    "n_winners": int,    # win=True
    "n_losers": int,
    "total_pnl_sol": float,
    "shared_funder_count": int  # funders with ≥2 children
  }
}
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import defaultdict
from typing import Any, Dict, List

# Make sibling modules importable when run from any cwd
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import wallet_profiler as wp
import fund_flow as ff
from holder_analyzer import get_top_holders

DEFAULT_TOP_N = 5
DEFAULT_OUT = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "cache", "sniper_net_report.json"
)


def analyze_mint(
    mint: str,
    top_n: int = DEFAULT_TOP_N,
    *,
    use_cache: bool = True,
    holder_provider=None,
) -> Dict[str, Any]:
    """
    Run the full pipeline for one mint.
    """
    if holder_provider is None:
        holder_provider = get_top_holders

    # 1. Get top holders + profile each
    profiles = wp.profile_top_holders(
        mint, top_n=top_n, holder_provider=holder_provider, use_cache=use_cache
    )

    # 2. For each profiled wallet, trace its funder (sequential to respect RPC)
    funders_map: Dict[str, List[str]] = defaultdict(list)
    funder_details: Dict[str, Dict[str, Any]] = {}
    for p in profiles:
        w = p.get("wallet")
        if not w:
            continue
        funder = ff.get_funder(w, use_cache=use_cache)
        if funder:
            funders_map[funder].append(w)
            if funder not in funder_details:
                # only call trace_funder for funder details we haven't seen
                traced = ff._cache_get(w)
                if traced:
                    funder_details[funder] = {
                        "first_seen_as_funder": traced.get("fund_ts"),
                        "sample_amount_sol": traced.get("fund_amount_sol"),
                    }
                else:
                    funder_details[funder] = {}

    # 3. Build summary
    n_winners = sum(1 for p in profiles if p.get("win") is True)
    n_losers = sum(1 for p in profiles if p.get("win") is False)
    total_pnl_sol = sum(p.get("realized_pnl_sol") or 0.0 for p in profiles)
    shared_funders = {f: kids for f, kids in funders_map.items() if len(kids) >= 2}

    return {
        "mint": mint,
        "ts": int(time.time()),
        "top_holders": profiles,
        "funders": dict(funders_map),
        "funder_details": funder_details,
        "summary": {
            "n_holders": len(profiles),
            "n_winners": n_winners,
            "n_losers": n_losers,
            "total_pnl_sol": round(total_pnl_sol, 6),
            "shared_funder_count": len(shared_funders),
            "shared_funders": shared_funders,
        },
    }


def _main() -> int:
    p = argparse.ArgumentParser(
        description="Jiro Sniper Net — profile top holders + funding for a mint."
    )
    p.add_argument("mint", help="Token mint address")
    p.add_argument("--top-n", type=int, default=DEFAULT_TOP_N,
                   help="How many top holders to profile (default 5)")
    p.add_argument("--out", default=DEFAULT_OUT,
                   help="Output JSON path (default: cache/sniper_net_report.json)")
    p.add_argument("--no-cache", action="store_true",
                   help="Bypass cache (use sparingly — burns RPC credits)")
    args = p.parse_args()

    report = analyze_mint(args.mint, top_n=args.top_n, use_cache=not args.no_cache)

    out_path = args.out
    out_dir = os.path.dirname(out_path)
    if out_dir and not os.path.isdir(out_dir):
        os.makedirs(out_dir, exist_ok=True)

    with open(out_path, "w") as f:
        json.dump(report, f, indent=2)

    # Compact summary to stdout
    s = report["summary"]
    print(json.dumps({
        "mint": args.mint,
        "out": out_path,
        "n_holders": s["n_holders"],
        "n_winners": s["n_winners"],
        "n_losers": s["n_losers"],
        "total_pnl_sol": s["total_pnl_sol"],
        "shared_funder_count": s["shared_funder_count"],
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())