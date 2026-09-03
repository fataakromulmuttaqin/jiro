#!/usr/bin/env python3
"""
cabal_detector.py — Jiro Sniper Net module.

Given a Jiro Sniper Net report (from profile_top_holders.py) OR raw wallet
data, detect groups of wallets that look like they belong to the same
cabal/FnF group. Pure analytics on data already in hand — ZERO extra
RPC calls (everything we need is in the profile output).

WHAT IT DOES:
For a token's top holders, look for two strong cabal signals:
1. SHARED FUNDER: two+ holders funded by the same wallet within a tight
   time window. This is the classic "bundler" pattern — operator sends
   SOL from one master wallet to N child wallets, then all N children
   buy the same token. Free replacement for paid "common-funder" services.

2. CO-BUY TIMING: two+ holders bought the same token within a short
   window. Even without shared funder evidence, synchronized entry
   timing alone is suspicious.

CABAL CONFIDENCE:
A cluster's "cabal_score" is:
  - 2+ wallets with shared funder: +0.5 base
  - same funder, same time (±10 min): +0.3
  - co-buy timing within ±5 min: +0.2 each additional match (cap +0.4)
  - all winners (pnl > 0): +0.2 (suggests coordination skill)
  - all losers (pnl < 0): -0.2 (suggests exit liquidity)

Final score clamped to [0, 1]. Score >= 0.6 → tagged CABAL.
Score 0.3-0.6 → SUSPECT_CLUSTER (worth manual review).
Score < 0.3 → SOLO (independent traders).

LIMITATIONS:
- "Co-buy timing" detection uses the wallet's first_buy_ts (unix seconds)
  from wallet_profiler. If a wallet bought via multiple txs in the same
  block, timing will look perfectly synchronized (false positive for cabal).
- Common-funder evidence is MUCH stronger than co-buy alone. Score weighting
  reflects this.
- We don't analyze cross-token behavior (yet). A cabal that splits across
  many tokens will look like multiple "incidental" clusters. P2.5 will add
  cross-token aggregation.
- SOLO classification is NOT "innocent" — it's "insufficient evidence of
  cabal". Solo wallets may still be sybil or exit liquidity.

OUTPUT SCHEMA (per mint):
{
  "mint": str,
  "clusters": [
    {
      "cluster_id": int,
      "type": "CABAL" | "SUSPECT_CLUSTER" | "SOLO",
      "cabal_score": float,
      "shared_funder": str|None,
      "wallets": [
        {"wallet": str, "label": str, "pnl_sol": float, "win": bool, "first_buy_ts": int}
      ],
      "reason": str  # human-readable explanation
    }
  ],
  "summary": {
    "n_wallets": int,
    "n_clusters": int,
    "n_cabal": int,
    "n_suspect": int,
    "n_solo": int
  }
}
"""

from __future__ import annotations

import os
import json
import time
import logging
from collections import defaultdict
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger("cabal_detector")

# Tunables (override via env if needed)
CABAL_SCORE_THRESHOLD = float(os.environ.get("CABAL_SCORE_THRESHOLD", "0.6"))
SUSPECT_SCORE_THRESHOLD = float(os.environ.get("SUSPECT_SCORE_THRESHOLD", "0.3"))
CO_BUY_WINDOW_S = int(os.environ.get("CO_BUY_WINDOW_S", "300"))  # 5 min default
FUNDER_TIME_WINDOW_S = int(os.environ.get("FUNDER_TIME_WINDOW_S", "600"))  # 10 min
# Optional user-supplied cabal seed DB (Bos said he'd provide later)
CABAL_SEED_PATH = os.environ.get("CABAL_SEED_PATH", "")

# Cache cabal analysis results (pure analytics, no RPC involved)
CABAL_CACHE_PATH = os.environ.get(
    "CABAL_CACHE_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache", "cabal_analysis.json"),
)
_cache: Dict[str, Dict[str, Any]] = {}


# ---------------------------------------------------------------------------
# Cache (analytics cache — keeps repeated analyses cheap)
# ---------------------------------------------------------------------------

def _cache_load() -> None:
    global _cache
    if _cache:
        return
    if not CABAL_CACHE_PATH or not os.path.exists(CABAL_CACHE_PATH):
        return
    try:
        with open(CABAL_CACHE_PATH, "r") as f:
            data = json.load(f)
        if isinstance(data, dict):
            _cache = data
    except (OSError, json.JSONDecodeError) as e:
        log.warning("cabal cache load failed: %s", e)


def _cache_save() -> None:
    d = os.path.dirname(CABAL_CACHE_PATH)
    if d and not os.path.isdir(d):
        try:
            os.makedirs(d, exist_ok=True)
        except OSError:
            pass
    try:
        tmp = CABAL_CACHE_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(_cache, f)
        os.replace(tmp, CABAL_CACHE_PATH)
    except OSError as e:
        log.warning("cabal cache save failed: %s", e)


# ---------------------------------------------------------------------------
# Seed DB (Bos provides this later — empty by default)
# ---------------------------------------------------------------------------

