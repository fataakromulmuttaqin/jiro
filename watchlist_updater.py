#!/usr/bin/env python3
"""
watchlist_updater.py — Jiro Sniper Net module.

Auto-maintain smart_money.py's watchlist based on profiling results:

- PROMOTE winners (pnl_sol > 0, win=True) into the watchlist with an
  auto-generated label like "JSN winner 2026-09-03 +1.234 SOL".
- PRUNE existing watchlist entries that have been observed LOSING on
  recent profiles (e.g. pnl_sol < 0 across N observations).
- NEVER delete an entry with a human-set label (starts with "[manual]")
  or that has been in the watchlist < MIN_AGE_HOURS — protects against
  one bad observation killing a good wallet.

This runs as a cron job after each profile_top_holders run. Output:
updated watchlist.json on disk, plus a small JSON diff log so we know
what changed (for the website "recent updates" tab later).

LIMITATIONS:
- "Win rate" tracking is per-profile observation, not multi-mint
  aggregation. A wallet that wins big on one token and loses on another
  shows up as one observation here. Cross-mint aggregation is in P5.
- The auto-label format is fixed. If you want prettier labels, edit
  `_label_for_winner` or write a separate "label_enhancer" module.
- Promotions respect WATCHLIST_MAX_ENTRIES to keep the watchlist small
  enough for cheap polling (10 wallets × 10 sigs = 100 sigs/cycle).

ENV CONFIG:
  WATCHLIST_PATH          # default: ./watchlist.json (smart_money default)
  WATCHLIST_MAX_ENTRIES   # default: 20
  MIN_WATCHLIST_AGE_HOURS # default: 24
  WATCHLIST_DIFF_PATH     # default: cache/watchlist_diff.json
"""

from __future__ import annotations

import os
import json
import time
import logging
from typing import Any, Dict, List, Optional

log = logging.getLogger("watchlist_updater")

WATCHLIST_PATH = os.environ.get("WATCHLIST_PATH", "watchlist.json")
WATCHLIST_MAX_ENTRIES = int(os.environ.get("WATCHLIST_MAX_ENTRIES", "20"))
MIN_WATCHLIST_AGE_HOURS = int(os.environ.get("MIN_WATCHLIST_AGE_HOURS", "24"))
WATCHLIST_DIFF_PATH = os.environ.get(
    "WATCHLIST_DIFF_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache", "watchlist_diff.json"),
)

# PnL threshold to count as a "winner" worth promoting
WINNER_MIN_PNL_SOL = float(os.environ.get("WINNER_MIN_PNL_SOL", "0.05"))
# Loss threshold to consider pruning (only over multiple observations)
LOSER_MAX_PNL_SOL = float(os.environ.get("LOSER_MAX_PNL_SOL", "-0.05"))


# ---------------------------------------------------------------------------
# Watchlist IO
# ---------------------------------------------------------------------------

def _load_watchlist() -> List[Dict[str, Any]]:
    """Load watchlist.json. Returns [] if missing/corrupt.

    Normalizes entries to: {address, label, added_ts, source}
    """
    if not os.path.exists(WATCHLIST_PATH):
        return []
    try:
        with open(WATCHLIST_PATH, "r") as f:
            raw = json.load(f)
        if not isinstance(raw, list):
            return []
        # Backfill fields for entries that lack them
        now = int(time.time())
        normalized = []
        for entry in raw:
            if not entry.get("address"):
                continue
            normalized.append({
                "address": entry["address"],
                "label": entry.get("label", ""),
                # If added_ts is missing, assume "old enough" so pruning rules work
                "added_ts": entry.get("added_ts", now - (MIN_WATCHLIST_AGE_HOURS + 1) * 3600),
                "source": entry.get("source", "unknown"),
            })
        return normalized
    except (OSError, json.JSONDecodeError) as e:
        log.warning("watchlist load failed: %s", e)
        return []


def _save_watchlist(watchlist: List[Dict[str, Any]]) -> None:
    """Atomic save: write .tmp, then rename. Never partially write."""
    tmp = WATCHLIST_PATH + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(watchlist, f, indent=2)
        os.replace(tmp, WATCHLIST_PATH)
    except OSError as e:
        log.error("watchlist save failed: %s", e)
        raise


# ---------------------------------------------------------------------------
# Promotion / pruning logic
# ---------------------------------------------------------------------------

def _label_for_winner(profile: Dict[str, Any]) -> str:
    """Build a label like 'JSN winner 2026-09-03 +1.234 SOL'."""
    pnl = profile.get("realized_pnl_sol") or 0.0
    ts_str = time.strftime("%Y-%m-%d", time.gmtime(profile.get("_profiled_at") or time.time()))
    return f"JSN winner {ts_str} +{pnl:.3f} SOL"


def _is_manual_label(label: str) -> bool:
    return label.startswith("[manual]") or label.startswith("manual:")


