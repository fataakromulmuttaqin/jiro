#!/usr/bin/env python3
"""
test_trading_logic.py — unit tests for entry scoring + TP/SL/trailing/partial
behavior using a mocked price/quote feed. Covers the bugs the README bragged
about fixing (partial TP against original size; trailing peak persistence)
plus a few new edge cases.
"""

import os
import json
import pytest

# IMPORTANT: import order matters. safety is reloaded by tests/safety tests
# and can clobber env. We set our own right before importing trading.
os.environ["DRY_RUN"] = "true"
os.environ["AUTO_TRADE_ENABLED"] = "false"

import trading


MINT = "TestMint1111111111111111111111111111111111"


def _flat_pair(price=1.0, liq=10_000, buys=10, sells=5):
    return {
        "priceUsd": str(price),
        "liquidity": {"usd": liq},
        "txns": {"h1": {"buys": buys, "sells": sells}, "m5": {"buys": buys, "sells": sells}},
    }


def _candidate(term="testmeme", notice="none", cross=True, organic=True, vol="thousands"):
    return {
        "term": term,
        "description": "test",
        "category": "meme",
        "est_posts_1_24h": vol,
        "cross_community": cross,
        "organic": organic,
        "crypto_notice_level": notice,
    }


# ---------------------------- ENTRY SCORING ----------------------------

def test_entry_score_rejects_low_liquidity():
    pair = _flat_pair(price=1.0, liq=500)  # below min 2000
    res = trading.compute_entry_score(_candidate(), pair)
    assert any("below min" in r for r in res["hard_fail"])


def test_entry_score_rejects_too_much_liquidity():
    pair = _flat_pair(price=1.0, liq=200_000)  # above max 80k
    res = trading.compute_entry_score(_candidate(), pair)
    assert any("above max" in r for r in res["hard_fail"])


def test_entry_score_rejects_low_buy_sell_ratio():
    pair = _flat_pair(price=1.0, liq=10_000, buys=4, sells=10)  # ratio < 1.1
    res = trading.compute_entry_score(_candidate(), pair)
    assert any("buy/sell ratio" in r for r in res["hard_fail"])


def test_entry_score_rejects_no_pair_data():
    res = trading.compute_entry_score(_candidate(), None)
    assert any("no on-chain" in r for r in res["hard_fail"])


def test_entry_score_accepts_strong_candidate():
    pair = _flat_pair(price=1.0, liq=10_000, buys=50, sells=10)
    res = trading.compute_entry_score(_candidate(), pair)
    assert res["hard_fail"] == []
    assert res["score"] >= trading.CFG["entry_filters"]["min_entry_score"]


def test_entry_score_punishes_saturated_crypto_notice():
    pair = _flat_pair(price=1.0, liq=10_000)
    fresh = trading.compute_entry_score(_candidate(notice="none"), pair)["score"]
    saturated = trading.compute_entry_score(_candidate(notice="saturated"), pair)["score"]
    assert fresh > saturated


# ---------------------- POSITION LIFECYCLE / EXITS ----------------------

@pytest.fixture
def position(tmp_path, monkeypatch, request):
    """A fresh open position with known entry data, plus all file I/O
    redirected to a shared temp directory so tests don't touch real state.

    We use tmp_path_factory so every test that depends on this fixture
    sees the SAME shared dir, instead of getting a fresh one each time."""
    if not hasattr(request.config, "_jiro_shared_dir"):
        request.config._jiro_shared_dir = tmp_path
    shared = request.config._jiro_shared_dir

    monkeypatch.setattr(trading, "POSITIONS_FILE", str(shared / "positions.json"))
    monkeypatch.setattr(trading, "LEDGER_FILE", str(shared / "ledger.json"))
    trading.save_positions([])

    pos = {
        "id": "test-pos-1",
        "term": "testmeme",
        "description": "",
        "mint": MINT,
        "pair_url": "",
        "entry_price_usd": 1.0,
        "position_usd": 50.0,
        "sol_spent": 0.5,
        "tokens_bought_raw": 1_000_000,
        "tokens_remaining_raw": 1_000_000,
        "realized_usd": 0.0,
        "tp_price_usd": 1.60,
        "sl_price_usd": 0.75,
        "trailing_stop_price_usd": None,
        "peak_price_usd": 1.0,
        "partial_tp_hits": [],
        "opened_at": "2026-09-01T00:00:00",
        "narrative_status": "accelerating",
        "status": "open",
        "buy_tx": "DRY_RUN",
    }
    trading.save_positions([pos])
    return pos


