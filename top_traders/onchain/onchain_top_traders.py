#!/usr/bin/env python3
"""
onchain_top_traders.py — Jiro Sniper Net module.

Scan on-chain transaction history for a Solana token mint and return the
top N wallets by either realized PnL (default) or buy/sell frequency.
Uses the shared multi-provider RPC client at `~/ruangkerja/jiro/rpc_client.py`
(no custom HTTP layer).

WHY:
- "Smart money" wallets rotate addresses. One-shot discovery goes stale fast.
  This module is meant to be fed by a cron so the cabal_detector stays
  current.
- For each top wallet we also fetch the current SOL balance and whether
  they're still holding the mint, so cabal_detector can expire stale entries.

INPUT:
- A Solana token mint (CA). 32-byte base58 address.

OUTPUT:
- JSON list of dicts (one per top wallet):
    {
      "wallet_address": str,
      "buys": int,                    # SPL transfers of mint OUT to wallet
      "sells": int,                   # SPL transfers of mint IN from wallet
      "buy_volume_token": float,      # raw token amount bought (UI units)
      "sell_volume_token": float,
      "buy_volume_sol": float,        # SOL value (from inner swap price or fallback)
      "sell_volume_sol": float,
      "realized_pnl_sol": float,      # sell_sol - avg_buy_price * sell_token (FIFO-ish)
      "current_sol_balance": float,   # live SOL balance
      "still_holding": bool,          # has token account for this mint with amount>0
      "first_seen_ts": int|None,
      "last_active_ts": int|None,
      "source": "onchain_rpc",
      "tx_signatures_sampled": int,
      "is_program": bool              # True if skipped for being a program
    }

FILES:
- cache/{CA}_txs.jsonl     — raw fetched txs, one per line. TTL 1h.
- output/{CA}_traders.json — final top-N list, ranked by chosen strategy.

CLI:
  python onchain_top_traders.py <CA> [--strategy pnl|freq] [--limit 10]

EXIT CODES:
- Always 0. Errors are encoded in the output (warnings, source='rpc_unavailable').

TUNABLES (env):
  ONCHAIN_MAX_SIGS          — max sigs to scan (default 1000)
  ONCHAIN_TX_CONCURRENCY    — max parallel getTransaction workers (default 10)
  ONCHAIN_BUDGET_S          — total runtime budget seconds (default 60)
  ONCHAIN_MIN_TX_AGE_DAYS   — drop wallets whose last_active older than this (default 30)
  ONCHAIN_MIN_TX_COUNT      — drop wallets with < N total txs (default 2)
  ONCHAIN_CACHE_TTL_S       — tx cache TTL seconds (default 3600)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import logging
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Optional, Tuple
from urllib.request import urlopen, Request
from urllib.error import URLError, HTTPError

# ---------------------------------------------------------------------------
# Make jiro's rpc_client importable when this module is run directly
# (it lives one level above us in the repo).
# ---------------------------------------------------------------------------
_HERE = os.path.dirname(os.path.abspath(__file__))
_JIRO_ROOT = os.path.dirname(os.path.dirname(_HERE))  # ~/ruangkerja/jiro

# ---------------------------------------------------------------------------
# Tiny .env loader (mirrors run_bot.load_dotenv) so RPC_URL/Helius keys are
# picked up when invoked outside run_bot. Does NOT override already-set env.
# MUST run BEFORE importing rpc_client, since rpc_client freezes its provider
# list at import time from the env.
# ---------------------------------------------------------------------------
def _bootstrap_dotenv() -> None:
    env_path = os.path.join(_JIRO_ROOT, ".env")
    if not os.path.exists(env_path):
        return
    try:
        with open(env_path, "r", encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key, val = key.strip(), val.strip()
                if not key or key in os.environ:
                    continue
                if len(val) >= 2 and val[0] in ("'", '"') and val[-1] == val[0]:
                    val = val[1:-1]
                os.environ[key] = val
    except OSError:
        pass


_bootstrap_dotenv()

if _JIRO_ROOT not in sys.path:
    sys.path.insert(0, _JIRO_ROOT)

from rpc_client import get_rpc_client  # noqa: E402  — shared multi-provider client

log = logging.getLogger("onchain_top_traders")
logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
MAX_SIGS = int(os.environ.get("ONCHAIN_MAX_SIGS", "1000"))
TX_CONCURRENCY = int(os.environ.get("ONCHAIN_TX_CONCURRENCY", "5"))
ENRICHMENT_CONCURRENCY = int(os.environ.get("ONCHAIN_ENRICH_CONCURRENCY", "3"))
ENRICHMENT_RETRIES = int(os.environ.get("ONCHAIN_ENRICH_RETRIES", "3"))
# Each retry waits progressively longer to ride out Helius's per-IP cooldown
# window (~30s). Total max wait per call: 1 + 6 + 18 = 25s.
ENRICHMENT_RETRY_SLEEP_S = float(os.environ.get("ONCHAIN_ENRICH_RETRY_SLEEP_S", "3.0"))
BUDGET_S = float(os.environ.get("ONCHAIN_BUDGET_S", "60"))
MIN_TX_AGE_DAYS = int(os.environ.get("ONCHAIN_MIN_TX_AGE_DAYS", "30"))
MIN_TX_COUNT = int(os.environ.get("ONCHAIN_MIN_TX_COUNT", "2"))
CACHE_TTL_S = int(os.environ.get("ONCHAIN_CACHE_TTL_S", "3600"))
SOL_PRICE_CACHE_TTL_S = 300  # 5 min for coingecko SOL/USD
SOL_PRICE_URL = (
    "https://api.coingecko.com/api/v3/simple/price?ids=solana&vs_currencies=usd"
)

CACHE_DIR = os.path.join(_HERE, "cache")
OUTPUT_DIR = os.path.join(_HERE, "output")

# Well-known Solana program / system addresses that shouldn't be treated
# as "wallets". Everything here has been a fee-payer or destination on
# real on-chain traffic but is clearly not a trader's wallet.
_KNOWN_PROGRAMS = {
    "11111111111111111111111111111111",          # System Program
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",  # Token Program
    "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL",  # Associated Token Program
    "ComputeBudget111111111111111111111111111111",
    "JUP6LkbZbjS1jKKwapdHNy24zcUvxsEtbKDRAsMQifX",  # Jupiter v6
    "JUP4Fb2cqiRUcaTHdrPC8h2gNsA2ETXiPDD33WcGuJB",  # Jupiter v4
    "whirLbMiicVdio4qvUfM5KAgimbCtYJmybW91m6RD",    # Whirlpools
    "9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM",  # Pump.fun
    "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P",  # Pump.fun (other id)
    "So11111111111111111111111111111111111111112",  # Wrapped SOL mint
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC mint
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",  # USDT mint
}

# Program-owned token accounts start with this prefix (ATA program owns them).
# More robust: getAccountInfo + check owner. We do both.


# ---------------------------------------------------------------------------
# SOL price (coingecko) with 5-min cache
# ---------------------------------------------------------------------------
_sol_price_cache: Dict[str, Any] = {"ts": 0.0, "price": 0.0}
_sol_price_lock = threading.Lock()


def get_sol_price_usd() -> float:
    now = time.time()
    with _sol_price_lock:
        if now - _sol_price_cache["ts"] < SOL_PRICE_CACHE_TTL_S:
            return _sol_price_cache["price"]
    try:
        req = Request(SOL_PRICE_URL, headers={"User-Agent": "jiro-onchain/1.0"})
        with urlopen(req, timeout=4) as r:
            payload = json.loads(r.read().decode("utf-8"))
        price = float(((payload or {}).get("solana") or {}).get("usd") or 0.0)
    except (URLError, HTTPError, ValueError, TimeoutError) as e:
        log.warning("coingecko SOL/USD fetch failed: %s", e)
        price = 0.0
    with _sol_price_lock:
        # Only update if newer data succeeded — keep stale value on failure.
        if price > 0:
            _sol_price_cache["price"] = price
        _sol_price_cache["ts"] = now
        return _sol_price_cache["price"]


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------
def _cache_path(ca: str) -> str:
    return os.path.join(CACHE_DIR, f"{ca}_txs.jsonl")


def _cache_is_fresh(ca: str) -> bool:
    p = _cache_path(ca)
    if not os.path.exists(p):
        return False
    age = time.time() - os.path.getmtime(p)
    return age < CACHE_TTL_S


def _cache_load_txs(ca: str) -> List[Dict[str, Any]]:
    p = _cache_path(ca)
    out: List[Dict[str, Any]] = []
    if not os.path.exists(p):
        return out
    try:
        with open(p, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError as e:
        log.warning("cache read failed for %s: %s", ca, e)
    return out


def _cache_append_tx(ca: str, tx: Dict[str, Any]) -> None:
    p = _cache_path(ca)
    try:
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps(tx, separators=(",", ":")) + "\n")
    except OSError as e:
        log.warning("cache write failed for %s: %s", ca, e)


def _cache_clear(ca: str) -> None:
    p = _cache_path(ca)
    try:
        if os.path.exists(p):
            os.remove(p)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# RPC helpers
# ---------------------------------------------------------------------------
def _client():
    return get_rpc_client()


def _rpc_with_retry(rpc, method: str, params: list, *, max_retries: int = ENRICHMENT_RETRIES,
                    sleep_s: float = ENRICHMENT_RETRY_SLEEP_S,
                    deadline: Optional[float] = None) -> Optional[Dict[str, Any]]:
    """Wrapper around RpcClient.rpc that retries on transient failures
    (None result). Useful for the post-process enrichment calls where a
    single 429 from Helius shouldn't poison the whole row.

    Sleeps grow exponentially so we have a chance to clear the per-provider
    cooldown window that RpcClient applies after a 429 (default 30s)."""
    for attempt in range(max_retries + 1):
        if deadline is not None and time.time() >= deadline:
            return None
        res = rpc.rpc(method, params)
        if res is not None:
            return res
        if attempt < max_retries:
            time.sleep(sleep_s * (3 ** attempt))
    return None


def _get_signatures_paged(rpc, mint: str, max_sigs: int) -> List[Dict[str, Any]]:
    """Fetch up to `max_sigs` signatures for `mint`, paging at 1000/page."""
    out: List[Dict[str, Any]] = []
    page_limit = 1000  # Helius cap per getSignaturesForAddress call
    before: Optional[str] = None
    while len(out) < max_sigs:
        params: List[Any] = [mint, {"limit": page_limit}]
        if before:
            params[1]["before"] = before
        res = rpc.rpc("getSignaturesForAddress", params)
        if not isinstance(res, list) or not res:
            break
        out.extend(res)
        if len(res) < page_limit:
            break  # last page
        before = res[-1].get("signature")
        if not before:
            break
    return out[:max_sigs]


def _fetch_tx(rpc, sig_info: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Fetch a single parsed transaction with v0 support."""
    sig = sig_info.get("signature")
    if not sig:
        return None
    res = rpc.rpc(
        "getTransaction",
        [sig, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}],
    )
    if not isinstance(res, dict):
        return None
    res["_sig"] = sig
    res["_blockTime"] = sig_info.get("blockTime")
    return res


