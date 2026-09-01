#!/usr/bin/env python3
"""
test_safety.py — unit tests for safety arm/lock logic.
"""

import os
import importlib

import pytest


def _reload_safety(monkeypatch, env: dict):
    """Reload the safety module with a fresh env so each test is isolated."""
    for k in list(os.environ.keys()):
        if k.startswith(("AUTO_TRADE_ENABLED", "DRY_RUN", "ARM_LIVE_TRADE",
                          "SOLANA_PRIVATE_KEY", "RPC_URL", "REFUSE_PUBKEYS")):
            monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    import safety
    importlib.reload(safety)
    return safety


def test_default_state_is_alert_only(monkeypatch):
    safety = _reload_safety(monkeypatch, {})
    s = safety.arm_status()
    assert s["AUTO_TRADE_ENABLED"] is False
    assert s["DRY_RUN"] is True
    assert s["ARM_LIVE_TRADE_set"] is False
    assert s["will_trade_live"] is False
    assert s["mode"] == "ALERT-ONLY"


def test_paper_mode_when_auto_on_but_dry_run(monkeypatch):
    safety = _reload_safety(monkeypatch, {
        "AUTO_TRADE_ENABLED": "true",
        "DRY_RUN": "true",
        "SOLANA_PRIVATE_KEY": "fake",
    })
    s = safety.arm_status()
    assert s["will_trade_live"] is False
    assert s["mode"] == "PAPER"


def test_live_mode_requires_arm_token(monkeypatch):
    safety = _reload_safety(monkeypatch, {
        "AUTO_TRADE_ENABLED": "true",
        "DRY_RUN": "false",
        "SOLANA_PRIVATE_KEY": "fake",
    })
    s = safety.arm_status()
    assert s["will_trade_live"] is False  # missing arm token

    # arm token wrong
    monkeypatch.setenv("ARM_LIVE_TRADE", "yes")
    importlib.reload(safety)
    assert safety.arm_status()["will_trade_live"] is False

    # arm token correct
    monkeypatch.setenv("ARM_LIVE_TRADE", "YES-I-WANT-LIVE-MONEY-AT-RISK-2026")
    importlib.reload(safety)
    assert safety.arm_status()["will_trade_live"] is True


def test_assert_safe_for_live_raises_without_arm(monkeypatch):
    safety = _reload_safety(monkeypatch, {
        "AUTO_TRADE_ENABLED": "true",
        "DRY_RUN": "false",
        "SOLANA_PRIVATE_KEY": "fake",
    })
    with pytest.raises(RuntimeError, match="ARM_LIVE_TRADE"):
        safety.assert_safe_for_live()


def test_assert_safe_for_live_passes_silently_in_paper(monkeypatch):
    safety = _reload_safety(monkeypatch, {})
    safety.assert_safe_for_live()  # must not raise


def test_refuse_pubkeys_blocks_listed_wallet(monkeypatch):
    safety = _reload_safety(monkeypatch, {
        "AUTO_TRADE_ENABLED": "true",
        "DRY_RUN": "false",
        "SOLANA_PRIVATE_KEY": "fake",
        "ARM_LIVE_TRADE": "YES-I-WANT-LIVE-MONEY-AT-RISK-2026",
        "REFUSE_PUBKEYS": "MyMainWallet111,MyOtherWallet222",
    })
    assert safety.arm_ok_to_trade("RandomWallet333") is True
    assert safety.arm_ok_to_trade("MyMainWallet111") is False
    assert safety.arm_ok_to_trade("MyOtherWallet222") is False