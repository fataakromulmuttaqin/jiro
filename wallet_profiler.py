#!/usr/bin/env python3
"""
wallet_profiler.py — Jiro Sniper Net module.

Reconstructs a wallet's trading PnL for a specific SPL token mint using only
public Solana RPC. This is the free-RPC replacement for paid services like
Birdeye/Nansen "wallet PnL" endpoints, which cost $$$/month.

WHY THIS EXISTS:
- We need to know: "when this top wallet bought this token, did it pump or
  dump? How much did it make?". Without that, we can't tell winners from
  exit-liquidity.
- Birdeye Pro / Nansen paid PnL = $50-300/mo. We get the same answer for $0
  by reconstructing from raw tx history ourselves.

HOW IT WORKS:
1. `getSignaturesForAddress(wallet)` — fetch recent tx signatures
2. For each signature, `getTransaction(sig)` — fetch full tx with
   preTokenBalances + postTokenBalances.
3. For each tx where the wallet is an account holder of our target mint,
   diff post vs pre token amount → that's the buy/sell delta in this tx.
4. Sum buys and sells in SOL terms (using tx SOL balance diffs as proxy
   for cost basis when swap leg is the mint we're tracking; this works
   for direct SOL→mint swaps but is approximate for routed swaps).
5. PnL = sells_sol - buys_sol (realized only).

LIMITATIONS (read before trusting numbers):
- This only sees ON-CHAIN reality, not off-chain intent. A wallet that
  CEX'd to fund its first buy will look "fresh" but actually has deep
  history elsewhere.
- For ROUTED swaps (e.g. USDC→SOL→mint via Jupiter aggregator), the SOL
  balance diff at the wallet is NOT the mint cost basis — there are
  intermediate token accounts. We approximate by assuming the SOL diff
  correlates with the mint swap leg. This is wrong ~20-30% of the time
  for Jupiter-routed swaps.
- A wallet can hold tokens across MULTIPLE token accounts (different ATAs
  for the same mint). We aggregate across all of them, but if the wallet
  closed an ATA between txs, that mint is invisible to us afterwards.
- "First buy" detection only catches the FIRST tx in our signature window
  where the wallet gained this mint. Older history beyond `MAX_SIGS` is
  invisible. For deep-history profiles, raise MAX_SIGS (costs more RPC).
- Tokens received via airdrop / transfer (not a swap) look identical to
  buys in the balance diff — we can't distinguish without parsing inner
  instructions, which is out of scope here.

RPC COST (Helius free tier = 100K credits/mo):
- 1 sig list call: ~1 credit (returns 1k sigs)
- 1 tx detail call: ~1 credit
- Per wallet profile: ~1 + N_tx calls. Default N=50 → ~51 credits.
- 5 wallets × 5 tokens = 25 wallet-profiles = ~1275 credits.
- Well under free tier. Cache aggressively to avoid repeat work.

OUTPUT SCHEMA (per wallet × mint):
{
  "wallet": "<address>",
  "label": "<watchlist label or short addr>",
  "mint": "<token mint>",
  "tx_count": int,           # total txs we saw for this wallet
  "mint_tx_count": int,      # txs that touched our target mint
  "first_buy_ts": int|None,  # unix seconds of first mint purchase
  "last_action_ts": int|None,
  "buys_sol": float,         # total SOL spent on this mint (approx)
  "sells_sol": float,        # total SOL received from this mint
  "realized_pnl_sol": float, # sells - buys (can be negative)
  "roi_pct": float|None,     # pnl / buys * 100, None if buys=0
  "still_holds_pct": float,  # % of supply bought that's NOT yet sold
  "current_balance_ui": float,  # tokens currently held
  "win": bool|None,          # True if pnl>0, None if no trades
  "behavior_tags": []        # filled by behavior_miner, empty here
}
"""

from __future__ import annotations

import os
import json
import time
import logging
from typing import Any, Dict, List, Optional, Tuple

import rpc_client

# Logging (inherits root config if set)
log = logging.getLogger("wallet_profiler")

# --- Tunables (override via env if needed) ---
MAX_SIGS_PER_WALLET = int(os.environ.get("PROFILER_MAX_SIGS", "50"))   # tx history depth
PROFILE_CACHE_TTL_S = int(os.environ.get("PROFILER_CACHE_TTL_S", str(7 * 24 * 3600)))  # 7 days
PROFILE_CACHE_PATH = os.environ.get(
    "PROFILER_CACHE_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache", "wallet_profiles.json"),
)

# In-memory + on-disk cache (tx history is immutable, so cache is safe forever)
# Format: {f"{wallet}:{mint}": {"ts": float, "profile": {...}}}
_cache: Dict[str, Dict[str, Any]] = {}