def _fetch_many_txs(
    rpc, sigs: List[Dict[str, Any]], concurrency: int, deadline: float,
) -> List[Dict[str, Any]]:
    """Parallel getTransaction with concurrency cap. Stops spawning new tasks
    once the deadline is past. Returns successful tx dicts (parsed)."""
    results: List[Optional[Dict[str, Any]]] = [None] * len(sigs)
    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as ex:
        futures = {}
        for i, s in enumerate(sigs):
            if time.time() >= deadline:
                break
            fut = ex.submit(_fetch_tx, rpc, s)
            futures[fut] = i
        for fut in as_completed(futures):
            i = futures[fut]
            try:
                tx = fut.result()
            except Exception as e:  # noqa: BLE001
                log.debug("tx fetch err: %s", e)
                tx = None
            results[i] = tx
    return [r for r in results if r is not None]


# ---------------------------------------------------------------------------
# Transaction parsing
# ---------------------------------------------------------------------------
WSOL_MINT = "So11111111111111111111111111111111111111112"


def _wallet_account_index(keys: List[Any], wallet: str) -> Optional[int]:
    """Find the index of `wallet` in the tx's accountKeys list."""
    def _key_addr(k: Any) -> str:
        if isinstance(k, dict):
            return k.get("pubkey") or k.get("address") or ""
        return str(k)

    for i, k in enumerate(keys):
        if _key_addr(k) == wallet:
            return i
    return None


