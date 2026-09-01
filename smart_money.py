#!/usr/bin/env python3
"""
smart_money.py — tracks a watchlist of known-good wallets and raises a
"convergence" signal when 2+ of them buy the same mint close together in
time. This is the "smart wallet / KOL tracking" edge described in trenching
practice — jiro currently has no equivalent (it only reacts to narrative +
raw on-chain liquidity/price microstructure, never to WHO is buying).

Design goals:
- No paid API required (Nansen/Cielo-style labeling is paid; this is the
  free-RPC-only version: YOU maintain the watchlist, e.g. curated from
  GMGN/Dexscreener top-trader tabs on past winners, or lists shared by
  KOLs you trust). See `watchlist.example.json`.
- Cheap enough to poll every cycle: only checks each watched wallet's most
  recent N signatures, not full history.

Watchlist format (watchlist.json, one level up from this file or path set
via SMART_MONEY_WATCHLIST env var):
[
  {"address": "5Q54...", "label": "CashCat Top PnL"},
  {"address": "9Fk2...", "label": "Early Sniper - 68% WR"}
]

LIMITATIONS:
- Detecting "wallet X bought mint Y" from getTransaction requires diffing
  postTokenBalances vs preTokenBalances for that owner — this module does
  that, but Jupiter-routed swaps sometimes touch several intermediate
  token accounts; a false-negative (missed buy) is more likely than a
  false-positive here.
- No historical win-rate/PnL scoring — this module only tells you a
  watched wallet bought something, not whether that wallet is currently
  "hot" or "cold". Curate and prune your watchlist manually (see notes.md).
"""

import os
import json
import time
from typing import Dict, Any, List, Optional

import rpc_client

DEFAULT_WATCHLIST_PATH = os.environ.get("SMART_MONEY_WATCHLIST", "watchlist.json")
RECENT_SIGS_PER_WALLET = 10          # how far back to look each poll cycle
_seen_signatures: set = set()        # avoid double-counting the same buy tx
_recent_buys: List[Dict[str, Any]] = []   # rolling log: {wallet, label, mint, ts, sig}
_RECENT_BUYS_MAX_AGE_SECONDS = 60 * 30


def _rpc(method: str, params: list) -> Optional[Dict[str, Any]]:
    return rpc_client.rpc_call(method, params)


def load_watchlist(path: str = DEFAULT_WATCHLIST_PATH) -> List[Dict[str, str]]:
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r") as f:
            data = json.load(f)
        return [w for w in data if w.get("address")]
    except (json.JSONDecodeError, OSError):
        return []


def _extract_buys_from_tx(tx: Dict[str, Any], owner: str) -> List[str]:
    """Returns list of mints this owner's token balance INCREASED for in this tx."""
    mints = []
    try:
        meta = tx["meta"]
        pre = {b["accountIndex"]: b for b in (meta.get("preTokenBalances") or [])}
        post = {b["accountIndex"]: b for b in (meta.get("postTokenBalances") or [])}
        for idx, post_bal in post.items():
            if post_bal.get("owner") != owner:
                continue
            pre_bal = pre.get(idx)
            pre_amt = float(pre_bal["uiTokenAmount"]["uiAmount"] or 0) if pre_bal else 0.0
            post_amt = float(post_bal["uiTokenAmount"]["uiAmount"] or 0)
            if post_amt > pre_amt:
                mints.append(post_bal.get("mint"))
    except (KeyError, TypeError, ValueError):
        pass
    return [m for m in mints if m]


def poll_watchlist(watchlist: Optional[List[Dict[str, str]]] = None) -> List[Dict[str, Any]]:
    """Call once per monitor cycle. Fetches each watched wallet's recent
    signatures, parses NEW ones for token buys, and appends to the rolling
    log. Returns the list of newly-detected buys this call (may be empty)."""
    watchlist = watchlist if watchlist is not None else load_watchlist()
    new_buys = []
    now = time.time()

    for w in watchlist:
        sigs = _rpc("getSignaturesForAddress", [w["address"], {"limit": RECENT_SIGS_PER_WALLET}]) or []
        for s in sigs:
            sig = s.get("signature")
            if not sig or sig in _seen_signatures:
                continue
            _seen_signatures.add(sig)
            tx = _rpc("getTransaction", [sig, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}])
            if not tx:
                continue
            for mint in _extract_buys_from_tx(tx, w["address"]):
                entry = {
                    "wallet": w["address"],
                    "label": w.get("label", ""),
                    "mint": mint,
                    "ts": s.get("blockTime") or now,
                    "signature": sig,
                }
                _recent_buys.append(entry)
                new_buys.append(entry)

    # trim old entries
    cutoff = now - _RECENT_BUYS_MAX_AGE_SECONDS
    _recent_buys[:] = [b for b in _recent_buys if b["ts"] >= cutoff]
    return new_buys


def check_convergence(mint: str, min_wallets: int = 2, window_seconds: int = 900) -> Dict[str, Any]:
    """Has this mint been bought by `min_wallets`+ watched wallets within
    the last `window_seconds`? Call this as part of entry scoring — a
    convergence hit is a strong bonus signal per trenching practice."""
    now = time.time()
    hits = [
        b for b in _recent_buys
        if b["mint"] == mint and (now - b["ts"]) <= window_seconds
    ]
    distinct_wallets = {h["wallet"] for h in hits}
    return {
        "mint": mint,
        "converged": len(distinct_wallets) >= min_wallets,
        "wallet_count": len(distinct_wallets),
        "wallets": [{"address": w, "label": next((h["label"] for h in hits if h["wallet"] == w), "")}
                    for w in distinct_wallets],
    }
