#!/usr/bin/env python3
"""
fund_flow.py — Jiro Sniper Net module.

For a given wallet, traces WHERE its SOL originally came from by walking
the very first inbound SOL transfer(s). Builds a directed graph:

    funder → [funded_wallet_1, funded_wallet_2, ...]

This is the cheap-RPC substitute for the "common-funder" heuristic used
by paid cabal-detection services. Two wallets funded by the same source
wallet within a tight time window are very likely the same entity
operating multiple wallets (a "bundler" pattern).

HOW IT WORKS:
1. `getSignaturesForAddress(wallet, limit=N)` — fetch recent sigs
2. Walk them OLDEST → NEWEST.
3. For each tx, diff pre/post SOL balances for `wallet`.
4. The FIRST tx with a positive SOL delta (and > 0.01 SOL received) is
   treated as the funding tx. We pull THAT tx's pre-balances and find
   the account whose post-balance is LOWER than pre by the matching
   amount (within 1% tolerance) → that's the funder.
5. We record the funder as a node in the graph with an edge from
   funder → wallet.

LIMITATIONS:
- We only trace back ONE hop. The funder's funder is a separate call.
  For cabal detection we don't need full recursion — we just need to
  find SHARED funders across many wallets.
- If the wallet's first tx was a SWAP (not a transfer), we might pick
  the wrong tx as "funding". Mitigation: only count deltas > 0.05 SOL
  as funding events (small amounts are likely fees/refunds).
- Some wallets receive SOL via multiple small txs (dust consolidation).
  We pick the largest single inbound transfer as the primary funder.
- Public mainnet-beta RPC sometimes returns pre/post balances WITHOUT
  accountKeys[].owner info. We always have accountKeys, but identifying
  "which signer actually sent the SOL" requires checking the tx's
  main balance-account diff, which we do.

OUTPUT SCHEMA:
{
  "wallet": str,
  "funder": str|None,           # the address that funded this wallet
  "fund_amount_sol": float|None, # how much SOL was sent
  "fund_ts": int|None,           # unix seconds of funding tx
  "fund_sig": str|None,          # signature of the funding tx
  "depth": int,                  # 0 if no trace, 1 if direct funder found
  "edges": [                     # all detected fund edges from this wallet
    {"from": str, "to": str, "amount_sol": float, "ts": int, "sig": str}
  ]
}
"""

from __future__ import annotations

import os
import json
import time
import logging
from typing import Any, Dict, List, Optional

import rpc_client

log = logging.getLogger("fund_flow")

# Tunables
MAX_SIGS_FOR_FUND_SCAN = int(os.environ.get("FUND_FLOW_MAX_SIGS", "100"))
MIN_FUND_AMOUNT_SOL = float(os.environ.get("FUND_FLOW_MIN_SOL", "0.05"))
FUND_TOLERANCE_SOL = float(os.environ.get("FUND_FLOW_TOL_SOL", "0.005"))  # 1% of 0.5 SOL
FUND_CACHE_TTL_S = int(os.environ.get("FUND_FLOW_CACHE_TTL_S", str(30 * 24 * 3600)))  # 30 days
FUND_CACHE_PATH = os.environ.get(
    "FUND_FLOW_CACHE_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache", "fund_flow.json"),
)

_cache: Dict[str, Dict[str, Any]] = {}


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

def _cache_load() -> None:
    global _cache
    if _cache:
        return
    if not FUND_CACHE_PATH or not os.path.exists(FUND_CACHE_PATH):
        return
    try:
        with open(FUND_CACHE_PATH, "r") as f:
            data = json.load(f)
        if isinstance(data, dict):
            _cache = data
    except (OSError, json.JSONDecodeError) as e:
        log.warning("fund_flow cache load failed: %s", e)