def _token_balance_for_mint(
    balances: List[Dict[str, Any]], idx: int, mint: str
) -> int:
    """Return raw amount in `balances` for account index `idx` and `mint`.
    Returns 0 if not found."""
    for tb in balances:
        if tb.get("accountIndex") == idx and tb.get("mint") == mint:
            return int((tb.get("uiTokenAmount") or {}).get("amount") or "0")
    return 0


def _token_balance_changes(tx: Dict[str, Any], wallet: str) -> Tuple[float, float, float]:
    """Return (token_delta, sol_value_lamports, wsol_value_lamports) for `wallet`.

    token_delta is the signed delta of the mint of interest (set via
    tx["_mint"]), in RAW units. Positive = wallet received the mint.

    sol_value_lamports is the native SOL balance change. WSOL is a separate
    SPL token — pump.fun / Raydium / Jupiter route buys through the user's
    WSOL token account, so we ALSO compute wsol_value_lamports from the
    WSOL mint balance diff. Callers that need "SOL value of this trade" should
    use sol_value_lamports + wsol_value_lamports.

    Returns (0.0, 0, 0) if `wallet` isn't in this tx.
    """
    meta = tx.get("meta") or {}
    keys = ((tx.get("transaction") or {}).get("message") or {}).get("accountKeys") or []
    idx = _wallet_account_index(keys, wallet)
    if idx is None:
        return 0.0, 0, 0

    mint = tx.get("_mint")
    pre_tb = meta.get("preTokenBalances") or []
    post_tb = meta.get("postTokenBalances") or []

    # Mint-of-interest delta (e.g. the memecoin being traded)
    pre_amt = _token_balance_for_mint(pre_tb, idx, mint)
    post_amt = _token_balance_for_mint(post_tb, idx, mint)
    token_delta = post_amt - pre_amt

    # Native SOL delta (sign convention: negative = spent, positive = received)
    pre_balances = meta.get("preBalances") or []
    post_balances = meta.get("postBalances") or []
    pre_bal = pre_balances[idx] if idx < len(pre_balances) else 0
    post_bal = post_balances[idx] if idx < len(post_balances) else 0
    sol_delta = int(post_bal) - int(pre_bal)

    # WSOL delta (1:1 with SOL; if the user swapped via WSOL account the native
    # SOL delta will be 0 but WSOL will move). Sign convention: negative when
    # wallet's WSOL went DOWN (spent on buy), positive when it went UP.
    pre_wsol = _token_balance_for_mint(pre_tb, idx, WSOL_MINT)
    post_wsol = _token_balance_for_mint(post_tb, idx, WSOL_MINT)
    wsol_delta = post_wsol - pre_wsol

    return float(token_delta), sol_delta, wsol_delta