def test_trailing_stop_activates_and_ratches(monkeypatch, position):
    """trailing activates at +30%, ratchets up as price climbs, never drops."""
    # price hits +50% — should activate trailing 15% behind peak
    trading._update_trailing_stop(position, current_price=1.50)
    assert position["peak_price_usd"] == 1.50
    assert position["trailing_stop_price_usd"] == pytest.approx(1.50 * 0.85)

    # price climbs higher — trail must ratchet up
    trading._update_trailing_stop(position, current_price=2.00)
    assert position["trailing_stop_price_usd"] == pytest.approx(2.00 * 0.85)

    # price falls back — trail must NOT move down
    prior_trail = position["trailing_stop_price_usd"]
    trading._update_trailing_stop(position, current_price=1.60)
    assert position["trailing_stop_price_usd"] == prior_trail


def test_trailing_does_not_activate_below_threshold(position):
    """At +10%, trailing must stay inactive."""
    trading._update_trailing_stop(position, current_price=1.10)
    assert position["trailing_stop_price_usd"] is None
    assert position["peak_price_usd"] == 1.10


def test_trailing_disabled_via_config(monkeypatch, position):
    """If trailing_stop_enabled=false, _update_trailing_stop is a no-op."""
    cfg = trading.CFG
    cfg["trading"]["trailing_stop_enabled"] = False
    try:
        trading._update_trailing_stop(position, current_price=10.0)
        assert position["trailing_stop_price_usd"] is None
    finally:
        cfg["trading"]["trailing_stop_enabled"] = True


def test_partial_tp_against_original_size(monkeypatch, position):
    """50% at +40% sells exactly 50% of ORIGINAL position size.
    Remaining = 50% (not 25%). This is the bug the README bragged about
    fixing."""
    level = {"at_pct": 40, "sell_pct": 50}
    # mock _sell_raw_amount to just decrement the counter without network
    captured = {}

    def fake_sell(wallet, pos, amount_raw):
        captured["amount"] = amount_raw
        pos["tokens_remaining_raw"] -= amount_raw
        pos["_last_sell_tx"] = "DRY"
        return int(amount_raw * 1.4)  # pretend 1.4 SOL came back

    monkeypatch.setattr(trading, "_sell_raw_amount", fake_sell)
    trading.take_partial_profit(None, position, level, current_price=1.40, sol_price=100.0)

    assert captured["amount"] == 500_000  # 50% of 1,000,000
    assert position["tokens_remaining_raw"] == 500_000  # NOT 250_000
    assert 40 in position["partial_tp_hits"]


def test_partial_tp_full_exhaustion_closes(monkeypatch, position):
    """If a partial TP empties the position, it should auto-close."""
    # simulate: position only has 500_000 tokens (the "second tranche" scenario)
    position["tokens_bought_raw"] = 1_000_000
    position["tokens_remaining_raw"] = 500_000  # already half-sold

    captured = {}

    def fake_sell(wallet, pos, amount_raw):
        pos["tokens_remaining_raw"] -= amount_raw
        pos["_last_sell_tx"] = "DRY"
        return int(amount_raw * 1.4)

    def fake_close(wallet, pos, reason, current_price, sol_price):
        captured["closed"] = True
        captured["reason"] = reason
        pos["status"] = "closed"

    monkeypatch.setattr(trading, "_sell_raw_amount", fake_sell)
    monkeypatch.setattr(trading, "close_position_full", fake_close)

    level = {"at_pct": 100, "sell_pct": 50}  # would try to sell 50% of 1M = 500k
    trading.take_partial_profit(None, position, level, current_price=2.0, sol_price=100.0)

    assert position["tokens_remaining_raw"] == 0
    assert captured.get("closed") is True
    assert captured["reason"] == "partial_tp_exhausted"