def _promote_winners(
    watchlist: List[Dict[str, Any]],
    profiles: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Add new winners to the watchlist. Skip if already present."""
    existing_addrs = {w["address"] for w in watchlist}
    additions: List[Dict[str, Any]] = []
    for p in profiles:
        addr = p.get("wallet")
        if not addr or addr in existing_addrs:
            continue
        pnl = p.get("realized_pnl_sol") or 0.0
        if not p.get("win") or pnl < WINNER_MIN_PNL_SOL:
            continue
        additions.append({
            "address": addr,
            "label": _label_for_winner(p),
            "added_ts": int(time.time()),
            "source": "sniper_net",
            "added_via_mint": p.get("mint"),
            "_pnl": pnl,  # for sorting below; stripped before save
        })

    if not additions:
        return []

    # Respect WATCHLIST_MAX_ENTRIES (cap total size)
    room = WATCHLIST_MAX_ENTRIES - len(watchlist)
    if room <= 0:
        log.info("watchlist full (%d entries) — skipping %d additions",
                 len(watchlist), len(additions))
        return []
    additions.sort(key=lambda w: -w.get("_pnl", 0.0))
    additions = additions[:room]

    # Strip internal sort key
    for a in additions:
        a.pop("_pnl", None)
    watchlist.extend(additions)
    return additions


def _prune_losers(
    watchlist: List[Dict[str, Any]],
    profiles: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Remove watchlist entries observed LOSING in this profile run.

    Rules to AVOID false positives:
    - Skip manual entries (label starts with [manual])
    - Skip entries added < MIN_WATCHLIST_AGE_HOURS ago
    - Only prune if loser pnl is below LOSER_MAX_PNL_SOL threshold
    """
    if not profiles:
        return []

    losing_addrs = {
        p["wallet"] for p in profiles
        if (p.get("realized_pnl_sol") or 0.0) < LOSER_MAX_PNL_SOL and p.get("win") is False
    }
    if not losing_addrs:
        return []

    cutoff = int(time.time()) - MIN_WATCHLIST_AGE_HOURS * 3600
    survivors: List[Dict[str, Any]] = []
    pruned: List[Dict[str, Any]] = []
    for w in watchlist:
        if (
            w["address"] in losing_addrs
            and not _is_manual_label(w.get("label", ""))
            and w.get("added_ts", 0) <= cutoff
        ):
            pruned.append(w)
        else:
            survivors.append(w)
    # in-place update
    watchlist[:] = survivors
    return pruned


# ---------------------------------------------------------------------------
# Public entry
# ---------------------------------------------------------------------------

def update_from_report(
    report: Dict[str, Any],
    *,
    watchlist: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """
    Apply promotion + pruning to a watchlist using one sniper_net_report.

    Returns a diff: {added: [...], pruned: [...], final_size: int}.
    """
    if watchlist is None:
        watchlist = _load_watchlist()
    before_addrs = {w["address"] for w in watchlist}

    profiles = report.get("top_holders") or []
    added = _promote_winners(watchlist, profiles)
    pruned = _prune_losers(watchlist, profiles)

    diff = {
        "ts": int(time.time()),
        "added": added,
        "pruned": pruned,
        "final_size": len(watchlist),
        "mint": report.get("mint"),
    }

    if added or pruned:
        try:
            _save_watchlist(watchlist)
        except OSError:
            pass  # already logged

    # Append to rolling diff log (for website "recent activity" later)
    try:
        existing: List[Dict[str, Any]] = []
        if os.path.exists(WATCHLIST_DIFF_PATH):
            with open(WATCHLIST_DIFF_PATH, "r") as f:
                existing = json.load(f) if os.path.getsize(WATCHLIST_DIFF_PATH) > 0 else []
        existing.append(diff)
        # keep last 100 diffs
        existing = existing[-100:]
        d = os.path.dirname(WATCHLIST_DIFF_PATH)
        if d and not os.path.isdir(d):
            os.makedirs(d, exist_ok=True)
        with open(WATCHLIST_DIFF_PATH, "w") as f:
            json.dump(existing, f, indent=2)
    except OSError as e:
        log.warning("diff log write failed: %s", e)

    return diff


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _main() -> int:
    import argparse
    p = argparse.ArgumentParser(
        description="Auto-update watchlist from sniper_net_report.json"
    )
    p.add_argument(
        "--report",
        default=os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "cache", "sniper_net_report.json",
        ),
        help="Path to sniper_net_report.json",
    )
    args = p.parse_args()

    if not os.path.exists(args.report):
        print(f"ERROR: report not found: {args.report}", file=sys.stderr)
        return 1

    with open(args.report) as f:
        report = json.load(f)

    diff = update_from_report(report)
    print(json.dumps({
        "added_count": len(diff["added"]),
        "pruned_count": len(diff["pruned"]),
        "final_size": diff["final_size"],
    }, indent=2))
    return 0


if __name__ == "__main__":
    import sys
    raise SystemExit(_main())