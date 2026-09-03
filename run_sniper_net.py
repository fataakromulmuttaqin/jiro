#!/usr/bin/env python3
"""
run_sniper_net.py — Jiro Sniper Net end-to-end cron entry point.

Chains all P1-P3 stages for one or many mints:
  1. profile_top_holders.py  → top N wallets' PnL profiles
  2. cabal_detector.py       → cabal clustering
  3. behavior_miner.py       → behavior tagging
  4. watchlist_updater.py    → promote winners / prune losers

Emits:
  - cache/sniper_net_report.json (single-mint) OR
  - cache/sniper_net_batch.json (multi-mint)
  - Telegram alert (if anything cabal-y detected)

USAGE:
    python3 run_sniper_net.py MINT1 [MINT2 ...] [--top-n N] [--no-cache]

This is what the cron job calls.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any, Dict, List

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import profile_top_holders as pth
import cabal_detector as cd
import behavior_miner as bm
import watchlist_updater as wu

DEFAULT_OUT_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "cache"
)


def run_for_mint(
    mint: str,
    top_n: int = 5,
    *,
    use_cache: bool = True,
    update_watchlist: bool = True,
) -> Dict[str, Any]:
    """Full pipeline for one mint. Returns the enriched report dict."""
    # 1. profile
    report = pth.analyze_mint(mint, top_n=top_n, use_cache=use_cache)
    # 2. cabal
    report = cd.analyze_report(report)
    # 3. behavior
    profiles = report.get("top_holders") or []
    classifications = bm.classify_all(profiles)
    profiles = bm.merge_into_profiles(profiles, classifications)
    report["behavior"] = classifications
    report["top_holders"] = profiles
    # 4. watchlist
    if update_watchlist:
        diff = wu.update_from_report(report)
        report["watchlist_diff"] = diff
    return report


def _maybe_alert(report: Dict[str, Any]) -> None:
    """Send a Telegram alert if cabal/suspect clusters found.

    No-op if notifier import fails (e.g. token missing).
    """
    cabal_summary = (report.get("cabal") or {}).get("summary") or {}
    if not cabal_summary.get("n_cabal") and not cabal_summary.get("n_suspect"):
        return
    try:
        from notifier import send_telegram  # local; project's notifier.py
        text = (
            f"🎯 *Jiro Sniper Net*\n"
            f"mint: `{report.get('mint', '?')[:8]}…`\n"
            f"cabals: {cabal_summary.get('n_cabal', 0)} | "
            f"suspects: {cabal_summary.get('n_suspect', 0)}\n"
            f"winners: {cabal_summary.get('n_winners', '?')}\n"
        )
        send_telegram(text)
    except Exception as e:  # noqa: BLE001
        # best-effort — never crash the run
        print(f"alert skipped: {e}", file=sys.stderr)


def _main() -> int:
    p = argparse.ArgumentParser(
        description="Jiro Sniper Net — full pipeline runner"
    )
    p.add_argument("mints", nargs="+", help="Token mint address(es)")
    p.add_argument("--top-n", type=int, default=5,
                   help="Top N holders to profile per mint")
    p.add_argument("--out-dir", default=DEFAULT_OUT_DIR,
                   help="Output directory (default: cache/)")
    p.add_argument("--no-cache", action="store_true",
                   help="Bypass RPC cache (burns Helius credits)")
    p.add_argument("--no-watchlist", action="store_true",
                   help="Skip watchlist update")
    args = p.parse_args()

    if not os.path.isdir(args.out_dir):
        os.makedirs(args.out_dir, exist_ok=True)

    reports: List[Dict[str, Any]] = []
    for mint in args.mints:
        try:
            report = run_for_mint(
                mint,
                top_n=args.top_n,
                use_cache=not args.no_cache,
                update_watchlist=not args.no_watchlist,
            )
            reports.append(report)
            _maybe_alert(report)

            # write single-mint file
            mint_short = mint[:8]
            single_path = os.path.join(args.out_dir, f"sniper_net_{mint_short}.json")
            with open(single_path, "w") as f:
                json.dump(report, f, indent=2)
        except Exception as e:  # noqa: BLE001
            print(f"ERROR processing {mint}: {e}", file=sys.stderr)

    # write batch summary
    batch = {
        "ts": int(time.time()),
        "n_mints": len(reports),
        "reports": [
            {
                "mint": r.get("mint"),
                "summary": (r.get("cabal") or {}).get("summary", {}),
                "n_winners": sum(1 for p in (r.get("top_holders") or []) if p.get("win")),
                "n_losers": sum(1 for p in (r.get("top_holders") or []) if p.get("win") is False),
            }
            for r in reports
        ],
    }
    batch_path = os.path.join(args.out_dir, "sniper_net_batch.json")
    with open(batch_path, "w") as f:
        json.dump(batch, f, indent=2)

    print(json.dumps({
        "n_mints": batch["n_mints"],
        "batch_path": batch_path,
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())