def _is_wallet_swap(tx: Dict[str, Any], wallet: str) -> bool:
    """Heuristic: does this tx look like a swap (vs. a pure transfer / airdrop)?

    Looks at inner instructions for a token swap pattern (Jupiter / Raydium /
    Orca / Pump.fun). If we can't find swap markers we still treat it as a
    transfer — that's the conservative choice for PnL purposes (no PnL is
    computed for pure transfers, since we can't infer price).
    """
    ixs = ((tx.get("transaction") or {}).get("message") or {}).get("instructions") or []
    inner = (tx.get("meta") or {}).get("innerInstructions") or []
    # Look for known swap program IDs in any instruction.
    SWAP_PROGRAMS = {
        "JUP6LkbZbjS1jKKwapdHNy24zcUvxsEtbKDRAsMQifX",  # Jupiter v6
        "JUP4Fb2cqiRUcaTHdrPC8h2gNsA2ETXiPDD33WcGuJB",  # Jupiter v4
        "JUP3c2Uh3WA4Ng33tw6APaDgQ7Y6aqt5w8B5eC1S7",   # Jupiter v3 (legacy)
        "whirLbMiicVdio4qvUfM5KAgimbCtYJmybW91m6RD",    # Orca Whirlpool
        "9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM",  # Pump.fun
        "CAMMCzo5YL8w4VFF8KVHrKjyGGmmfK58rtwG5CM5uSbX",  # Raydium CLMM
        "675kPX9MHTjS2zt1qfr1WiHuHqDc1d1KbQXq5pjEswbU",  # Raydium AMM v4
    }
    for ix in ixs:
        prog = ix.get("programId") if isinstance(ix, dict) else None
        if prog in SWAP_PROGRAMS:
            return True
    for group in inner:
        for ix in (group.get("instructions") or []):
            prog = ix.get("programId") if isinstance(ix, dict) else None
            if prog in SWAP_PROGRAMS:
                return True
    return False