# ---------------------------------------------------------------------------
# Cache helpers
# ---------------------------------------------------------------------------

def _ensure_cache_dir() -> None:
    """Make sure parent dir of PROFILE_CACHE_PATH exists."""
    d = os.path.dirname(PROFILE_CACHE_PATH)
    if d and not os.path.isdir(d):
        try:
            os.makedirs(d, exist_ok=True)
        except OSError as e:
            log.warning("could not create cache dir %s: %s", d, e)


def _cache_load() -> None:
    """Load disk cache into memory. Silent on missing/corrupt file."""
    global _cache
    if _cache:
        return  # already loaded
    if not PROFILE_CACHE_PATH or not os.path.exists(PROFILE_CACHE_PATH):
        return
    try:
        with open(PROFILE_CACHE_PATH, "r") as f:
            data = json.load(f)
        if isinstance(data, dict):
            _cache = data
    except (OSError, json.JSONDecodeError) as e:
        log.warning("cache load failed (%s); starting fresh", e)


def _cache_save() -> None:
    """Persist cache to disk. Silent on write failure (cache is best-effort)."""
    _ensure_cache_dir()
    try:
        tmp = PROFILE_CACHE_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(_cache, f)
        os.replace(tmp, PROFILE_CACHE_PATH)
    except OSError as e:
        log.warning("cache save failed: %s", e)


def _cache_get(wallet: str, mint: str) -> Optional[Dict[str, Any]]:
    """Return cached profile if fresh enough, else None."""
    _cache_load()
    key = f"{wallet}:{mint}"
    entry = _cache.get(key)
    if not entry:
        return None
    if time.time() - entry.get("ts", 0) > PROFILE_CACHE_TTL_S:
        return None
    return entry.get("profile")


def _cache_put(wallet: str, mint: str, profile: Dict[str, Any]) -> None:
    """Store profile in cache + flush to disk."""
    _cache_load()
    key = f"{wallet}:{mint}"
    _cache[key] = {"ts": time.time(), "profile": profile}
    _cache_save()


# ---------------------------------------------------------------------------
# RPC wrappers (delegate to failover client)
# ---------------------------------------------------------------------------

def _rpc(method: str, params: List[Any]) -> Optional[Any]:
    """Thin wrapper so we can mock in tests."""
    return rpc_client.rpc_call(method, params)


def get_signatures(wallet: str, limit: int = MAX_SIGS_PER_WALLET) -> List[Dict[str, Any]]:
    """Return up to `limit` most-recent signatures for wallet (oldest first)."""
    res = _rpc(
        "getSignaturesForAddress",
        [wallet, {"limit": limit}],
    )
    if not isinstance(res, list):
        return []
    # Sort oldest first so we process buys chronologically
    res = sorted(res, key=lambda s: (s.get("blockTime") or 0))
    return res


def get_transaction(signature: str) -> Optional[Dict[str, Any]]:
    """Return parsed transaction JSON or None on miss."""
    res = _rpc(
        "getTransaction",
        [
            signature,
            {"encoding": "json", "commitment": "confirmed", "maxSupportedTransactionVersion": 0},
        ],
    )
    return res if isinstance(res, dict) else None


# ---------------------------------------------------------------------------
# Token-balance diffing
# ---------------------------------------------------------------------------