def test_stop_loss_is_hard_floor(monkeypatch, position):
    """Even if trailing stop is way above, the SL must trigger when price
    breaches it."""
    position["trailing_stop_price_usd"] = 5.0  # very generous — shouldn't fire
    trading._persist_position(position)
    closed = {}

    monkeypatch.setattr(trading, "get_sol_price_usd", lambda: 100.0)
    monkeypatch.setattr(trading, "get_token_price_usd", lambda mint: 0.5)  # below SL
    monkeypatch.setattr(trading, "close_position_full",
                        lambda w, p, reason, cp, sp: closed.update(reason=reason))
    monkeypatch.setattr(trading.onchain_analyzer, "evaluate_exit_signals", lambda *a, **kw: [])

    trading.monitor_once(None)
    assert closed.get("reason") == "stop_loss"


def test_onchain_dump_overrides_trailing(monkeypatch, position):
    """If on-chain analyzer says whale_dump, that reason wins over trailing/SL."""
    closed = {}

    monkeypatch.setattr(trading, "get_sol_price_usd", lambda: 100.0)
    monkeypatch.setattr(trading, "get_token_price_usd", lambda mint: 1.5)
    monkeypatch.setattr(
        trading.onchain_analyzer, "evaluate_exit_signals",
        lambda *a, **kw: ["top holders' combined balance dropped 25%"],
    )

    def fake_close(wallet, pos, reason, current_price, sol_price):
        closed["reason"] = reason

    monkeypatch.setattr(trading, "close_position_full", fake_close)
    trading.monitor_once(None)
    assert "onchain_signal" in closed["reason"]


def test_narrative_decay_closes_when_in_profit(monkeypatch, position):
    """If Grok marked the narrative declining AND we're in profit, lock gains."""
    position["narrative_status"] = "declining"
    trading._persist_position(position)  # sync the file so monitor_once sees it
    closed = {}

    monkeypatch.setattr(trading, "get_sol_price_usd", lambda: 100.0)
    monkeypatch.setattr(trading, "get_token_price_usd", lambda mint: 1.2)  # +20%
    monkeypatch.setattr(trading.onchain_analyzer, "evaluate_exit_signals", lambda *a, **kw: [])

    def fake_close(wallet, pos, reason, current_price, sol_price):
        closed["reason"] = reason

    monkeypatch.setattr(trading, "close_position_full", fake_close)
    trading.monitor_once(None)
    assert closed["reason"] == "narrative_decay"


def test_narrative_decay_ignored_when_in_loss(monkeypatch, position):
    """Decay alone should NOT exit if we're in loss — let SL handle it."""
    position["narrative_status"] = "declining"
    trading._persist_position(position)
    closed = {}

    monkeypatch.setattr(trading, "get_sol_price_usd", lambda: 100.0)
    monkeypatch.setattr(trading, "get_token_price_usd", lambda mint: 0.70)  # -30%
    monkeypatch.setattr(trading.onchain_analyzer, "evaluate_exit_signals", lambda *a, **kw: [])

    def fake_close(wallet, pos, reason, current_price, sol_price):
        closed["reason"] = reason

    monkeypatch.setattr(trading, "close_position_full", fake_close)
    trading.monitor_once(None)
    assert closed["reason"] == "stop_loss"


