#!/usr/bin/env python3
"""
behavior_miner.py — Jiro Sniper Net module.

Given a list of wallet profiles (output of wallet_profiler), classify each
wallet into a "behavior archetype" — a single-word tag that captures how
the wallet trades. NO RPC calls — this is pure analytics on data we already
have.

ARCHETYPES (single tag per wallet, ordered by precedence):

| Tag              | Meaning                                                  |
|------------------|----------------------------------------------------------|
| `BUNDLER`        | Massive single-tx buys (suggests scripted bundler wallet) |
| `SNIPER`         | Bought within minutes of token launch (early entry)       |
| `EARLY_EXIT`     | Bought then dumped <30 min later (harvest profit fast)    |
| `DIAMOND_HAND`   | Bought and STILL HOLDS 90%+ (paper-hands risk)            |
| `WHALE`          | Single position > 5 SOL (institutional scale)             |
| `SCALPER`        | Multiple buy+sell round trips, small ROI per cycle        |
| `SWING`          | Held for 1-7 days, took profit at moderate multiple       |
| `EXIT_LIQUIDITY` | Bought late and lost money (sold near bottom)             |
| `LOSER`          | Generic losing wallet                                     |
| `WINNER`         | Generic winning wallet (none of the above apply)          |

We pick the FIRST matching tag from the list above (precedence order),
so a wallet can have at most ONE tag. This keeps the website ranking
column stable — each wallet gets exactly one classification.

LIMITATIONS:
- Archetypes are NOT mutually exclusive in spirit (a WHALE can also be
  a SNIPER), but we pick ONE for display purposes. Real classification
  would be a multi-label prob distribution; this is just the headline.
- "Held for X days" requires comparing first_buy_ts and last_action_ts.
  Both come from profile data. If the wallet has no sells yet, we treat
  "still_holding" as the holding period so far.
- The "token age" (mint age) is NOT known to behavior_miner — we don't
  query it. So "SNIPER" detection uses a simpler proxy: if first_buy_ts
  is within 1h of the wallet's earliest tx in our window, it's a SNIPER
  for THIS profile (relative to other activity).

OUTPUT SCHEMA:
{
  "wallet": str,
  "label": str,
  "tag": str,
  "tag_reason": str,  # human-readable explanation
  "metrics": {        # sub-metrics that drove the classification
    "buy_sell_count": (int, int),
    "held_seconds": int|None,
    "first_buy_age_in_window_s": int|None,
    "still_holds_pct": float,
    "roi_pct": float|None,
  }
}
"""

from __future__ import annotations

import os
import logging
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger("behavior_miner")

# Tunables
SNIPER_WINDOW_S = int(os.environ.get("BEHAVIOR_SNIPER_WINDOW_S", "3600"))   # 1h
EARLY_EXIT_WINDOW_S = int(os.environ.get("BEHAVIOR_EARLY_EXIT_S", "1800"))  # 30 min
DIAMOND_HAND_THRESHOLD = float(os.environ.get("BEHAVIOR_DIAMOND_HAND_PCT", "90"))  # 90%
WHALE_MIN_SOL = float(os.environ.get("BEHAVIOR_WHALE_MIN_SOL", "5.0"))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _held_seconds(p: Dict[str, Any]) -> Optional[int]:
    """Compute approximate hold duration (first_buy_ts to last_action_ts)."""
    fb = p.get("first_buy_ts")
    la = p.get("last_action_ts")
    if not fb or not la or la < fb:
        return None
    return la - fb


def _first_buy_age_in_window(p: Dict[str, Any]) -> Optional[int]:
    """How long after the wallet's earliest tx did it buy this mint?

    Proxy for "sniper behavior": if it bought this token very early in
    its observed history, it's probably a sniper (wallet was created
    specifically for this trade, or this trade was the very first thing).
    """
    fb = p.get("first_buy_ts")
    # We don't have the wallet's first tx in the profile — but tx_count
    # gives us a hint. Use first_buy_ts - earliest_possible_ts as a rough
    # proxy. The earliest the wallet could have transacted in our window
    # is roughly (now - tx_count * avg_block_time), but we don't know
    # avg_block_time. Use a simpler proxy: was first_buy_ts near the
    # edge of our sig window? We don't have window bounds either.
    # Workaround: if buys_ui is concentrated in one tx (mint_tx_count == 1
    # AND first_buy_ts is set), treat that as a single-shot sniper.
    if not fb:
        return None
    # Return None — we don't have enough info without more RPC. The
    # sniper detection below uses buys_ui concentration instead.
    return None