def _extract_mint_diffs(
    tx: Dict[str, Any],
    wallet: str,
    target_mint: str,
) -> Tuple[float, float, float]:
    """
    For a single tx, return (sol_delta, mint_delta_ui, still_holds_one_token_account).

    - sol_delta: change in wallet's SOL balance in this tx (positive = received SOL)
    - mint_delta_ui: change in wallet's balance of `target_mint` (positive = bought/received)
    - still_holds: True if the wallet STILL has a token account for this mint AFTER the tx

    Notes:
      * Uses uiAmount which already accounts for decimals — we don't need the mint
        decimals to interpret "I now hold 1234.56 of this token".
      * If the wallet never appeared in any token balance in this tx, mint_delta_ui = 0
        and still_holds reflects post-state from previous txs (caller handles).
    """
    meta = tx.get("meta") or {}
    if not meta:
        return 0.0, 0.0, False

    pre_sol = 0.0
    post_sol = 0.0
    # Index SOL balance changes via accountIndex lookups against pre/post Balances
    # (these are lamports for the wallet's main account)
    # We compare by account INDEX, not by owner, because the wallet may have multiple
    # accounts. We rely on the fact that the tx.message.accountKeys[i] is the owner pubkey.
    message = tx.get("message") or {}
    account_keys = message.get("accountKeys") or []
    # accountKeys entries may be plain strings OR {"pubkey": "...", "signer": ...}
    key_to_idx: Dict[str, int] = {}
    for i, k in enumerate(account_keys):
        if isinstance(k, str):
            key_to_idx[k] = i
        elif isinstance(k, dict) and k.get("pubkey"):
            key_to_idx[k["pubkey"]] = i

    wallet_idx = key_to_idx.get(wallet)
    pre_balances = meta.get("preBalances") or []
    post_balances = meta.get("postBalances") or []
    if wallet_idx is not None and wallet_idx < len(pre_balances) and wallet_idx < len(post_balances):
        # Convert lamports → SOL
        pre_sol = pre_balances[wallet_idx] / 1e9
        post_sol = post_balances[wallet_idx] / 1e9
    sol_delta = post_sol - pre_sol

    # Token balance diffs
    pre_tokens = {b["accountIndex"]: b for b in (meta.get("preTokenBalances") or [])}
    post_tokens = {b["accountIndex"]: b for b in (meta.get("postTokenBalances") or [])}

    mint_delta_ui = 0.0
    still_holds = False
    # We need to check ALL token accounts in this tx, see which ones are owned by `wallet`
    # AND have mint == target_mint.
    # Token balance entries include "owner" field. (Helius / parsed RPCs include it;
    # public mainnet-beta sometimes doesn't — in that case we miss token diffs.)
    for idx, post_bal in post_tokens.items():
        if post_bal.get("mint") != target_mint:
            continue
        if post_bal.get("owner") != wallet:
            continue
        post_amt = float(post_bal.get("uiTokenAmount", {}).get("uiAmount") or 0)
        pre_bal = pre_tokens.get(idx)
        pre_amt = float(pre_bal.get("uiTokenAmount", {}).get("uiAmount") or 0) if pre_bal else 0.0
        mint_delta_ui += post_amt - pre_amt
        if post_amt > 0:
            still_holds = True

    # Also check pre-only entries (account closed in this tx) so we still credit the sell
    for idx, pre_bal in pre_tokens.items():
        if pre_bal.get("mint") != target_mint:
            continue
        if pre_bal.get("owner") != wallet:
            continue
        if idx in post_tokens:
            continue  # already counted above
        pre_amt = float(pre_bal.get("uiTokenAmount", {}).get("uiAmount") or 0)
        # closed account → full amount was "spent"
        mint_delta_ui -= pre_amt

    return sol_delta, mint_delta_ui, still_holds


# ---------------------------------------------------------------------------
# Core: profile one wallet × one mint
# ---------------------------------------------------------------------------

def _profile_uncached(wallet: str, mint: str, label: Optional[str] = None) -> Dict[str, Any]:
    """
    Walk the wallet's recent txs and reconstruct PnL for `mint`.
    Pure function given RPC behavior — easy to unit-test with fixture txs.
    """
    sigs = get_signatures(wallet, MAX_SIGS_PER_WALLET)

    buys_sol = 0.0
    sells_sol = 0.0
    buys_ui = 0.0
    sells_ui = 0.0
    current_balance_ui = 0.0  # last seen post-balance, naive but works for non-closed ATAs
    first_buy_ts: Optional[int] = None
    last_action_ts: Optional[int] = None
    mint_tx_count = 0
    still_holds = False

    for sig_info in sigs:
        sig = sig_info.get("signature")
        ts = sig_info.get("blockTime")
        if not sig:
            continue
        tx = get_transaction(sig)
        if not tx:
            continue
        sol_delta, mint_delta_ui, holds = _extract_mint_diffs(tx, wallet, mint)
        if mint_delta_ui == 0.0 and not holds:
            # this tx didn't touch our mint at all
            continue
        mint_tx_count += 1
        if ts:
            last_action_ts = ts

        if mint_delta_ui > 0:
            # bought/received this mint → SOL left the wallet (rough)
            buys_ui += mint_delta_ui
            # sol_delta is wallet's net SOL change. For a direct SOL→mint swap,
            # it's strongly negative. For routed swaps, it's a noisy signal.
            # Use abs(sol_delta) only when it matches the direction we'd expect
            # (negative on buy). Otherwise, fall back to ui-based heuristic.
            if sol_delta < 0:
                buys_sol += abs(sol_delta)
            else:
                # SOL went UP while mint went UP — likely a transfer-in or
                # a routed swap that net-positived the SOL leg. Cost basis
                # is unknown; we count 0 here and flag later.
                pass
            if first_buy_ts is None and ts:
                first_buy_ts = ts

        elif mint_delta_ui < 0:
            # sold/sent this mint → SOL came in (rough)
            sells_ui += abs(mint_delta_ui)
            if sol_delta > 0:
                sells_sol += sol_delta

        # update running balance (heuristic) — applied to BOTH branches
        current_balance_ui = max(0.0, current_balance_ui + mint_delta_ui)
        still_holds = holds or current_balance_ui > 0

    realized_pnl_sol = sells_sol - buys_sol
    roi_pct: Optional[float] = None
    if buys_sol > 0:
        roi_pct = (realized_pnl_sol / buys_sol) * 100.0

    # Win = made money AND had at least one trade
    win: Optional[bool] = None
    if mint_tx_count > 0 and (buys_sol > 0 or sells_sol > 0):
        win = realized_pnl_sol > 0

    total_bought_ui = buys_ui
    still_holding_pct = 0.0
    if total_bought_ui > 0:
        still_holding_pct = max(0.0, min(100.0, (current_balance_ui / total_bought_ui) * 100.0))

    return {
        "wallet": wallet,
        "label": label or wallet[:6] + "…" + wallet[-4:],
        "mint": mint,
        "tx_count": len(sigs),
        "mint_tx_count": mint_tx_count,
        "first_buy_ts": first_buy_ts,
        "last_action_ts": last_action_ts,
        "buys_sol": round(buys_sol, 6),
        "sells_sol": round(sells_sol, 6),
        "realized_pnl_sol": round(realized_pnl_sol, 6),
        "roi_pct": round(roi_pct, 2) if roi_pct is not None else None,
        "still_holds_pct": round(still_holding_pct, 2),
        "current_balance_ui": round(current_balance_ui, 4),
        "win": win,
        "behavior_tags": [],
        "_profiled_at": int(time.time()),
    }