def _swap_sol_value(tx: Dict[str, Any], wallet: str, fee_payer: Optional[str] = None) -> Tuple[float, float]:
    """If the tx looks like a swap involving `wallet`, extract the SOL value
    of the buy/sell (in lamports, signed: negative = SOL spent, positive =
    SOL received).

    Pump.fun / Jupiter / Raydium wraps SOL in WSOL and routes trades through
    the fee payer's WSOL account. That means:

      - The buyer's own WSOL/SOL balance often DOESN'T move (their WSOL
        sits in a separate account, untouched — the fee payer pays).
      - The fee payer's WSOL/SOL balance moves by the trade's SOL value.

    To capture the real SOL value of a trade we therefore sum:

      (wallet's own native SOL delta)
      + (wallet's own WSOL delta)               # if user paid from their own WSOL
      + (fee_payer's native SOL delta, if not wallet)
      + (fee_payer's WSOL delta, if not wallet) # pump.fun / most routers

    Returns (sol_value_lamports, abs_token_delta_raw).
    sol_value_lamports is negative for buys, positive for sells.
    """
    token_delta, sol_delta, wsol_delta = _token_balance_changes(tx, wallet)
    if fee_payer and fee_payer != wallet:
        _, fp_sol, fp_wsol = _token_balance_changes(tx, fee_payer)
        sol_delta += fp_sol
        wsol_delta += fp_wsol
    return float(sol_delta + wsol_delta), token_delta


# ---------------------------------------------------------------------------
# Wallet classification
# ---------------------------------------------------------------------------
def _is_program_address(rpc, addr: str) -> bool:
    """Check if `addr` is owned by a program (executable). Also reject
    known program IDs without a lookup."""
    if addr in _KNOWN_PROGRAMS:
        return True
    try:
        info = rpc.rpc("getAccountInfo", [addr, {"encoding": "jsonParsed"}])
    except Exception:
        return False
    val = (info or {}).get("value") if isinstance(info, dict) else None
    if not val:
        return False  # empty accounts we couldn't resolve — treat as wallet
    if val.get("executable"):
        return True
    owner = val.get("owner")
    if owner and owner != "11111111111111111111111111111111":
        # Non-system owner → it's a PDA / program-owned account, not a wallet
        return True
    return False


