#!/usr/bin/env python3
"""
holder_analyzer.py — ENTRY-time holder distribution / rug screening.

Everything in onchain_analyzer.py is an EXIT signal (fires after you're
already in). This module is the missing ENTRY-side counterpart: the
"holder distribution" checklist from trenching practice (top10 %, dev
hold %, sniper/bundler concentration, fresh-wallet %, mint/freeze
authority) — checked BEFORE opening a position, not after.

Metrics implemented (free RPC + pump.fun public API only):

1. TOP10_PCT — combined % of supply held by the top N token accounts.
2. DEV_HOLD_PCT — % held by the pump.fun-reported creator wallet, if the
   token originated on pump.fun and the creator is still among the top
   holders. (Best-effort: only works for pump.fun-origin tokens.)
3. FRESH_WALLET_PCT — among top holders, what fraction are wallets with
   almost no transaction history (a classic sybil/sniper-farm signature:
   dozens of wallets created solely to ape one launch).
4. BUNDLER_CLUSTER_PCT — among top holders, the largest group of wallets
   that were all first funded (in SOL) by the SAME source address. This
   is the strongest free signal for "these wallets are one entity/bundle
   pretending to be organic holders."
5. MINT_AUTHORITY_RENOUNCED / FREEZE_AUTHORITY_RENOUNCED — straight from
   the mint account. Non-renounced = dev can mint infinite supply or
   freeze your tokens; both are hard-reject-worthy for most trenchers.

LIMITATIONS (read this before trusting the score):
- getTokenLargestAccounts only returns the top 20 TOKEN ACCOUNTS. One of
  them is very often the AMM pool's own vault, which this module cannot
  reliably exclude without knowing the pool address — it will inflate
  top10_pct somewhat. Treat as "top10 including pool", not pure holders.
- DEV_HOLD_PCT only resolves for pump.fun-origin tokens (uses pump.fun's
  public frontend API). For anything else it comes back as "unknown",
  not "zero" — don't treat unknown as safe.
- FRESH_WALLET_PCT and BUNDLER_CLUSTER_PCT use `getSignaturesForAddress`
  with a capped page size (see FUNDING_HISTORY_LIMIT). For a wallet with
  more history than that, this looks further back than needed for fresh
  wallets (the interesting case) but may miss the true first-funding tx
  for older wallets — those simply won't be flagged as clustered, which
  is a false-negative bias, not a false-positive one.
- This is RPC-call-heavy: screening one token costs roughly
  1 (supply) + 1 (largest accounts) + 1 (mint authorities)
  + top_n (owner lookups) + up to top_n (signature history)
  + up to top_n (oldest-tx funding lookup)
  ≈ 3 + 3*top_n calls. With top_n=15 that's ~48 calls per screen.
  Use a paid RPC (Helius/QuickNode/Triton) for this, not public mainnet-beta,
  and cache results (see `screen_cache`) so you don't re-screen the same
  mint every monitor cycle.
"""

import os
import time
import requests
from typing import Dict, Any, List, Optional, Set

import rpc_client

PUMPFUN_API = "https://frontend-api.pump.fun/coins"
FUNDING_HISTORY_LIMIT = 50          # sigs fetched per wallet to judge "fresh" + find funder
FRESH_WALLET_TX_THRESHOLD = 5       # fewer than this many total sigs seen => "fresh"

_screen_cache: Dict[str, Dict[str, Any]] = {}   # mint -> {ts, result}
_SCREEN_CACHE_TTL_SECONDS = 60 * 20


# --------------------------------------------------------------------------
# Low-level RPC helpers
# --------------------------------------------------------------------------

def _rpc(method: str, params: list) -> Optional[Dict[str, Any]]:
    return rpc_client.rpc_call(method, params)


def get_total_supply(mint: str) -> Optional[float]:
    r = _rpc("getTokenSupply", [mint])
    if not r:
        return None
    try:
        return float(r["value"]["uiAmount"])
    except (KeyError, TypeError, ValueError):
        return None


def get_mint_authorities(mint: str) -> Dict[str, Optional[str]]:
    """Returns {'mint_authority': str|None, 'freeze_authority': str|None}.
    None means renounced (or lookup failed — caller should treat unknown
    conservatively, i.e. NOT the same as confirmed-renounced)."""
    r = _rpc("getAccountInfo", [mint, {"encoding": "jsonParsed"}])
    out = {"mint_authority": "UNKNOWN", "freeze_authority": "UNKNOWN"}
    try:
        info = r["value"]["data"]["parsed"]["info"]
        out["mint_authority"] = info.get("mintAuthority")       # None = renounced
        out["freeze_authority"] = info.get("freezeAuthority")   # None = renounced
    except (KeyError, TypeError):
        pass
    return out