def _count_buys_sells(p: Dict[str, Any]) -> Tuple[int, int]:
    """Approximate number of buy vs sell txs.

    Without inner-instruction parsing, we approximate:
    - buys ≈ number of txs with positive mint_delta_ui
    - sells ≈ number with negative mint_delta_ui
    Since wallet_profiler doesn't store per-tx signs (only aggregated),
    we estimate buys = ceil(buys_ui / first_buy_size) if we had that,
    or fall back to mint_tx_count / 2 split. The simplest approximation:
    if current_balance_ui == 0 and total bought > 0, the wallet fully
    sold — assume sells = 1 if there's 1 buy. Otherwise we can't tell
    precisely, so just report mint_tx_count as "transactions".
    """
    # This is intentionally coarse — we don't have per-tx direction.
    # Use the buys_ui / sells_ui relationship as a heuristic:
    buys_ui = p.get("buys_sol", 0) or 0  # SOL spent, but that's cost not units
    sells_sol = p.get("sells_sol", 0) or 0
    # The profile also tracks `current_balance_ui` — that's the units left.
    # Number of distinct buy/sell events is unknowable without per-tx data.
    # We expose mint_tx_count for context, and report "1+ buys, 1+ sells"
    # if both SOL numbers > 0.
    n_buys = 1 if buys_ui > 0 else 0
    n_sells = 1 if sells_sol > 0 else 0
    return n_buys, n_sells


# ---------------------------------------------------------------------------
# Tag assignment
# ---------------------------------------------------------------------------

def _tag_bundler(p: Dict[str, Any]) -> Optional[Tuple[str, str]]:
    """Bundler = single huge tx that bought a lot.

    Proxy: mint_tx_count == 1 AND buys_sol >= WHALE_MIN_SOL.
    Real bundlers split across multiple txs but a single-tx whale buy
    is also a strong signal.
    """
    if p.get("mint_tx_count") == 1 and (p.get("buys_sol") or 0) >= WHALE_MIN_SOL:
        return "BUNDLER", f"single tx bought {(p.get('buys_sol') or 0):.2f} SOL"
    return None


def _tag_sniper(p: Dict[str, Any]) -> Optional[Tuple[str, str]]:
    """Sniper = entered fast relative to its own history OR was the only tx.

    Without token-launch timestamp, we proxy by: first_buy_ts is the only
    tx we see (mint_tx_count == 1), suggesting it was a one-shot entry
    (typical sniper pattern).
    """
    if p.get("mint_tx_count") == 1 and p.get("first_buy_ts"):
        return "SNIPER", "single tx entry"
    return None


def _tag_early_exit(p: Dict[str, Any]) -> Optional[Tuple[str, str]]:
    """Bought and fully sold within EARLY_EXIT_WINDOW_S."""
    held = _held_seconds(p)
    if held is None:
        return None
    if held <= EARLY_EXIT_WINDOW_S and (p.get("sells_sol") or 0) > 0 and (p.get("still_holds_pct") or 0) < 10:
        return "EARLY_EXIT", f"sold after {held}s ({held // 60}m)"
    return None


def _tag_diamond_hand(p: Dict[str, Any]) -> Optional[Tuple[str, str]]:
    """Bought and still holds >= DIAMOND_HAND_THRESHOLD%."""
    shp = p.get("still_holds_pct") or 0
    if shp >= DIAMOND_HAND_THRESHOLD and (p.get("buys_sol") or 0) > 0:
        return "DIAMOND_HAND", f"still holds {shp:.0f}%"
    return None


def _tag_whale(p: Dict[str, Any]) -> Optional[Tuple[str, str]]:
    """Single position > WHALE_MIN_SOL."""
    total = (p.get("buys_sol") or 0) + (p.get("current_balance_ui") or 0)  # rough
    if (p.get("buys_sol") or 0) >= WHALE_MIN_SOL:
        return "WHALE", f"bought {(p.get('buys_sol') or 0):.2f} SOL"
    return None


def _tag_scalper(p: Dict[str, Any]) -> Optional[Tuple[str, str]]:
    """Multiple buy+sell round trips. Proxy: both buys and sells > 0 with high tx count."""
    n_buys, n_sells = _count_buys_sells(p)
    if (p.get("mint_tx_count") or 0) >= 4 and n_buys >= 1 and n_sells >= 1:
        return "SCALPER", f"{p.get('mint_tx_count')} txs"
    return None


