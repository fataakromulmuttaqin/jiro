#!/usr/bin/env python3
"""
run_smart_wallet_sync.py — cron wrapper for smart_wallet_sync.py.

Gate: only call the sync if at least one source's `*_traders.json`
file has been modified within the last `--max-age-min` minutes.

This is the entry point the cron job should call — it short-circuits
to a clean no-op when there's no new upstream data, so the actual
sync code never even has to load its aggregator.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
JIRO_ROOT = SCRIPT_DIR.parent.parent

DEFAULT_OUTPUT_DIRS = [
    JIRO_ROOT / "top_traders" / "api_adapter" / "output",
    JIRO_ROOT / "top_traders" / "gmgn_scraper" / "output",
    JIRO_ROOT / "top_traders" / "onchain" / "output",
]


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--max-age-min",
        type=int,
        default=30,
        help="Only sync if a source output was modified within N minutes. Default 30.",
    )
    p.add_argument(
        "--ttl-days",
        type=int,
        default=14,
        help="Forwarded to smart_wallet_sync.py --ttl-days.",
    )
    p.add_argument(
        "--source",
        default="all",
        help="Forwarded to smart_wallet_sync.py --source.",
    )
    p.add_argument(
        "--seed-from-cabal-detector",
        default=None,
        help="Forwarded to smart_wallet_sync.py --seed-from-cabal-detector.",
    )
    p.add_argument(
        "--no-telegram",
        action="store_true",
        help="Skip Telegram even on add/expire.",
    )
    p.add_argument(
        "--force",
        action="store_true",
        help="Skip the freshness gate and always run sync.",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview only; never write cabal_seeds.json or sync report.",
    )
    return p.parse_args()


def _has_fresh_output(max_age_min: int) -> bool:
    cutoff = time.time() - max_age_min * 60
    for d in DEFAULT_OUTPUT_DIRS:
        if not d.exists():
            continue
        for p in d.glob("*_traders.json"):
            try:
                if p.stat().st_mtime >= cutoff:
                    return True
            except OSError:
                continue
    return False


def main() -> int:
    args = _parse_args()

    if not args.force and not _has_fresh_output(args.max_age_min):
        print(
            f"[run_sync] no source output modified in last "
            f"{args.max_age_min}min — no-op",
            file=sys.stderr,
        )
        return 0

    cmd = [
        sys.executable,
        str(SCRIPT_DIR / "smart_wallet_sync.py"),
        f"--ttl-days={args.ttl_days}",
        f"--source={args.source}",
    ]
    if args.no_telegram:
        cmd.append("--no-telegram")
    if args.dry_run:
        cmd.append("--dry-run")
    if args.seed_from_cabal_detector:
        cmd.append(f"--seed-from-cabal-detector={args.seed_from_cabal_detector}")

    print(f"[run_sync] executing: {' '.join(cmd)}", file=sys.stderr)
    return subprocess.call(cmd)


if __name__ == "__main__":
    sys.exit(main())