def _cache_save() -> None:
    d = os.path.dirname(FUND_CACHE_PATH)
    if d and not os.path.isdir(d):
        try:
            os.makedirs(d, exist_ok=True)
        except OSError:
            pass
    try:
        tmp = FUND_CACHE_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(_cache, f)
        os.replace(tmp, FUND_CACHE_PATH)
    except OSError as e:
        log.warning("fund_flow cache save failed: %s", e)


def _cache_get(wallet: str) -> Optional[Dict[str, Any]]:
    _cache_load()
    entry = _cache.get(wallet)
    if not entry:
        return None
    if time.time() - entry.get("ts", 0) > FUND_CACHE_TTL_S:
        return None
    return entry.get("data")


def _cache_put(wallet: str, data: Dict[str, Any]) -> None:
    _cache_load()
    _cache[wallet] = {"ts": time.time(), "data": data}
    _cache_save()


# ---------------------------------------------------------------------------
# RPC helpers
# ---------------------------------------------------------------------------

def _rpc(method: str, params: List[Any]) -> Optional[Any]:
    return rpc_client.rpc_call(method, params)


def _get_signatures(wallet: str, limit: int) -> List[Dict[str, Any]]:
    res = _rpc("getSignaturesForAddress", [wallet, {"limit": limit}])
    if not isinstance(res, list):
        return []
    return res


def _get_transaction(signature: str) -> Optional[Dict[str, Any]]:
    res = _rpc(
        "getTransaction",
        [
            signature,
            {"encoding": "json", "commitment": "confirmed", "maxSupportedTransactionVersion": 0},
        ],
    )
    return res if isinstance(res, dict) else None


# ---------------------------------------------------------------------------
# Trace logic
# ---------------------------------------------------------------------------

def _find_funder_in_tx(tx: Dict[str, Any], target_wallet: str) -> Optional[Dict[str, Any]]:
    """
    Given a tx where `target_wallet` received SOL, find which account
    sent it. Returns {"funder": address, "amount_sol": float, "sig": str}
    or None.
    """
    meta = tx.get("meta") or {}
    message = tx.get("message") or {}
    account_keys = message.get("accountKeys") or []

    pre_balances = meta.get("preBalances") or []
    post_balances = meta.get("postBalances") or []
    if len(pre_balances) != len(post_balances) or len(pre_balances) != len(account_keys):
        return None

    # Find target wallet's index
    target_idx: Optional[int] = None
    for i, k in enumerate(account_keys):
        key = k if isinstance(k, str) else (k.get("pubkey") if isinstance(k, dict) else None)
        if key == target_wallet:
            target_idx = i
            break
    if target_idx is None:
        return None

    target_delta_sol = (post_balances[target_idx] - pre_balances[target_idx]) / 1e9
    if target_delta_sol < MIN_FUND_AMOUNT_SOL:
        return None

    # Look for the counterparty: someone whose SOL decreased by approximately
    # the same amount (minus tx fee). Fee is at most ~0.01 SOL.
    expected_out_sol = target_delta_sol
    best_match: Optional[Dict[str, Any]] = None
    best_diff = float("inf")

    for i, k in enumerate(account_keys):
        if i == target_idx:
            continue
        delta_sol = (post_balances[i] - pre_balances[i]) / 1e9
        if delta_sol >= -FUND_TOLERANCE_SOL:
            # didn't lose SOL (or barely did); skip
            continue
        sent_sol = abs(delta_sol)
        # target received ~sent_sol minus fee. Diff should be tiny.
        diff = abs(sent_sol - expected_out_sol)
        if diff < best_diff:
            best_diff = diff
            best_match = {
                "funder": k if isinstance(k, str) else (k.get("pubkey") if isinstance(k, dict) else None),
                "amount_sol": sent_sol,
                "diff_sol": diff,
            }
    return best_match


