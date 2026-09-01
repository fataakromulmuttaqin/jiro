#!/usr/bin/env python3
"""
TRADING ENGINE v2 — smart entry scoring, trailing stop, partial take-profit,
on-chain dump detection, narrative-decay exit.
================================================================================
⚠️  READ THIS BEFORE TOUCHING AUTO_TRADE_ENABLED ⚠️
This module signs real transactions with a real wallet and spends real money.
Memecoins found this early frequently: rug, have unsellable ("honeypot")
contracts, or have near-zero real liquidity. No amount of on-chain analysis
makes this safe — it only makes it LESS blind. This is a scaffold, not an
audited product. Recommended path:
  1. Run with DRY_RUN=true (default) for a while and read the logs.
  2. Test with a burner wallet and a tiny position_size_usd.
  3. Only then consider disabling dry-run, and still keep size small.
Nothing in this file is financial advice.

SECRETS (environment variables — never put these in config.json):
  SOLANA_PRIVATE_KEY   - base58 secret key of the trading wallet. Use a
                          DEDICATED burner wallet, never your main one.
  RPC_URL              - Solana RPC endpoint. Public mainnet-beta is heavily
                          rate-limited; get a free key from Helius/QuickNode/
                          Triton for anything real.
  AUTO_TRADE_ENABLED   - "true" to actually place trades. Default "false".
  DRY_RUN              - "true" simulates everything, sends nothing.
                          Default "true". Both this AND AUTO_TRADE_ENABLED
                          must be explicitly set for real transactions to fire.

TUNABLE PARAMETERS (edit config.json, not env vars — see config.py):
  trading.position_size_usd, take_profit_pct, stop_loss_pct,
  trailing_stop_*, partial_take_profit, max_daily_loss_usd,
  entry_filters.*, onchain_exit_signals.*
"""

import os
import sys
import json
import time
import base64
import uuid
import datetime as dt
from typing import Optional, Dict, Any, List

import requests

try:
    from solders.keypair import Keypair
    from solders.transaction import VersionedTransaction
except ImportError:
    print("Missing deps. Run: pip install solders base58 requests --break-system-packages", file=sys.stderr)
    raise
import base58

import config as cfgmod
import onchain_analyzer
import narrative
import rpc_client
import safety
import holder_analyzer
import smart_money

# ----------------------------------------------------------------------------
# SECRETS / INFRA (env vars only)
# ----------------------------------------------------------------------------

