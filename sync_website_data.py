#!/usr/bin/env python3
"""
sync_website_data.py — copy Jiro Sniper Net JSON output into the website's
public/data/ directory and generate manifest.json.

Run this after each pipeline run:
    cd ~/ruangkerja/jiro
    python3 sync_website_data.py [--cabal-seed path] [--watchlist path]

OUTPUT:
    website/public/data/
      ├── manifest.json
      ├── sniper_net_<mint8>.json   (one per analyzed mint)
      ├── sniper_net_batch.json     (latest batch summary)
      ├── watchlist.json            (current smart money watchlist)
      └── watchlist_diff.json       (rolling change log)

This script is what `run_sniper_net.py` should call at the end of each run
to keep it in sync. After sync, run `vercel --prod` from website/ to deploy.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from typing import Any, Dict, List

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
WEBSITE_DATA = os.path.join(THIS_DIR, "website", "public", "data")
JI_CACHE = os.path.join(THIS_DIR, "cache")

WATCHLIST_DEFAULT = os.path.join(THIS_DIR, "watchlist.json")
CABAL_SEED_DEFAULT = os.path.join(THIS_DIR, "cabal_seeds.json")  # user-supplied, optional


def _ensure_data_dir() -> None:
    if not os.path.isdir(WEBSITE_DATA):
        os.makedirs(WEBSITE_DATA, exist_ok=True)


def _safe_copy(src: str, dst_name: str) -> bool:
    """Copy src → website/public/data/dst_name. Return True if copied."""
    dst = os.path.join(WEBSITE_DATA, dst_name)
    if not os.path.exists(src):
        return False
    try:
        shutil.copy2(src, dst)
        return True
    except OSError as e:
        print(f"copy failed {src} → {dst}: {e}", file=sys.stderr)
        return False


def _generate_manifest() -> int:
    """Walk website/public/data/, list all sniper_net_*.json files,
    build manifest.json. Returns number of mints listed."""
    mints: List[Dict[str, Any]] = []
    for fname in os.listdir(WEBSITE_DATA):
        if not (fname.startswith("sniper_net_") and fname.endswith(".json")):
            continue
        if fname == "sniper_net_batch.json":
            continue
        if fname == "sniper_net_with_cabal.json":
            continue
        if fname == "sniper_net_with_behavior.json":
            continue
        path = os.path.join(WEBSITE_DATA, fname)
        try:
            with open(path) as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict) or "mint" not in data:
            continue
        cabal_sum = (data.get("cabal") or {}).get("summary") or {}
        winners = sum(1 for p in (data.get("top_holders") or []) if p.get("win") is True)
        mints.append({
            "mint": data["mint"],
            "file": fname,
            "analyzed_at": data.get("ts", 0),
            "n_holders": cabal_sum.get("n_wallets", len(data.get("top_holders", []))),
            "n_cabal": cabal_sum.get("n_cabal", 0),
            "n_winners": winners,
        })
    mints.sort(key=lambda m: m["analyzed_at"], reverse=True)

    manifest = {
        "generated_at": int(time.time()),
        "mints": mints,
    }
    manifest_path = os.path.join(WEBSITE_DATA, "manifest.json")
    with open(manifest_path, "w") as f:
        json.dump(manifest, f, indent=2)
    return len(mints)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Sync Jiro Sniper Net JSON output → website/public/data/"
    )
    parser.add_argument(
        "--cabal-seed", default=CABAL_SEED_DEFAULT,
        help="Path to cabal_seeds.json (funder_addr → cabal_name)",
    )
    parser.add_argument(
        "--watchlist", default=WATCHLIST_DEFAULT,
        help="Path to smart_money watchlist.json",
    )
    parser.add_argument(
        "--cache-dir", default=JI_CACHE,
        help="Jiro cache directory (default: ./cache)",
    )
    args = parser.parse_args()

    _ensure_data_dir()

    copied = 0

    # 1. sniper_net_<mint8>.json files from cache/
    if os.path.isdir(args.cache_dir):
        for fname in os.listdir(args.cache_dir):
            if fname.startswith("sniper_net_") and fname.endswith(".json"):
                if fname in ("sniper_net_batch.json", "sniper_net_with_cabal.json",
                             "sniper_net_with_behavior.json"):
                    # these get copied too, just by different rules below
                    pass
                src = os.path.join(args.cache_dir, fname)
                if _safe_copy(src, fname):
                    copied += 1

    # 2. sniper_net_batch.json
    src = os.path.join(args.cache_dir, "sniper_net_batch.json")
    if _safe_copy(src, "sniper_net_batch.json"):
        copied += 1

    # 3. cabal_seeds.json (if exists)
    if os.path.exists(args.cabal_seed):
        if _safe_copy(args.cabal_seed, "cabal_seeds.json"):
            copied += 1
            print(f"loaded cabal seed DB: {args.cabal_seed}")

    # 4. watchlist.json + diff log
    if os.path.exists(args.watchlist):
        if _safe_copy(args.watchlist, "watchlist.json"):
            copied += 1
    diff_log = os.path.join(args.cache_dir, "watchlist_diff.json")
    if _safe_copy(diff_log, "watchlist_diff.json"):
        copied += 1

    # 5. Generate manifest
    n_mints = _generate_manifest()

    print(f"synced {copied} files · {n_mints} mints in manifest → {WEBSITE_DATA}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())