# ---------------------------------------------------------------------------
# Main scan
# ---------------------------------------------------------------------------
def scan_top_traders(
    ca: str,
    *,
    strategy: str = "pnl",
    limit: int = 10,
    max_sigs: int = MAX_SIGS,
    tx_concurrency: int = TX_CONCURRENCY,
    budget_s: float = BUDGET_S,
    use_cache: bool = True,
    rpc=None,
) -> Dict[str, Any]:
    """Scan recent on-chain history for `ca` and return top N wallets.

    Returns a dict with:
      - "traders":  List[Dict[str, Any]]
      - "warnings": List[str]
      - "stats":    Dict[str, Any]
    """
    started = time.time()
    deadline = started + budget_s
    warnings: List[str] = []
    stats: Dict[str, Any] = {
        "mint": ca,
        "strategy": strategy,
        "limit": limit,
        "max_sigs": max_sigs,
        "tx_concurrency": tx_concurrency,
        "budget_s": budget_s,
    }

    if rpc is None:
        rpc = _client()

    # 1) Signatures
    sigs: List[Dict[str, Any]] = []
    cached: List[Dict[str, Any]] = []
    cache_used = False
    if use_cache and _cache_is_fresh(ca):
        cached = _cache_load_txs(ca)
        if cached:
            sigs = [{"signature": c.get("_sig"), "blockTime": c.get("_blockTime")}
                    for c in cached if c.get("_sig")]
            cache_used = True
            stats["cache_loaded_txs"] = len(cached)
    if not sigs:
        if cache_used:
            warnings.append("cache was marked fresh but empty; refetching")
        _cache_clear(ca)
        sigs = _get_signatures_paged(rpc, ca, max_sigs)
        if not sigs:
            warnings.append("getSignaturesForAddress returned no signatures")
            return {
                "traders": [],
                "warnings": warnings,
                "stats": {**stats, "result": "rpc_unavailable", "elapsed_s": time.time() - started},
            }
    stats["signatures_fetched"] = len(sigs)

    # 2) Parallel getTransaction (or use cache)
    txs: List[Dict[str, Any]] = []
    if use_cache and cache_used:
        txs = cached
    else:
        if time.time() >= deadline:
            warnings.append("deadline hit before tx fetch began")
        else:
            txs = _fetch_many_txs(rpc, sigs, tx_concurrency, deadline)
            # Persist to cache.
            for tx in txs:
                _cache_append_tx(ca, tx)
    stats["txs_decoded"] = len(txs)
    if not txs:
        warnings.append("no transactions decoded (all RPC calls failed?)")
        return {
            "traders": [],
            "warnings": warnings,
            "stats": {**stats, "result": "rpc_unavailable", "elapsed_s": time.time() - started},
        }

    # 3) Build per-wallet aggregates.
    # We need to classify wallets. To save RPC calls, we lazy-classify each
    # unique address we see ONCE (with a deadline-aware skip on exhaustion).
    wallets: Dict[str, Dict[str, Any]] = {}
    classification_cache: Dict[str, bool] = {}
    classification_lock = threading.Lock()
    classify_budget_left = 30  # hard cap on getAccountInfo lookups

    def _classify(addr: str) -> bool:
        nonlocal classify_budget_left
        with classification_lock:
            if addr in classification_cache:
                return classification_cache[addr]
            if classify_budget_left <= 0:
                return False
            classify_budget_left -= 1
        is_prog = _is_program_address(rpc, addr)
        with classification_lock:
            classification_cache[addr] = is_prog
        return is_prog

    for tx in txs:
        block_time = tx.get("_blockTime") or (tx.get("blockTime"))
        tx_message = (tx.get("transaction") or {}).get("message") or {}
        keys = tx_message.get("accountKeys") or []

        def _key_addr(k: Any) -> str:
            if isinstance(k, dict):
                return k.get("pubkey") or k.get("address") or ""
            return str(k)

        # Find fee payer (first account key, signer)
        fee_payer = ""
        for k in keys:
            if isinstance(k, dict) and k.get("signer"):
                fee_payer = _key_addr(k)
                break
        if not fee_payer and keys:
            fee_payer = _key_addr(keys[0])
        if not fee_payer:
            continue

        # The mint of interest is `ca`; tag the tx so _token_balance_changes
        # can pull only that mint's balance changes.
        tx["_mint"] = ca

        # For each signer (unique wallet), compute token + SOL delta.
        # Spl-transfers that involve the mint are either buys or sells.
        signers: List[str] = []
        seen_signers: set = set()
        for k in keys:
            if isinstance(k, dict) and k.get("signer"):
                addr = _key_addr(k)
                if addr and addr not in seen_signers:
                    signers.append(addr)
                    seen_signers.add(addr)

        # Also consider non-signer wallets that received/sent the mint —
        # this catches cases where a script wallet pays fees but a different
        # address is the trader. We pull every unique address referenced in
        # pre/postTokenBalances for `ca`.
        meta = tx.get("meta") or {}
        all_token_accts = (meta.get("preTokenBalances") or []) + (meta.get("postTokenBalances") or [])
        for tb in all_token_accts:
            if tb.get("mint") != ca:
                continue
            ai = tb.get("accountIndex")
            if ai is None or ai >= len(keys):
                continue
            addr = _key_addr(keys[ai])
            if addr and addr not in seen_signers:
                signers.append(addr)
                seen_signers.add(addr)

        for w in signers:
            if not w:
                continue
            # Classification: only skip obviously-program addresses.
            # We do this lazily and within budget.
            try:
                if _classify(w):
                    if w not in wallets:
                        wallets[w] = _new_wallet_row(w)
                    wallets[w]["is_program"] = True
                    continue
            except Exception:
                pass

            token_delta, _sol_unused, _wsol_unused = _token_balance_changes(tx, w)
            if token_delta == 0:
                # Not involved in this mint's movement. Still update last_active.
                if w in wallets:
                    if block_time and (wallets[w]["last_active_ts"] is None or block_time > wallets[w]["last_active_ts"]):
                        wallets[w]["last_active_ts"] = block_time
                continue

            sol_delta, _ = _swap_sol_value(tx, w, fee_payer=fee_payer)
            row = wallets.setdefault(w, _new_wallet_row(w))
            row["is_program"] = False

            if token_delta > 0:
                # BUY (received mint)
                row["buys"] += 1
                row["buy_volume_token"] += token_delta
                if sol_delta < 0:
                    row["buy_volume_sol"] += abs(sol_delta) / 1e9
            else:
                # SELL (sent mint)
                row["sells"] += 1
                row["sell_volume_token"] += abs(token_delta)
                if sol_delta > 0:
                    row["sell_volume_sol"] += sol_delta / 1e9

            # Realized PnL is sell_sol - average_buy_price * sell_tokens.
            # Simple FIFO using weighted avg cost.
            if row["buy_volume_token"] > 0:
                avg_cost = row["buy_volume_sol"] / row["buy_volume_token"]
                cost_of_sold = avg_cost * abs(token_delta if token_delta < 0 else 0)
                proceeds_of_sold = sol_delta / 1e9 if (token_delta < 0 and sol_delta > 0) else 0.0
                # Update realized incrementally: only attribute cost when we see sells.
                if token_delta < 0:
                    row["realized_pnl_sol"] += (proceeds_of_sold - cost_of_sold)

            if block_time:
                if row["first_seen_ts"] is None or block_time < row["first_seen_ts"]:
                    row["first_seen_ts"] = block_time
                if row["last_active_ts"] is None or block_time > row["last_active_ts"]:
                    row["last_active_ts"] = block_time

    # 4) Filter noise (program-owned, stale, low-activity).
    now_ts = int(time.time())
    max_age_s = MIN_TX_AGE_DAYS * 86400
    eligible: List[Dict[str, Any]] = []
    for w, row in wallets.items():
        if row["is_program"]:
            continue
        if (row["buys"] + row["sells"]) < MIN_TX_COUNT:
            continue
        if row["last_active_ts"] and (now_ts - row["last_active_ts"]) > max_age_s:
            continue
        # Estimate unrealized PnL: position size in tokens * current SOL price
        # (we don't know the token's current price — approximate by avg cost
        # basis zero, so unrealized = current holdings * avg_cost).
        position = row["buy_volume_token"] - row["sell_volume_token"]
        if row["buy_volume_token"] > 0 and position > 0:
            avg_cost = row["buy_volume_sol"] / row["buy_volume_token"]
            row["unrealized_pnl_estimate"] = avg_cost * position
        else:
            row["unrealized_pnl_estimate"] = 0.0
        row["wallet_address"] = w
        row["source"] = "onchain_rpc"
        row["tx_signatures_sampled"] = len(txs)
        eligible.append(row)
    stats["wallets_after_filter"] = len(eligible)

    # 5) Rank by strategy.
    if strategy == "freq":
        eligible.sort(
            key=lambda r: ((r["buys"] + r["sells"]), r["realized_pnl_sol"]),
            reverse=True,
        )
    else:  # pnl
        eligible.sort(
            key=lambda r: (r["realized_pnl_sol"] + r.get("unrealized_pnl_estimate", 0.0)),
            reverse=True,
        )

    top = eligible[: max(1, limit)]

    # 6) Enrich top wallets with current balance + holding check.
    # Run enrichment in parallel (low concurrency to avoid 429) with retry.
    sol_price_usd = get_sol_price_usd()
    stats["sol_price_usd"] = sol_price_usd

    def _enrich_one(r: Dict[str, Any]) -> None:
        addr = r["wallet_address"]
        enrich_deadline = deadline  # shared budget
        # Current SOL balance — Helius returns {'context':..., 'value': <lamports>}.
        bal = _rpc_with_retry(rpc, "getBalance", [addr], deadline=enrich_deadline)
        if isinstance(bal, dict):
            v = bal.get("value")
            r["current_sol_balance"] = float(v) / 1e9 if v is not None else None
        elif isinstance(bal, (int, float)):
            r["current_sol_balance"] = float(bal) / 1e9
        else:
            r["current_sol_balance"] = None

        # Token accounts for mint — still holding?
        tas = _rpc_with_retry(
            rpc,
            "getTokenAccountsByOwner",
            [addr, {"mint": ca}, {"encoding": "jsonParsed"}],
            deadline=enrich_deadline,
        )
        total_amt = 0.0
        tas_list: List[Dict[str, Any]] = []
        if isinstance(tas, dict) and isinstance(tas.get("value"), list):
            tas_list = tas["value"]
        elif isinstance(tas, list):
            tas_list = [t for t in tas if isinstance(t, dict)]
        for ta in tas_list:
            parsed = (((ta.get("account") or {}).get("data") or {}).get("parsed") or {}).get("info") or {}
            amt = float((((ta.get("account") or {}).get("data") or {}).get("parsed") or {})
                        .get("info", {}).get("tokenAmount", {}).get("uiAmount") or 0)
            if parsed.get("mint") == ca:
                total_amt += amt
        r["still_holding"] = total_amt > 0
        r["current_token_balance_ui"] = total_amt

        # Convert realized_pnl_sol to USD for convenience.
        if sol_price_usd > 0:
            r["realized_pnl_usd"] = r["realized_pnl_sol"] * sol_price_usd
        else:
            r["realized_pnl_usd"] = None

    with ThreadPoolExecutor(max_workers=max(1, ENRICHMENT_CONCURRENCY)) as ex:
        futs = [ex.submit(_enrich_one, r) for r in top]
        for f in as_completed(futs):
            try:
                f.result()
            except Exception as e:  # noqa: BLE001
                log.warning("enrich error: %s", e)

    elapsed = time.time() - started
    if elapsed >= budget_s:
        warnings.append(f"budget exceeded: ran {elapsed:.1f}s of {budget_s}s")

    stats["elapsed_s"] = round(elapsed, 2)
    stats["result"] = "ok" if top else "empty"

    # Per-run rpc stats snapshot for debugging rate-limit issues.
    try:
        stats["rpc_provider_stats"] = rpc.stats()
    except Exception:
        pass

    return {"traders": top, "warnings": warnings, "stats": stats}