def _bool_env(name: str, default: bool) -> bool:
    val = os.environ.get(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")

RPC_URL = os.environ.get("RPC_URL", "https://api.mainnet-beta.solana.com")
SOLANA_PRIVATE_KEY = os.environ.get("SOLANA_PRIVATE_KEY", "")
AUTO_TRADE_ENABLED = _bool_env("AUTO_TRADE_ENABLED", False)
# CRITICAL: DRY_RUN defaults to TRUE. The only way to disable is an explicit
# env var. config.json CANNOT enable live trading — safety.assert_safe_for_live()
# is the single source of truth at process start.
DRY_RUN = _bool_env("DRY_RUN", True)

# safety gate: if user set both AUTO_TRADE_ENABLED=true and DRY_RUN=false,
# they MUST also set ARM_LIVE_TRADE=YES-I-WANT-LIVE-MONEY-AT-RISK-2026 or we
# hard-fail at process start. Cheap insurance against accidental live mode.
safety.assert_safe_for_live()

SOL_MINT = "So11111111111111111111111111111111111111112"
JUPITER_QUOTE_URL = "https://quote-api.jup.ag/v6/quote"
JUPITER_SWAP_URL = "https://quote-api.jup.ag/v6/swap"
JUPITER_PRICE_URL = "https://api.jup.ag/price/v2"
DEXSCREENER_TOKEN_URL = "https://api.dexscreener.com/latest/dex/tokens"

_HERE = os.path.dirname(os.path.abspath(__file__))
POSITIONS_FILE = os.path.join(_HERE, "positions.json")
LEDGER_FILE = os.path.join(_HERE, "ledger.json")

CFG = cfgmod.load_config()


def reload_config() -> None:
    """Call this to pick up config.json edits without restarting the process."""
    global CFG
    CFG = cfgmod.load_config()


# ----------------------------------------------------------------------------
# WALLET / RPC HELPERS
# ----------------------------------------------------------------------------

class Wallet:
    def __init__(self, secret_b58: str):
        if not secret_b58:
            raise RuntimeError("SOLANA_PRIVATE_KEY not set")
        self.keypair = Keypair.from_base58_string(secret_b58)
        self.pubkey = self.keypair.pubkey()

    def sign(self, versioned_tx: VersionedTransaction) -> VersionedTransaction:
        return VersionedTransaction(versioned_tx.message, [self.keypair])


def rpc_call(method: str, params: list) -> Dict[str, Any]:
    # wrapper that keeps the old "raise on error" semantics for callers
    # that want them (send_tx confirmation), but still uses the failover
    # client under the hood.
    result = rpc_client.rpc_call(method, params)
    if result is None:
        raise RuntimeError(f"RPC {method} failed: every provider unavailable")
    return result


def send_raw_transaction(signed_tx: VersionedTransaction) -> str:
    raw_b64 = base64.b64encode(bytes(signed_tx)).decode("utf-8")
    return rpc_call("sendTransaction", [raw_b64, {"encoding": "base64", "skipPreflight": True, "maxRetries": 3}])


def confirm_signature(signature: str, timeout_s: int = 60) -> bool:
    start = time.time()
    while time.time() - start < timeout_s:
        result = rpc_call("getSignatureStatuses", [[signature], {"searchTransactionHistory": True}])
        status = result.get("value", [None])[0]
        if status and status.get("confirmationStatus") in ("confirmed", "finalized"):
            return status.get("err") is None
        time.sleep(3)
    return False


def get_sol_price_usd() -> Optional[float]:
    try:
        r = requests.get(JUPITER_PRICE_URL, params={"ids": SOL_MINT}, timeout=15)
        r.raise_for_status()
        data = r.json().get("data", {})
        price = data.get(SOL_MINT, {}).get("price")
        return float(price) if price else None
    except Exception:
        pass
    # fallback: Dexscreener SOL/USDC pair
    try:
        r = requests.get(f"{DEXSCREENER_TOKEN_URL}/{SOL_MINT}", timeout=15)
        r.raise_for_status()
        pairs = r.json().get("pairs") or []
        usd_pairs = [p for p in pairs if p.get("quoteToken", {}).get("symbol") in ("USDC", "USDT")]
        if usd_pairs:
            return float(usd_pairs[0]["priceUsd"])
    except Exception:
        pass
    return None


# ----------------------------------------------------------------------------
# JUPITER SWAP
# ----------------------------------------------------------------------------

def get_quote(input_mint: str, output_mint: str, amount_raw: int, slippage_bps: Optional[int] = None) -> Optional[Dict[str, Any]]:
    slippage_bps = slippage_bps or CFG["trading"]["max_slippage_bps"]
    params = {"inputMint": input_mint, "outputMint": output_mint, "amount": amount_raw, "slippageBps": slippage_bps}
    try:
        r = requests.get(JUPITER_QUOTE_URL, params=params, timeout=15)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"[warn] quote failed ({input_mint[:6]}->{output_mint[:6]}): {e}", file=sys.stderr)
        return None


def sellability_check(token_mint: str, probe_lamports: int = 1_000_000) -> bool:
    """SOL->token->SOL round-trip quote. If it doesn't route back or loses
    more than ~60% just on quotes (before real fees/slippage), treat the
    pool as too thin / suspicious to touch. Not foolproof against contract-
    level sell-blocking honeypots."""
    fwd = get_quote(SOL_MINT, token_mint, probe_lamports)
    if not fwd or "outAmount" not in fwd:
        return False
    out_amount = int(fwd["outAmount"])
    if out_amount <= 0:
        return False
    rev = get_quote(token_mint, SOL_MINT, out_amount)
    if not rev or "outAmount" not in rev:
        return False
    sol_back = int(rev["outAmount"])
    return sol_back >= probe_lamports * 0.4