def trace_funder(wallet: str, *, max_sigs: int = MAX_SIGS_FOR_FUND_SCAN) -> Dict[str, Any]:
    """
    Walk wallet's recent txs oldest→newest, return the FIRST inbound SOL
    transfer as its funder.

    Also returns ALL detected fund edges (in case the wallet received SOL
    in multiple txs). Used by cabal_detector later.
    """
    sigs = _get_signatures(wallet, max_sigs)
    if not sigs:
        return {
            "wallet": wallet,
            "funder": None,
            "fund_amount_sol": None,
            "fund_ts": None,
            "fund_sig": None,
            "depth": 0,
            "edges": [],
            "_error": "no signatures returned",
        }

    # sort oldest first
    sigs_sorted = sorted(sigs, key=lambda s: (s.get("blockTime") or 0))

    edges: List[Dict[str, Any]] = []
    primary_funder: Optional[Dict[str, Any]] = None

    for sig_info in sigs_sorted:
        sig = sig_info.get("signature")
        ts = sig_info.get("blockTime")
        if not sig:
            continue
        tx = _get_transaction(sig)
        if not tx:
            continue
        match = _find_funder_in_tx(tx, wallet)
        if match and match.get("funder"):
            edge = {
                "from": match["funder"],
                "to": wallet,
                "amount_sol": round(match["amount_sol"], 6),
                "ts": ts,
                "sig": sig,
            }
            edges.append(edge)
            # largest inbound transfer wins as primary funder
            if primary_funder is None or match["amount_sol"] > primary_funder["amount_sol"]:
                primary_funder = {
                    "funder": match["funder"],
                    "amount_sol": match["amount_sol"],
                    "ts": ts,
                    "sig": sig,
                }
            # keep walking — we want all edges, not just first
        # short-circuit once we have a solid funder (saves RPC credits)
        if primary_funder and primary_funder["amount_sol"] >= 0.5:
            # still keep walking a few more for cabal detection
            if len(edges) >= 5:
                break

    return {
        "wallet": wallet,
        "funder": primary_funder["funder"] if primary_funder else None,
        "fund_amount_sol": round(primary_funder["amount_sol"], 6) if primary_funder else None,
        "fund_ts": primary_funder["ts"] if primary_funder else None,
        "fund_sig": primary_funder["sig"] if primary_funder else None,
        "depth": 1 if primary_funder else 0,
        "edges": edges[:10],  # cap to avoid bloat
        "_traced_at": int(time.time()),
    }


def get_funder(
    wallet: str,
    *,
    use_cache: bool = True,
    force_refresh: bool = False,
) -> Optional[str]:
    """Cheap accessor: just the funder address, with cache."""
    if use_cache and not force_refresh:
        cached = _cache_get(wallet)
        if cached:
            return cached.get("funder")
    res = trace_funder(wallet)
    if use_cache:
        _cache_put(wallet, res)
    return res.get("funder")


def batch_funders(
    wallets: List[str],
    *,
    use_cache: bool = True,
) -> Dict[str, Optional[str]]:
    """
    Trace funders for many wallets. Returns {wallet: funder_or_None}.
    Sequential to respect RPC rate limits — parallel would hammer Helius.
    """
    out: Dict[str, Optional[str]] = {}
    for w in wallets:
        try:
            out[w] = get_funder(w, use_cache=use_cache)
        except Exception as e:
            log.warning("get_funder(%s) failed: %s", w, e)
            out[w] = None
    return out


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _main() -> int:
    global MAX_SIGS_FOR_FUND_SCAN
    import argparse
    p = argparse.ArgumentParser(description="Trace a wallet's funding source.")
    p.add_argument("wallet", help="Solana wallet address")
    p.add_argument("--no-cache", action="store_true")
    p.add_argument("--max-sigs", type=int, default=MAX_SIGS_FOR_FUND_SCAN)
    args = p.parse_args()

    MAX_SIGS_FOR_FUND_SCAN = args.max_sigs

    if not args.no_cache:
        cached = _cache_get(args.wallet)
        if cached:
            print(json.dumps(cached, indent=2))
            return 0

    res = trace_funder(args.wallet)
    _cache_put(args.wallet, res)
    print(json.dumps(res, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())