def _tag_swing(p: Dict[str, Any]) -> Optional[Tuple[str, str]]:
    """Held 1-7 days, took profit."""
    held = _held_seconds(p)
    if held is None:
        return None
    if 86400 <= held <= 7 * 86400 and (p.get("sells_sol") or 0) > 0 and (p.get("realized_pnl_sol") or 0) > 0:
        return "SWING", f"held {held // 86400}d, pnl {(p.get('realized_pnl_sol') or 0):.3f} SOL"
    return None


def _tag_exit_liquidity(p: Dict[str, Any]) -> Optional[Tuple[str, str]]:
    """Lost money on a late buy. Proxy: pnl < 0 AND sells > 0."""
    pnl = p.get("realized_pnl_sol") or 0
    if pnl < 0 and (p.get("sells_sol") or 0) > 0 and (p.get("buys_sol") or 0) > 0:
        return "EXIT_LIQUIDITY", f"pnl {pnl:.3f} SOL"
    return None


def _tag_loser_or_winner(p: Dict[str, Any]) -> Tuple[str, str]:
    """Fallback: pure outcome-based."""
    pnl = p.get("realized_pnl_sol") or 0
    if pnl > 0:
        return "WINNER", f"pnl {pnl:.3f} SOL"
    elif pnl < 0:
        return "LOSER", f"pnl {pnl:.3f} SOL"
    return "NEUTRAL", "no realized pnl"


# Precedence-ordered list
_TAGGERS = [
    _tag_bundler,
    _tag_sniper,
    _tag_early_exit,
    _tag_diamond_hand,
    _tag_whale,
    _tag_scalper,
    _tag_swing,
    _tag_exit_liquidity,
]


def classify_wallet(profile: Dict[str, Any]) -> Dict[str, Any]:
    """Apply tag precedence list. Returns behavior_tags list + reasoning."""
    for tagger in _TAGGERS:
        result = tagger(profile)
        if result is not None:
            tag, reason = result
            metrics = {
                "held_seconds": _held_seconds(profile),
                "still_holds_pct": profile.get("still_holds_pct"),
                "roi_pct": profile.get("roi_pct"),
                "first_buy_ts": profile.get("first_buy_ts"),
                "last_action_ts": profile.get("last_action_ts"),
            }
            return {
                "wallet": profile.get("wallet"),
                "label": profile.get("label"),
                "tag": tag,
                "tag_reason": reason,
                "metrics": metrics,
            }
    # Fallback
    tag, reason = _tag_loser_or_winner(profile)
    return {
        "wallet": profile.get("wallet"),
        "label": profile.get("label"),
        "tag": tag,
        "tag_reason": reason,
        "metrics": {
            "held_seconds": _held_seconds(profile),
            "still_holds_pct": profile.get("still_holds_pct"),
            "roi_pct": profile.get("roi_pct"),
        },
    }


def classify_all(profiles: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Apply classify_wallet to a list of profiles."""
    return [classify_wallet(p) for p in profiles if p.get("wallet")]


def merge_into_profiles(
    profiles: List[Dict[str, Any]],
    classifications: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Attach tag + reason into each profile dict (in-place).
    Returns the same list (mutated)."""
    by_wallet = {c["wallet"]: c for c in classifications}
    for p in profiles:
        w = p.get("wallet")
        if w and w in by_wallet:
            cls = by_wallet[w]
            p["behavior_tags"] = [cls["tag"]]
            p["behavior_reason"] = cls["tag_reason"]
    return profiles


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _main() -> int:
    import argparse
    import json
    import sys

    p = argparse.ArgumentParser(description="Classify wallet behavior from profiles.")
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

    profiles = report.get("top_holders") or []
    classifications = classify_all(profiles)
    profiles = merge_into_profiles(profiles, classifications)
    report["behavior"] = classifications
    report["top_holders"] = profiles  # mutated with tags

    out = args.report.replace(".json", "_with_behavior.json")
    with open(out, "w") as f:
        json.dump(report, f, indent=2)

    tag_counts: Dict[str, int] = {}
    for c in classifications:
        tag_counts[c["tag"]] = tag_counts.get(c["tag"], 0) + 1
    print(json.dumps({"out": out, "tag_counts": tag_counts}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())