def execute_swap(wallet: Optional[Wallet], quote: Dict[str, Any]) -> Optional[str]:
    if DRY_RUN or not AUTO_TRADE_ENABLED:
        print(f"[DRY_RUN] would swap: inAmount={quote.get('inAmount')} outAmount={quote.get('outAmount')} "
              f"route={quote.get('inputMint','')[:6]}->{quote.get('outputMint','')[:6]}")
        return "DRY_RUN_NO_TX"

    # extra runtime guard: even if env was set, refuse without the arm token
    pubkey_str = str(wallet.pubkey) if wallet else ""
    if not safety.arm_ok_to_trade(pubkey_str):
        print("[safety] execute_swap blocked: live arm token missing or wallet on refuse-list. "
              "Set ARM_LIVE_TRADE=YES-I-WANT-LIVE-MONEY-AT-RISK-2026 in env to enable.",
              file=sys.stderr)
        return None

    swap_payload = {
        "quoteResponse": quote,
        "userPublicKey": str(wallet.pubkey),
        "wrapAndUnwrapSol": True,
        "dynamicComputeUnitLimit": True,
        "prioritizationFeeLamports": "auto",
    }
    try:
        r = requests.post(JUPITER_SWAP_URL, json=swap_payload, timeout=20)
        r.raise_for_status()
        swap_tx_b64 = r.json()["swapTransaction"]
    except Exception as e:
        print(f"[error] failed to build swap tx: {e}", file=sys.stderr)
        return None

    raw = base64.b64decode(swap_tx_b64)
    unsigned_tx = VersionedTransaction.from_bytes(raw)
    signed_tx = wallet.sign(unsigned_tx)

    try:
        sig = send_raw_transaction(signed_tx)
    except Exception as e:
        print(f"[error] failed to send tx: {e}", file=sys.stderr)
        return None

    print(f"[tx] sent {sig} — confirming...")
    if not confirm_signature(sig):
        print(f"[error] tx {sig} did not confirm cleanly — check explorer manually", file=sys.stderr)
        return None
    print(f"[tx] confirmed: https://solscan.io/tx/{sig}")
    return sig


# ----------------------------------------------------------------------------
# ENTRY SCORING — combines narrative strength + on-chain momentum
# ----------------------------------------------------------------------------

_POST_VOLUME_SCORE = {"hundreds": 3, "thousands": 6, "tens of thousands": 9}