def test_kill_switch_trips_on_daily_loss(monkeypatch, tmp_path):
    """If realized 24h loss >= cap, kill_switch_tripped must return True
    and open_position must refuse."""
    monkeypatch.setattr(trading, "POSITIONS_FILE", str(tmp_path / "positions.json"))
    monkeypatch.setattr(trading, "LEDGER_FILE", str(tmp_path / "ledger.json"))
    # seed ledger with $60 loss in the last 24h (cap is $50)
    trading.save_ledger([{
        "term": "x", "mint": "y", "reason": "stop_loss",
        "pnl_usd": -60, "pnl_pct": -25,
        "closed_at": "2026-09-01T00:00:00",  # recent
    }])

    assert trading.kill_switch_tripped() is True

    # open_position should refuse and return None
    pos = trading.open_position(
        wallet=None, term="foo", token_mint="bar",
        candidate=_candidate(), dex_pair=_flat_pair(),
    )
    assert pos is None


def test_max_open_positions_cap(monkeypatch, tmp_path):
    """Don't open a 4th position when cap is 3."""
    monkeypatch.setattr(trading, "POSITIONS_FILE", str(tmp_path / "positions.json"))
    monkeypatch.setattr(trading, "LEDGER_FILE", str(tmp_path / "ledger.json"))
    # pre-fill 3 open positions
    existing = [{
        "id": f"p-{i}", "term": f"old-{i}", "description": "", "mint": f"mint{i}",
        "entry_price_usd": 1.0, "position_usd": 50.0, "sol_spent": 0.5,
        "tokens_bought_raw": 1_000_000, "tokens_remaining_raw": 1_000_000,
        "realized_usd": 0.0, "tp_price_usd": 1.6, "sl_price_usd": 0.75,
        "trailing_stop_price_usd": None, "peak_price_usd": 1.0,
        "partial_tp_hits": [], "opened_at": "2026-08-01T00:00:00",
        "narrative_status": "accelerating", "status": "open", "buy_tx": "X",
    } for i in range(3)]
    trading.save_positions(existing)

    pos = trading.open_position(
        wallet=None, term="newterm",
        token_mint="newmint",
        candidate=_candidate(),
        dex_pair=_flat_pair(),
    )
    assert pos is None


def test_open_position_injects_mint_into_candidate(monkeypatch, tmp_path):
    """Regression fix: gap_finder_bot passes the candidate dict WITHOUT a
    `mint` key and the mint separately. compute_entry_score() and
    passes_entry() gate the smart-money convergence bonus and the holder/rug
    screen on `mint in candidate`, so before this fix those two signals never
    fired on the real entry path. open_position must inject the resolved mint
    into the candidate so the hardening signals actually run."""
    monkeypatch.setattr(trading, "POSITIONS_FILE", str(tmp_path / "positions.json"))
    monkeypatch.setattr(trading, "LEDGER_FILE", str(tmp_path / "ledger.json"))
    cand = _candidate()          # no "mint" key, like gap_finder_bot's `g`
    assert "mint" not in cand

    # Force sellability + price resolution to succeed so the flow gets past the
    # early gates and hits the mint injection + scoring.
    monkeypatch.setattr(trading, "sellability_check", lambda *a, **k: True)
    monkeypatch.setattr(trading, "get_token_price_usd", lambda *a, **k: 1.0)
    monkeypatch.setattr(trading, "get_sol_price_usd", lambda *a, **k: 150.0)
    monkeypatch.setattr(trading, "get_quote",
                        lambda *a, **k: {"outAmount": "1000000", "inAmount": "1000"})
    monkeypatch.setattr(trading, "execute_swap", lambda *a, **k: "fake_sig")
    # scoring gated on mint: turn off expensive per-mint screen so it passes
    trading.CFG["holder_filters"]["enabled"] = False
    trading.CFG["smart_money"]["enabled"] = False
    monkeypatch.setattr(trading, "passes_entry", lambda c, d: (c.get("mint") is not None) or c is None)

    pos = trading.open_position(
        wallet=None, term="newterm", token_mint="newmint",
        candidate=cand, dex_pair=_flat_pair(),
    )
    assert cand.get("mint") == "newmint", \
        "open_position must inject the resolved mint into the candidate"