def load_seed_cabals() -> Dict[str, str]:
    """
    Load user-supplied cabal seeds: {funder_address: "CABAL_NAME"}.
    Cold-start = {} until Bos injects. Format: {"5Q54...": "CashCartel", ...}
    """
    if not CABAL_SEED_PATH or not os.path.exists(CABAL_SEED_PATH):
        return {}
    try:
        with open(CABAL_SEED_PATH, "r") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return {str(k): str(v) for k, v in data.items()}
    except (OSError, json.JSONDecodeError) as e:
        log.warning("cabal seed load failed: %s", e)
    return {}


# ---------------------------------------------------------------------------
# Cluster detection
# ---------------------------------------------------------------------------

def _build_funder_groups(
    holders: List[Dict[str, Any]],
    funders_map: Dict[str, List[str]],
) -> List[Dict[str, Any]]:
    """
    Return list of funder groups:
    [{funder, children: [wallet, ...]}, ...]
    Only groups with 2+ children are interesting (likely cabal).
    """
    groups = []
    for funder, children in funders_map.items():
        if not funder or len(children) < 2:
            continue
        # filter to children that are actually in our holder set
        in_set = [w for w in children if any(h.get("wallet") == w for h in holders)]
        if len(in_set) < 2:
            continue
        groups.append({"funder": funder, "children": in_set})
    return groups


def _co_buy_groups(
    holders: List[Dict[str, Any]],
    window_s: int,
) -> List[Dict[str, Any]]:
    """
    Cluster holders whose first_buy_ts falls within `window_s` of each other.
    Greedy single-link clustering: walk sorted by ts, start a new cluster
    when the gap to the previous holder > window_s.

    Returns [{center_ts, members: [wallet, ...]}, ...] with len(members) >= 2.
    """
    tsed = [(h.get("first_buy_ts") or 0, h) for h in holders if h.get("first_buy_ts")]
    tsed.sort(key=lambda x: x[0])
    if not tsed:
        return []

    clusters: List[Dict[str, Any]] = []
    cur: List[Tuple[int, Dict[str, Any]]] = [tsed[0]]
    for ts, h in tsed[1:]:
        if ts - cur[-1][0] <= window_s:
            cur.append((ts, h))
        else:
            if len(cur) >= 2:
                clusters.append({
                    "center_ts": sum(t for t, _ in cur) // len(cur),
                    "members": [hh.get("wallet") for _, hh in cur],
                })
            cur = [(ts, h)]
    if len(cur) >= 2:
        clusters.append({
            "center_ts": sum(t for t, _ in cur) // len(cur),
            "members": [hh.get("wallet") for _, hh in cur],
        })
    return clusters


def _score_cluster(
    wallets: List[str],
    holders_by_wallet: Dict[str, Dict[str, Any]],
    shared_funder: Optional[str],
    funder_funding_ts: Optional[int],
    *,
    seed_names: Dict[str, str],
) -> Tuple[float, str]:
    """
    Compute cabal_score for a cluster + a human-readable reason.
    Score 0..1 — see module docstring for weights.
    """
    score = 0.0
    reasons: List[str] = []

    # 1. Shared funder (strongest signal)
    if shared_funder:
        score += 0.5
        if shared_funder in seed_names:
            score += 0.3
            reasons.append(f"known cabal '{seed_names[shared_funder]}' funder={shared_funder[:6]}…")
        else:
            reasons.append(f"shared funder {shared_funder[:6]}…")

    # 2. Funder timing tight
    if funder_funding_ts and len(wallets) >= 2:
        # gather funding ts for these wallets (look up via holders)
        funding_ts_list: List[int] = []
        for w in wallets:
            h = holders_by_wallet.get(w)
            if h and h.get("first_buy_ts"):
                # use first buy as a proxy — not perfect but usually close
                funding_ts_list.append(h["first_buy_ts"])
        if len(funding_ts_list) >= 2:
            spread = max(funding_ts_list) - min(funding_ts_list)
            if spread <= FUNDER_TIME_WINDOW_S:
                score += 0.3
                reasons.append(f"co-funding within {spread}s")

    # 3. Co-buy timing (already pre-filtered by caller to within window)
    score += 0.2 * min(2, len(wallets) - 1)  # +0.2 per extra member beyond first
    if len(wallets) >= 2:
        reasons.append(f"{len(wallets)} co-buy wallets")

    # 4. Win/loss alignment
    pnls = [holders_by_wallet.get(w, {}).get("realized_pnl_sol") for w in wallets]
    pnls = [p for p in pnls if p is not None]
    if pnls and all(p > 0 for p in pnls):
        score += 0.2
        reasons.append("all winners")
    elif pnls and all(p < 0 for p in pnls):
        score -= 0.2
        reasons.append("all losers (exit liquidity?)")

    score = max(0.0, min(1.0, score))
    reason = "; ".join(reasons) if reasons else "no signal"
    return score, reason