def get_top_holders(mint: str, top_n: int) -> List[Dict[str, Any]]:
    """Returns [{token_account, owner, amount, pct}] sorted descending.
    `owner` may be None if the account-owner lookup fails for that entry."""
    r = _rpc("getTokenLargestAccounts", [mint])
    total = get_total_supply(mint)
    if not r or not total or total <= 0:
        return []
    accounts = (r.get("value") or [])[:top_n]
    out = []
    for acc in accounts:
        token_account = acc.get("address")
        ui_amount = acc.get("uiAmount")
        if token_account is None or ui_amount is None:
            continue
        owner_info = _rpc("getAccountInfo", [token_account, {"encoding": "jsonParsed"}])
        owner = None
        try:
            owner = owner_info["value"]["data"]["parsed"]["info"]["owner"]
        except (KeyError, TypeError):
            pass
        out.append({
            "token_account": token_account,
            "owner": owner,
            "amount": float(ui_amount),
            "pct": float(ui_amount) / total * 100.0,
        })
    return out


def get_pumpfun_creator(mint: str) -> Optional[str]:
    """Best-effort: pump.fun's public frontend API exposes the original
    creator wallet for tokens launched there. Returns None for non-pump.fun
    tokens or if the API is unreachable — NOT the same as 'no dev wallet'."""
    try:
        r = requests.get(f"{PUMPFUN_API}/{mint}", timeout=10)
        if r.status_code != 200:
            return None
        data = r.json()
        return data.get("creator")
    except Exception:
        return None


def _wallet_signature_history(owner: str, limit: int) -> List[Dict[str, Any]]:
    r = _rpc("getSignaturesForAddress", [owner, {"limit": limit}])
    if not r:
        return []
    return r  # list of {signature, blockTime, ...}, newest first


def _oldest_funding_source(owner: str, sigs: List[Dict[str, Any]]) -> Optional[str]:
    """Best-effort: look at the OLDEST signature we fetched for this wallet
    (bounded by FUNDING_HISTORY_LIMIT — see module docstring caveat) and try
    to find who funded it with SOL in that transaction."""
    if not sigs:
        return None
    oldest_sig = sigs[-1]["signature"]
    tx = _rpc("getTransaction", [oldest_sig, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}])
    if not tx:
        return None
    try:
        account_keys = tx["transaction"]["message"]["accountKeys"]
        pre = tx["meta"]["preBalances"]
        post = tx["meta"]["postBalances"]
        owner_idx = None
        for i, k in enumerate(account_keys):
            addr = k["pubkey"] if isinstance(k, dict) else k
            if addr == owner:
                owner_idx = i
                break
        if owner_idx is None:
            return None
        # our wallet's balance went UP in this tx (it received funding) —
        # find another account whose balance went DOWN by roughly the same
        # amount (the funder). Simple heuristic, no exact instruction parse.
        received = post[owner_idx] - pre[owner_idx]
        if received <= 0:
            return None
        for i, k in enumerate(account_keys):
            if i == owner_idx:
                continue
            delta = post[i] - pre[i]
            if delta < 0 and abs(abs(delta) - received) < received * 0.05 + 5000:
                return k["pubkey"] if isinstance(k, dict) else k
    except (KeyError, TypeError, IndexError):
        return None
    return None


def analyze_wallet_freshness_and_funding(owner: str) -> Dict[str, Any]:
    sigs = _wallet_signature_history(owner, FUNDING_HISTORY_LIMIT)
    is_fresh = len(sigs) < FRESH_WALLET_TX_THRESHOLD
    funder = _oldest_funding_source(owner, sigs) if sigs else None
    return {"tx_count_seen": len(sigs), "is_fresh": is_fresh, "funding_source": funder}


# --------------------------------------------------------------------------
# Composite screen
# --------------------------------------------------------------------------