def profile_wallet(
    wallet: str,
    mint: str,
    label: Optional[str] = None,
    *,
    use_cache: bool = True,
    force_refresh: bool = False,
) -> Dict[str, Any]:
    """
    Public entry: profile a single wallet × mint, with cache.

    Returns the profile dict (always; never raises for missing data —
    callers should check `mint_tx_count == 0` for "no trading activity found").
    """
    if use_cache and not force_refresh:
        cached = _cache_get(wallet, mint)
        if cached:
            # preserve any caller-provided label override
            if label and not cached.get("label"):
                cached["label"] = label
            return cached

    profile = _profile_uncached(wallet, mint, label=label)
    if use_cache:
        _cache_put(wallet, mint, profile)
    return profile


def profile_top_holders(
    mint: str,
    top_n: int = 5,
    *,
    holder_provider=None,
    use_cache: bool = True,
) -> List[Dict[str, Any]]:
    """
    Get top N holders for `mint`, then profile each one's PnL.

    `holder_provider` is a callable(mint, top_n) → List[{address, label?, pct?}].
    Defaults to holder_analyzer.get_top_holders. Inject for tests.

    Returns list of profile dicts (one per holder). Order: top-N by holdings.
    """
    if holder_provider is None:
        from holder_analyzer import get_top_holders  # local import to avoid cycle
        holder_provider = get_top_holders

    holders = holder_provider(mint, top_n) or []
    profiles: List[Dict[str, Any]] = []
    for h in holders:
        addr = h.get("address")
        if not addr:
            continue
        try:
            p = profile_wallet(
                addr,
                mint,
                label=h.get("label"),
                use_cache=use_cache,
            )
            # carry the holder pct through so the website can rank
            p["holder_pct"] = h.get("pct")
            profiles.append(p)
        except Exception as e:  # never let one bad wallet kill the batch
            log.warning("profile_wallet(%s, %s) failed: %s", addr, mint, e)
            profiles.append({
                "wallet": addr,
                "label": h.get("label") or addr[:6] + "…" + addr[-4:],
                "mint": mint,
                "error": str(e),
            })
    return profiles


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _main() -> int:
    """Tiny CLI for ad-hoc profiling. Real cron lives in profile_top_holders.py."""
    global MAX_SIGS_PER_WALLET
    import argparse
    p = argparse.ArgumentParser(description="Profile a single wallet × mint PnL.")
    p.add_argument("wallet", help="Solana wallet address")
    p.add_argument("mint", help="Token mint address")
    p.add_argument("--label", default=None)
    p.add_argument("--no-cache", action="store_true")
    p.add_argument("--max-sigs", type=int, default=MAX_SIGS_PER_WALLET)
    args = p.parse_args()

    MAX_SIGS_PER_WALLET = args.max_sigs

    profile = profile_wallet(
        args.wallet,
        args.mint,
        label=args.label,
        use_cache=not args.no_cache,
    )
    print(json.dumps(profile, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())