def detect_clusters(
    *,
    holders: List[Dict[str, Any]],
    funders_map: Dict[str, List[str]],
    funder_details: Optional[Dict[str, Any]] = None,
    co_buy_window_s: int = CO_BUY_WINDOW_S,
) -> Dict[str, Any]:
    """
    Main entry: cluster the holders, score each cluster, classify.

    `holders`: list of profile dicts (output of wallet_profiler) — must
                have at least: wallet, first_buy_ts, realized_pnl_sol, win.
    `funders_map`: {funder_addr: [wallet1, wallet2, ...]} — output of
                   profile_top_holders.py.
    `funder_details`: optional {funder_addr: {first_seen_as_funder, ...}}
    """
    funder_details = funder_details or {}
    seed_names = load_seed_cabals()

    holders_by_wallet: Dict[str, Dict[str, Any]] = {h["wallet"]: h for h in holders if h.get("wallet")}

    # 1. Funder-based clusters (strongest signal)
    funder_groups = _build_funder_groups(holders, funders_map)

    # 2. Co-buy clusters
    co_buy = _co_buy_groups(holders, co_buy_window_s)

    # 3. Build unified cluster list with dedup by member set
    seen: List[set] = []  # list of member sets
    clusters_out: List[Dict[str, Any]] = []

    def _add_cluster(members: List[str], shared_funder: Optional[str]) -> None:
        mset = set(members)
        for s in seen:
            if s == mset:
                return  # dedup exact
        seen.append(mset)
        score, reason = _score_cluster(
            members,
            holders_by_wallet,
            shared_funder,
            funder_details.get(shared_funder, {}).get("first_seen_as_funder") if shared_funder else None,
            seed_names=seed_names,
        )
        if score >= CABAL_SCORE_THRESHOLD:
            ctype = "CABAL"
        elif score >= SUSPECT_SCORE_THRESHOLD:
            ctype = "SUSPECT_CLUSTER"
        else:
            ctype = "SOLO"
        clusters_out.append({
            "cluster_id": len(clusters_out),
            "type": ctype,
            "cabal_score": round(score, 3),
            "shared_funder": shared_funder,
            "shared_funder_name": seed_names.get(shared_funder) if shared_funder else None,
            "wallets": [
                {
                    "wallet": w,
                    "label": holders_by_wallet.get(w, {}).get("label", w[:6] + "…" + w[-4:]),
                    "pnl_sol": holders_by_wallet.get(w, {}).get("realized_pnl_sol"),
                    "win": holders_by_wallet.get(w, {}).get("win"),
                    "first_buy_ts": holders_by_wallet.get(w, {}).get("first_buy_ts"),
                }
                for w in members
            ],
            "reason": reason,
        })

    # funder-based first (highest signal)
    for grp in funder_groups:
        _add_cluster(grp["children"], shared_funder=grp["funder"])

    # then co-buy groups (may overlap with funder groups — dedup handles it)
    for grp in co_buy:
        _add_cluster(grp["members"], shared_funder=None)

    # finally — any wallet not in any cluster becomes its own SOLO singleton
    in_any_cluster = set()
    for c in clusters_out:
        for w in c["wallets"]:
            in_any_cluster.add(w["wallet"])
    for h in holders:
        w = h.get("wallet")
        if w and w not in in_any_cluster:
            _add_cluster([w], shared_funder=None)

    summary = {
        "n_wallets": len(holders),
        "n_clusters": len(clusters_out),
        "n_cabal": sum(1 for c in clusters_out if c["type"] == "CABAL"),
        "n_suspect": sum(1 for c in clusters_out if c["type"] == "SUSPECT_CLUSTER"),
        "n_solo": sum(1 for c in clusters_out if c["type"] == "SOLO"),
    }
    return {
        "ts": int(time.time()),
        "clusters": clusters_out,
        "summary": summary,
    }


def analyze_report(report: Dict[str, Any]) -> Dict[str, Any]:
    """
    Run cabal detection on a sniper_net_report (from profile_top_holders.py).
    Adds a `cabal` key to the report. Returns the same report (mutated).
    """
    cabal = detect_clusters(
        holders=report.get("top_holders") or [],
        funders_map=report.get("funders") or {},
        funder_details=report.get("funder_details") or {},
    )
    report["cabal"] = cabal
    return report


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _main() -> int:
    import argparse
    p = argparse.ArgumentParser(
        description="Run cabal detection on a sniper_net_report.json"
    )
    p.add_argument(
        "--report",
        default=os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "cache", "sniper_net_report.json",
        ),
        help="Path to sniper_net_report.json (default: cache/sniper_net_report.json)",
    )
    p.add_argument("--out", default=None,
                   help="Output path (default: <report>_with_cabal.json)")
    args = p.parse_args()

    if not os.path.exists(args.report):
        print(f"ERROR: report not found: {args.report}", file=sys.stderr)
        return 1

    with open(args.report) as f:
        report = json.load(f)

    enriched = analyze_report(report)
    out = args.out or args.report.replace(".json", "_with_cabal.json")
    with open(out, "w") as f:
        json.dump(enriched, f, indent=2)

    print(json.dumps(enriched.get("cabal", {}).get("summary", {}), indent=2))
    return 0


if __name__ == "__main__":
    import sys
    raise SystemExit(_main())