def compute_entry_score(candidate: Dict[str, Any], dex_pair: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Returns {"score": float 0-10, "reasons": [...], "hard_fail": [...]}.
    hard_fail is non-empty if a configured filter is violated outright —
    those override the score and block entry regardless of how high it is."""
    filters = CFG["entry_filters"]
    reasons = []
    hard_fail = []
    score = 0.0

    # --- narrative component (up to 6 points) ---
    vol_score = _POST_VOLUME_SCORE.get(candidate.get("est_posts_1_24h", ""), 2)
    score += vol_score * 0.4  # up to 3.6
    if candidate.get("cross_community"):
        score += 1.2
        reasons.append("cross-community spread")
    if candidate.get("organic"):
        score += 1.2
        reasons.append("still organic")
    notice = candidate.get("crypto_notice_level")
    if notice == "none":
        score += 1.0
        reasons.append("CT hasn't noticed yet")
    elif notice == "early_whispers":
        score += 0.5
        reasons.append("only early whispers on CT")

    # --- on-chain component (up to 4 points) ---
    if dex_pair is None:
        hard_fail.append("no on-chain pair data available")
    else:
        liq = (dex_pair.get("liquidity") or {}).get("usd") or 0
        if liq < filters["min_liquidity_usd"]:
            hard_fail.append(f"liquidity ${liq:.0f} below min ${filters['min_liquidity_usd']}")
        elif liq > filters["max_liquidity_usd"]:
            hard_fail.append(f"liquidity ${liq:.0f} above max ${filters['max_liquidity_usd']} — gap likely already gone")
        else:
            score += 1.0
            reasons.append(f"liquidity ${liq:.0f} in target range")

        txns_h1 = (dex_pair.get("txns") or {}).get("h1") or {}
        buys, sells = txns_h1.get("buys", 0), txns_h1.get("sells", 0)
        if buys + sells >= 5:
            ratio = buys / max(sells, 1)
            if ratio < filters["min_buy_sell_ratio_h1"]:
                hard_fail.append(f"buy/sell ratio {ratio:.2f} below min {filters['min_buy_sell_ratio_h1']}")
            else:
                score += min(2.0, ratio)  # more buy-heavy = higher score, capped
                reasons.append(f"buy/sell ratio {ratio:.2f} (1h)")

        # rough price-impact estimate for our intended position size
        position_usd = CFG["trading"]["position_size_usd"]
        if liq > 0:
            impact_pct = (position_usd / liq) * 100
            if impact_pct > filters["max_price_impact_pct"]:
                hard_fail.append(f"est. price impact {impact_pct:.1f}% exceeds max {filters['max_price_impact_pct']}%")
            else:
                score += 1.0
                reasons.append(f"est. price impact {impact_pct:.1f}%")

    # --- smart-money convergence bonus ---
    # Reads from smart_money._recent_buys which is kept warm by
    # gap_finder_bot.run_once() polling the watchlist each cycle. Cheap.
    sm_cfg = CFG.get("smart_money", {})
    if sm_cfg.get("enabled", True) and "mint" in (candidate or {}):
        try:
            conv = smart_money.check_convergence(
                candidate["mint"],
                min_wallets=sm_cfg.get("min_wallets_for_convergence", 2),
                window_seconds=sm_cfg.get("convergence_window_seconds", 900),
            )
            if conv["converged"]:
                bonus = float(sm_cfg.get("convergence_score_bonus", 1.5))
                score += bonus
                labels = ", ".join(w["label"] or w["address"][:8] for w in conv["wallets"])
                reasons.append(f"smart-money convergence ({conv['wallet_count']} wallets: {labels})")
        except Exception as e:
            # never let smart-money lookup crash the entry pipeline
            print(f"[warn] smart_money.check_convergence failed: {e}", file=sys.stderr)

    return {"score": round(score, 2), "reasons": reasons, "hard_fail": hard_fail}


def passes_entry(candidate: Dict[str, Any], dex_pair: Optional[Dict[str, Any]]) -> bool:
    result = compute_entry_score(candidate, dex_pair)
    if result["hard_fail"]:
        print(f"[entry-reject] {candidate.get('term')}: {'; '.join(result['hard_fail'])}")
        return False
    # --- holder distribution / rug hard-filter (after narrative + on-chain pass) ---
    hf_cfg = CFG.get("holder_filters", {})
    if hf_cfg.get("enabled", True) and "mint" in (candidate or {}):
        try:
            holder_screen = holder_analyzer.screen_token(candidate["mint"], hf_cfg)
            if holder_screen["should_reject"]:
                print(f"[entry-reject] {candidate.get('term')} (holder): "
                      f"{'; '.join(holder_screen['reject_reasons'])}")
                return False
            # fold risk into the score: 0 risk = no penalty, 10 risk = -3
            result["score"] = round(result["score"] - holder_screen["risk_score"] * 0.3, 2)
        except Exception as e:
            # RPC failures on holder screen: warn but don't auto-block.
            # Better to occasionally enter on a "no-screen" cycle than to
            # never enter because the RPC is flaky.
            print(f"[warn] holder_analyzer.screen_token failed: {e}", file=sys.stderr)
    if result["score"] < CFG["entry_filters"]["min_entry_score"]:
        print(f"[entry-reject] {candidate.get('term')}: score {result['score']} below "
              f"min {CFG['entry_filters']['min_entry_score']}")
        return False
    print(f"[entry-score] {candidate.get('term')}: {result['score']}/10 — {', '.join(result['reasons'])}")
    return True


# ----------------------------------------------------------------------------
# POSITIONS
# ----------------------------------------------------------------------------

def _load_json(path: str, default):
    if os.path.exists(path):
        try:
            with open(path) as f:
                return json.load(f)
        except Exception:
            return default
    return default


def _save_json(path: str, data) -> None:
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def load_positions() -> List[Dict[str, Any]]:
    return _load_json(POSITIONS_FILE, [])


def save_positions(positions: List[Dict[str, Any]]) -> None:
    _save_json(POSITIONS_FILE, positions)


def load_ledger() -> List[Dict[str, Any]]:
    return _load_json(LEDGER_FILE, [])


def save_ledger(ledger: List[Dict[str, Any]]) -> None:
    _save_json(LEDGER_FILE, ledger)


def realized_loss_last_24h_usd() -> float:
    ledger = load_ledger()
    cutoff = dt.datetime.utcnow() - dt.timedelta(hours=24)
    total = 0.0
    for entry in ledger:
        ts = dt.datetime.fromisoformat(entry["closed_at"])
        if ts >= cutoff and entry.get("pnl_usd", 0) < 0:
            total += entry["pnl_usd"]
    return abs(total)


def get_token_price_usd(token_mint: str) -> Optional[float]:
    pair = onchain_analyzer.get_dex_pair_data(token_mint)
    if not pair:
        return None
    price = pair.get("priceUsd")
    return float(price) if price else None


# ----------------------------------------------------------------------------
# OPEN
# ----------------------------------------------------------------------------

def kill_switch_tripped() -> bool:
    loss = realized_loss_last_24h_usd()
    cap = CFG["trading"]["max_daily_loss_usd"]
    if loss >= cap:
        print(f"[kill-switch] realized 24h loss ${loss:.2f} >= limit ${cap}. "
              f"No new positions until this clears or you reset ledger.json.", file=sys.stderr)
        return True
    return False


def open_position(wallet: Optional[Wallet], term: str, token_mint: str, pair_url: str = "",
                   description: str = "", dex_pair: Optional[Dict[str, Any]] = None,
                   candidate: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    positions = load_positions()
    open_count = sum(1 for p in positions if p["status"] == "open")
    max_positions = CFG["trading"]["max_open_positions"]

    if open_count >= max_positions:
        print(f"[skip] max open positions ({max_positions}) reached, skipping {term}")
        return None
    if kill_switch_tripped():
        return None
    if candidate is not None and not passes_entry(candidate, dex_pair):
        return None
    if not sellability_check(token_mint):
        print(f"[skip] {term} ({token_mint}) failed sellability check — likely thin/honeypot pool")
        return None

    entry_price = get_token_price_usd(token_mint)
    if entry_price is None:
        print(f"[skip] could not get a price for {term}, skipping")
        return None

    sol_price = get_sol_price_usd()
    if not sol_price:
        print(f"[skip] could not get SOL/USD price, skipping {term}")
        return None

    position_usd = CFG["trading"]["position_size_usd"]
    position_sol = position_usd / sol_price
    lamports_in = int(position_sol * 1_000_000_000)

    quote = get_quote(SOL_MINT, token_mint, lamports_in)
    if not quote:
        print(f"[skip] no route for {term}")
        return None

    if wallet is None and AUTO_TRADE_ENABLED and not DRY_RUN:
        print(f"[error] AUTO_TRADE_ENABLED but no wallet available, cannot open {term}", file=sys.stderr)
        return None

    sig = execute_swap(wallet, quote) if wallet else "NO_WALLET_DRY_RUN"
    if sig is None:
        print(f"[error] buy failed for {term}")
        return None

    tp_pct = CFG["trading"]["take_profit_pct"]
    sl_pct = CFG["trading"]["stop_loss_pct"]
    tokens_bought = int(quote["outAmount"])

    position = {
        "id": str(uuid.uuid4()),
        "term": term,
        "description": description,
        "mint": token_mint,
        "pair_url": pair_url,
        "entry_price_usd": entry_price,
        "position_usd": position_usd,
        "sol_spent": position_sol,
        "tokens_bought_raw": tokens_bought,
        "tokens_remaining_raw": tokens_bought,   # shrinks as partial TP fires
        "realized_usd": 0.0,                      # accumulates across partial + final sells
        "tp_price_usd": entry_price * (1 + tp_pct / 100),
        "sl_price_usd": entry_price * (1 - sl_pct / 100),
        "trailing_stop_price_usd": None,          # set once trailing activates
        "peak_price_usd": entry_price,
        "partial_tp_hits": [],                    # list of at_pct levels already sold
        "opened_at": dt.datetime.utcnow().isoformat(),
        "narrative_status": "accelerating",
        "status": "open",
        "buy_tx": sig,
    }
    positions.append(position)
    save_positions(positions)
    print(f"[opened] {term} | ${position_usd} (~{position_sol:.4f} SOL) @ ${entry_price:.8f} | "
          f"TP ${position['tp_price_usd']:.8f} | SL ${position['sl_price_usd']:.8f} | tx={sig}")
    return position


# ----------------------------------------------------------------------------
# CLOSE (full or partial)
# ----------------------------------------------------------------------------

def _sell_raw_amount(wallet: Optional[Wallet], position: Dict[str, Any], amount_raw: int) -> Optional[int]:
    """Sells an explicit raw token amount (capped at what's actually left).
    Returns lamports of SOL received, or None on failure."""
    amount_raw = min(amount_raw, position["tokens_remaining_raw"])
    if amount_raw <= 0:
        return None
    quote = get_quote(position["mint"], SOL_MINT, amount_raw)
    if not quote:
        return None
    sig = execute_swap(wallet, quote) if wallet else "NO_WALLET_DRY_RUN"
    if sig is None:
        return None
    position["_last_sell_tx"] = sig
    position["tokens_remaining_raw"] -= amount_raw
    return int(quote["outAmount"])


def _record_ledger(position: Dict[str, Any], reason: str, pnl_usd: float, pnl_pct: float) -> None:
    ledger = load_ledger()
    ledger.append({
        "term": position["term"],
        "mint": position["mint"],
        "reason": reason,
        "pnl_usd": pnl_usd,
        "pnl_pct": pnl_pct,
        "closed_at": dt.datetime.utcnow().isoformat(),
    })
    save_ledger(ledger)


def close_position_full(wallet: Optional[Wallet], position: Dict[str, Any], reason: str, current_price: float,
                         sol_price: float) -> None:
    remaining = position["tokens_remaining_raw"]
    if remaining > 0:
        lamports_back = _sell_raw_amount(wallet, position, remaining)
        if lamports_back is None:
            print(f"[error] sell failed for {position['term']}, will retry next cycle", file=sys.stderr)
            return
        usd_received = (lamports_back / 1_000_000_000) * sol_price
        position["realized_usd"] = position.get("realized_usd", 0.0) + usd_received

    pnl_usd = position.get("realized_usd", 0.0) - position["position_usd"]
    pnl_pct = pnl_usd / position["position_usd"] * 100

    position["status"] = "closed"
    position["close_reason"] = reason
    position["closed_at"] = dt.datetime.utcnow().isoformat()
    position["exit_price_usd"] = current_price
    position["sell_tx"] = position.pop("_last_sell_tx", None)
    position["pnl_usd"] = pnl_usd
    position["pnl_pct"] = pnl_pct

    _persist_position(position)
    _record_ledger(position, reason, pnl_usd, pnl_pct)
    onchain_analyzer.reset_history(position["mint"])

    tag = "✅" if pnl_usd >= 0 else "🔴"
    print(f"{tag} [closed:{reason}] {position['term']} | total pnl=${pnl_usd:+.2f} ({pnl_pct:+.1f}%)")


def take_partial_profit(wallet: Optional[Wallet], position: Dict[str, Any], level: Dict[str, Any],
                         current_price: float, sol_price: float) -> None:
    amount_raw = int(position["tokens_bought_raw"] * (level["sell_pct"] / 100))
    lamports_back = _sell_raw_amount(wallet, position, amount_raw)
    if lamports_back is None:
        print(f"[error] partial TP sell failed for {position['term']}, will retry next cycle", file=sys.stderr)
        return

    usd_received = (lamports_back / 1_000_000_000) * sol_price
    position["realized_usd"] = position.get("realized_usd", 0.0) + usd_received
    position["partial_tp_hits"].append(level["at_pct"])
    _persist_position(position)

    tranche_cost_basis = position["position_usd"] * (level["sell_pct"] / 100)
    tranche_pnl = usd_received - tranche_cost_basis
    print(f"💰 [partial-tp +{level['at_pct']}%] {position['term']} sold {level['sell_pct']}% of original | "
          f"tranche pnl ${tranche_pnl:+.2f} | tokens remaining: {position['tokens_remaining_raw']}")

    if position["tokens_remaining_raw"] <= 0:
        close_position_full(wallet, position, "partial_tp_exhausted", current_price, sol_price)


def _persist_position(position: Dict[str, Any]) -> None:
    positions = load_positions()
    for i, p in enumerate(positions):
        if p["id"] == position["id"]:
            positions[i] = position
    save_positions(positions)


# ----------------------------------------------------------------------------
# MONITOR LOOP — price TP/SL, trailing stop, partial TP, on-chain signals,
# narrative decay
# ----------------------------------------------------------------------------

def _update_trailing_stop(position: Dict[str, Any], current_price: float) -> None:
    tcfg = CFG["trading"]
    if not tcfg.get("trailing_stop_enabled"):
        return

    if current_price > position["peak_price_usd"]:
        position["peak_price_usd"] = current_price

    profit_pct = (position["peak_price_usd"] - position["entry_price_usd"]) / position["entry_price_usd"] * 100
    if profit_pct >= tcfg["trailing_stop_activate_pct"]:
        new_trail = position["peak_price_usd"] * (1 - tcfg["trailing_stop_distance_pct"] / 100)
        if position["trailing_stop_price_usd"] is None or new_trail > position["trailing_stop_price_usd"]:
            position["trailing_stop_price_usd"] = new_trail


def _check_partial_tp(wallet: Optional[Wallet], position: Dict[str, Any], current_price: float, sol_price: float) -> bool:
    """Returns True if a partial (or full, if it emptied the position) sell happened."""
    profit_pct = (current_price - position["entry_price_usd"]) / position["entry_price_usd"] * 100
    for level in CFG["trading"]["partial_take_profit"]:
        if level["at_pct"] not in position["partial_tp_hits"] and profit_pct >= level["at_pct"]:
            take_partial_profit(wallet, position, level, current_price, sol_price)
            return True
    return False


def monitor_once(wallet: Optional[Wallet]) -> None:
    positions = load_positions()
    open_positions = [p for p in positions if p["status"] == "open"]
    if not open_positions:
        return

    sol_price = get_sol_price_usd()
    if not sol_price:
        print("[warn] could not fetch SOL/USD price this cycle, skipping monitor pass", file=sys.stderr)
        return

    for p in open_positions:
        price = get_token_price_usd(p["mint"])
        if price is None:
            print(f"[warn] no price for {p['term']}, skipping this cycle")
            continue

        # 1. On-chain dump signals — highest priority, can override TP/SL entirely
        onchain_reasons = onchain_analyzer.evaluate_exit_signals(p["mint"], CFG["onchain_exit_signals"])
        if onchain_reasons:
            print(f"🚨 [onchain-alert] {p['term']}: {'; '.join(onchain_reasons)}")
            close_position_full(wallet, p, f"onchain_signal: {onchain_reasons[0]}", price, sol_price)
            continue

        # 2. Narrative decay — if Grok says the meme is declining/dead while
        #    we're in profit, tighten up rather than wait for price to catch down
        if p.get("narrative_status") in ("declining", "dead"):
            profit_pct = (price - p["entry_price_usd"]) / p["entry_price_usd"] * 100
            if profit_pct > 0:
                print(f"📉 [narrative-decay] {p['term']} narrative is {p['narrative_status']} "
                      f"while up {profit_pct:.1f}% — closing to lock gains")
                close_position_full(wallet, p, "narrative_decay", price, sol_price)
                continue

        # 3. Trailing stop bookkeeping — persist immediately so peak/trail
        #    price survive across monitor cycles even when we don't close.
        _update_trailing_stop(p, price)
        _persist_position(p)

        # 4. Trailing stop hit? — trailing_stop is profit-preservation, NOT
        #    a downside protector. If price is below entry we should let the
        #    stop_loss below handle it instead, so the trailing only fires
        #    when we're still in profit territory (or at worst breakeven).
        if (p["trailing_stop_price_usd"] is not None
                and price >= p["entry_price_usd"]
                and price <= p["trailing_stop_price_usd"]):
            close_position_full(wallet, p, "trailing_stop", price, sol_price)
            continue

        # 5. Partial take-profit ladder
        if _check_partial_tp(wallet, p, price, sol_price):
            continue

        # 6. Stop-loss is always a hard floor, regardless of trailing config.
        if price <= p["sl_price_usd"]:
            close_position_full(wallet, p, "stop_loss", price, sol_price)
            continue

        # 7. Fixed take-profit only applies when trailing stop is disabled —
        #    otherwise trailing is the intended profit-taking mechanism and
        #    a fixed TP would cap upside exactly when we want to let it run.
        if not CFG["trading"].get("trailing_stop_enabled") and price >= p["tp_price_usd"]:
            close_position_full(wallet, p, "take_profit", price, sol_price)
            continue

        profit_pct = (price - p["entry_price_usd"]) / p["entry_price_usd"] * 100
        trail_info = f" trail=${p['trailing_stop_price_usd']:.8f}" if p["trailing_stop_price_usd"] else ""
        tp_info = "" if CFG["trading"].get("trailing_stop_enabled") else f" TP ${p['tp_price_usd']:.8f}"
        print(f"[hold] {p['term']} @ ${price:.8f} ({profit_pct:+.1f}%){tp_info} "
              f"SL ${p['sl_price_usd']:.8f}{trail_info}")


def recheck_open_positions_narrative() -> None:
    """Slow-cadence check: re-ask Grok about each open position's narrative
    health. Call this from the main scan loop (every N scans), not the fast
    price monitor loop — LLM calls are too slow/costly for a 20s cycle."""
    positions = load_positions()
    open_positions = [p for p in positions if p["status"] == "open"]
    if not open_positions:
        return

    for p in open_positions:
        result = narrative.recheck_narrative(p["term"], p.get("description", ""))
        if not result:
            continue
        p["narrative_status"] = result.get("status", p.get("narrative_status"))
        p["narrative_score"] = result.get("score")
        p["narrative_note"] = result.get("note")
        _persist_position(p)
        print(f"[narrative-recheck] {p['term']}: {p['narrative_status']} "
              f"(score {result.get('score')}) — {result.get('note','')}")


def run_monitor_loop() -> None:
    wallet = Wallet(SOLANA_PRIVATE_KEY) if SOLANA_PRIVATE_KEY else None
    if wallet is None:
        print("[monitor] no SOLANA_PRIVATE_KEY set — will log prices/signals only, no trades.")
    elif DRY_RUN or not AUTO_TRADE_ENABLED:
        print("[monitor] DRY_RUN or AUTO_TRADE_ENABLED=false — trades will be simulated only.")

    interval = CFG["system"]["monitor_interval_seconds"]
    print(f"[monitor] watching positions every {interval}s. Ctrl+C to stop.")
    while True:
        try:
            reload_config()
            monitor_once(wallet)
        except Exception as e:
            print(f"[error] monitor cycle failed: {e}", file=sys.stderr)
        time.sleep(CFG["system"]["monitor_interval_seconds"])


if __name__ == "__main__":
    print("Config:")
    print(f"  AUTO_TRADE_ENABLED = {AUTO_TRADE_ENABLED}")
    print(f"  DRY_RUN            = {DRY_RUN}")
    print(json.dumps(CFG, indent=2))
    run_monitor_loop()
