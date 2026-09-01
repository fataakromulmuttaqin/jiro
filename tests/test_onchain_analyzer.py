#!/usr/bin/env python3
"""
test_onchain_analyzer.py — unit tests for exit-signal detection.

Covers:
- check_liquidity_pull fires on sudden LP drop
- check_whale_dump fires on top-N holder drop
- check_sell_pressure fires when sells outnumber buys
- check_fast_dump fires on rapid price drop
- evaluate_exit_signals returns empty list when nothing fires
- reset_history wipes a mint's history (re-entry starts clean)
"""

import time
import pytest

import onchain_analyzer


CFG = {
    "enabled": True,
    "whale_dump_threshold_pct": 15,
    "whale_check_top_n_holders": 10,
    "whale_check_window_minutes": 10,
    "liquidity_pull_threshold_pct": 20,
    "liquidity_check_window_minutes": 5,
    "sell_pressure_ratio_trigger": 0.35,
    "fast_dump_price_drop_pct": 12,
    "fast_dump_window_seconds": 180,
}


def _seed(mint, snapshots):
    """Push a sequence of snapshots into the in-memory history. The LAST
    snapshot is treated as "current" (timestamped at `now`); earlier ones
    are placed so they comfortably sit outside the relevant comparison
    window. This mirrors the production behavior where the monitor polls
    every N seconds and the most recent entry is the just-taken snapshot."""
    onchain_analyzer._history[mint] = []
    now = time.time()
    # Place snapshots[i] for i < N-1 at now - (N - i) * 1200 so each is
    # spaced 20 minutes back. The final snapshot lands at `now` (current).
    n = len(snapshots)
    for i, snap in enumerate(snapshots):
        if i == n - 1:
            ts = now
        else:
            ts = now - (n - 1 - i) * 1200
        onchain_analyzer._history[mint].append((ts, snap))


def test_liquidity_pull_fires():
    mint = "LiqMint111"
    _seed(mint, [
        {"liquidity_usd": 10000},     # t=0
        {"liquidity_usd": 7500},      # t=60s, drop of 25% -> fires
    ])
    msg = onchain_analyzer.check_liquidity_pull(mint, CFG)
    assert msg is not None
    assert "liquidity dropped" in msg


def test_liquidity_pull_does_not_fire_within_threshold():
    mint = "LiqMint222"
    _seed(mint, [
        {"liquidity_usd": 10000},
        {"liquidity_usd": 9000},   # 10% drop, threshold 20%
    ])
    msg = onchain_analyzer.check_liquidity_pull(mint, CFG)
    assert msg is None


def test_whale_dump_fires():
    mint = "WhaleMint111"
    _seed(mint, [
        {"whale_top_n_total": 1_000_000},
        {"whale_top_n_total": 800_000},   # 20% drop -> fires
    ])
    msg = onchain_analyzer.check_whale_dump(mint, CFG)
    assert msg is not None
    assert "holders" in msg


def test_sell_pressure_fires_when_sells_dominate():
    mint = "PressureMint111"
    _seed(mint, [
        # last snapshot only is what matters for this check
        {"buys_m5": 5, "sells_m5": 25},
    ])
    msg = onchain_analyzer.check_sell_pressure(mint, CFG)
    assert msg is not None
    assert "sell pressure" in msg


def test_sell_pressure_ignored_when_activity_too_low():
    mint = "QuietMint111"
    _seed(mint, [{"buys_m5": 1, "sells_m5": 1}])  # total < 5
    msg = onchain_analyzer.check_sell_pressure(mint, CFG)
    assert msg is None


def test_fast_dump_fires():
    mint = "DumpMint111"
    _seed(mint, [
        {"price_usd": 1.0},
        {"price_usd": 0.85},   # 15% drop -> fires
    ])
    msg = onchain_analyzer.check_fast_dump(mint, CFG)
    assert msg is not None
    assert "fast dump" in msg


def test_fast_dump_does_not_fire_for_slow_decline():
    mint = "SlowMint111"
    _seed(mint, [
        {"price_usd": 1.0},
        {"price_usd": 0.95},  # 5% drop, well below 12%
    ])
    msg = onchain_analyzer.check_fast_dump(mint, CFG)
    assert msg is None


def test_evaluate_returns_empty_when_disabled():
    mint = "DisabledMint111"
    _seed(mint, [{"price_usd": 1.0}, {"price_usd": 0.5}])
    cfg = dict(CFG)
    cfg["enabled"] = False
    reasons = onchain_analyzer.evaluate_exit_signals(mint, cfg)
    assert reasons == []


def test_reset_history_wipes_mint():
    mint = "ResetMint111"
    _seed(mint, [{"price_usd": 1.0}, {"price_usd": 0.5}])
    assert mint in onchain_analyzer._history
    onchain_analyzer.reset_history(mint)
    assert mint not in onchain_analyzer._history