def _new_wallet_row(wallet: str) -> Dict[str, Any]:
    return {
        "wallet_address": wallet,
        "buys": 0,
        "sells": 0,
        "buy_volume_token": 0.0,
        "sell_volume_token": 0.0,
        "buy_volume_sol": 0.0,
        "sell_volume_sol": 0.0,
        "realized_pnl_sol": 0.0,
        "unrealized_pnl_estimate": 0.0,
        "current_sol_balance": None,
        "still_holding": False,
        "current_token_balance_ui": 0.0,
        "first_seen_ts": None,
        "last_active_ts": None,
        "source": "onchain_rpc",
        "tx_signatures_sampled": 0,
        "is_program": False,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _cli() -> int:
    # Declare global up front so the help-string default expressions (which
    # read MIN_TX_COUNT / MIN_TX_AGE_DAYS at function-def time) and the
    # later override don't trip Python's "used before global" check.
    global MIN_TX_COUNT, MIN_TX_AGE_DAYS
    ap = argparse.ArgumentParser(
        description="Jiro on-chain top trader scanner (PnL or frequency).",
    )
    ap.add_argument("ca", help="Solana token mint address (base58)")
    ap.add_argument("--strategy", choices=("pnl", "freq"), default="pnl")
    ap.add_argument("--limit", type=int, default=10)
    ap.add_argument("--max-sigs", type=int, default=MAX_SIGS)
    ap.add_argument("--concurrency", type=int, default=TX_CONCURRENCY)
    ap.add_argument("--budget", type=float, default=BUDGET_S)
    ap.add_argument("--min-tx-count", type=int, default=MIN_TX_COUNT,
                    help=f"drop wallets with < N total txs (default {MIN_TX_COUNT})")
    ap.add_argument("--min-tx-age-days", type=int, default=MIN_TX_AGE_DAYS,
                    help=f"drop wallets whose last_active older than N days (default {MIN_TX_AGE_DAYS})")
    ap.add_argument("--no-cache", action="store_true",
                    help="ignore cache and re-fetch signatures/txs")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    # Override module-level constants with CLI args (must mutate globals
    # because the filter logic reads them at call time, not capture time).
    MIN_TX_COUNT = args.min_tx_count
    MIN_TX_AGE_DAYS = args.min_tx_age_days

    os.makedirs(CACHE_DIR, exist_ok=True)
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    result = scan_top_traders(
        args.ca,
        strategy=args.strategy,
        limit=args.limit,
        max_sigs=args.max_sigs,
        tx_concurrency=args.concurrency,
        budget_s=args.budget,
        use_cache=not args.no_cache,
    )

    out_path = os.path.join(OUTPUT_DIR, f"{args.ca}_traders.json")
    payload = {
        "mint": args.ca,
        "strategy": args.strategy,
        "limit": args.limit,
        "warnings": result["warnings"],
        "stats": result["stats"],
        "traders": result["traders"],
    }
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)

    if not args.quiet:
        print(json.dumps(payload, indent=2))
        print(f"\n[saved] {out_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(_cli())