def screen_token(mint: str, cfg_holders: Dict[str, Any], use_cache: bool = True) -> Dict[str, Any]:
    """Full holder-distribution screen for a mint. Returns a dict with raw
    metrics, a 0-10 risk_score (10 = worst), human-readable flags, and
    `should_reject` if any configured hard limit was breached.

    This is the function to call from trading.py BEFORE opening a position."""
    if use_cache:
        cached = _screen_cache.get(mint)
        if cached and (time.time() - cached["ts"]) < _SCREEN_CACHE_TTL_SECONDS:
            return cached["result"]

    top_n = cfg_holders.get("top_n_holders", 15)
    holders = get_top_holders(mint, top_n)
    authorities = get_mint_authorities(mint)
    creator = get_pumpfun_creator(mint)

    top10_pct = sum(h["pct"] for h in holders[:10])

    dev_hold_pct = None
    if creator:
        dev_hold_pct = sum(h["pct"] for h in holders if h.get("owner") == creator)

    # freshness + funding clustering (bounded RPC cost: one pass over top_n owners)
    owners = [h["owner"] for h in holders if h.get("owner")]
    freshness: Dict[str, Dict[str, Any]] = {}
    for owner in owners:
        freshness[owner] = analyze_wallet_freshness_and_funding(owner)

    fresh_supply_pct = sum(
        h["pct"] for h in holders
        if h.get("owner") and freshness.get(h["owner"], {}).get("is_fresh")
    )

    funder_groups: Dict[str, List[str]] = {}
    for h in holders:
        owner = h.get("owner")
        if not owner:
            continue
        funder = freshness.get(owner, {}).get("funding_source")
        if funder:
            funder_groups.setdefault(funder, []).append(owner)
    largest_cluster_owners: Set[str] = set()
    if funder_groups:
        biggest = max(funder_groups.values(), key=len)
        if len(biggest) >= 2:  # 2+ wallets sharing a funder = a cluster
            largest_cluster_owners = set(biggest)
    bundler_cluster_pct = sum(
        h["pct"] for h in holders if h.get("owner") in largest_cluster_owners
    )

    mint_renounced = authorities["mint_authority"] is None
    freeze_renounced = authorities["freeze_authority"] is None

    flags: List[str] = []
    risk_score = 0.0

    def _bucket(value: Optional[float], green_max: float, yellow_max: float, label: str, weight: float):
        nonlocal risk_score
        if value is None:
            flags.append(f"{label}: unknown (treat cautiously)")
            risk_score += weight * 0.6
            return
        if value <= green_max:
            flags.append(f"{label}: {value:.1f}% (green)")
        elif value <= yellow_max:
            flags.append(f"{label}: {value:.1f}% (yellow)")
            risk_score += weight * 0.5
        else:
            flags.append(f"{label}: {value:.1f}% (RED)")
            risk_score += weight

    _bucket(top10_pct, 25, 40, "top10_pct", 2.5)
    _bucket(dev_hold_pct, 5, 10, "dev_hold_pct", 2.0)
    _bucket(fresh_supply_pct, 20, 40, "fresh_wallet_pct", 2.0)
    _bucket(bundler_cluster_pct, 10, 20, "bundler_cluster_pct", 2.5)

    if not mint_renounced:
        flags.append("mint_authority: NOT renounced (RED)")
        risk_score += 0.75
    else:
        flags.append("mint_authority: renounced (green)")
    if not freeze_renounced:
        flags.append("freeze_authority: NOT renounced (RED)")
        risk_score += 0.75
    else:
        flags.append("freeze_authority: renounced (green)")

    risk_score = min(10.0, round(risk_score, 2))

    should_reject = False
    reject_reasons = []
    if cfg_holders.get("require_mint_authority_renounced", True) and not mint_renounced:
        should_reject = True
        reject_reasons.append("mint authority not renounced")
    if cfg_holders.get("require_freeze_authority_renounced", True) and not freeze_renounced:
        should_reject = True
        reject_reasons.append("freeze authority not renounced")
    if top10_pct > cfg_holders.get("top10_holder_max_pct", 40):
        should_reject = True
        reject_reasons.append(f"top10 holders {top10_pct:.1f}% > max")
    if dev_hold_pct is not None and dev_hold_pct > cfg_holders.get("dev_hold_max_pct", 10):
        should_reject = True
        reject_reasons.append(f"dev hold {dev_hold_pct:.1f}% > max")
    if bundler_cluster_pct > cfg_holders.get("bundler_cluster_max_pct", 20):
        should_reject = True
        reject_reasons.append(f"bundler cluster {bundler_cluster_pct:.1f}% > max")

    result = {
        "mint": mint,
        "top10_pct": round(top10_pct, 2),
        "dev_hold_pct": round(dev_hold_pct, 2) if dev_hold_pct is not None else None,
        "fresh_wallet_pct": round(fresh_supply_pct, 2),
        "bundler_cluster_pct": round(bundler_cluster_pct, 2),
        "mint_authority_renounced": mint_renounced,
        "freeze_authority_renounced": freeze_renounced,
        "creator_wallet": creator,
        "risk_score": risk_score,       # 0 = clean, 10 = maximally red-flagged
        "flags": flags,
        "should_reject": should_reject,
        "reject_reasons": reject_reasons,
        "holders_sampled": len(holders),
    }
    _screen_cache[mint] = {"ts": time.time(), "result": result}
    return result


def reset_cache(mint: Optional[str] = None) -> None:
    if mint:
        _screen_cache.pop(mint, None)
    else:
        _screen_cache.clear()
