#!/usr/bin/env python3
"""Unit tests for holder_analyzer with mocked RPC + pump.fun API."""

import sys
import os
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import holder_analyzer


def _stub_authorities(mint_renounced=True, freeze_renounced=True):
    return {
        "value": {
            "data": {
                "parsed": {
                    "info": {
                        "mintAuthority": None if mint_renounced else "SomeAuthority11111111111111111111111111",
                        "freezeAuthority": None if freeze_renounced else "SomeAuthority11111111111111111111111111",
                    }
                }
            }
        }
    }


def _stub_total_supply(amount=1_000_000_000):
    return {"value": {"uiAmount": amount}}


def _stub_top_holders(amounts_pcts):
    # amounts_pcts: list of (token_account, owner, amount, pct)
    return {"value": [
        {"address": acc, "uiAmount": amt}
        for acc, _own, amt, _pct in amounts_pcts
    ]}


class FakeTopHoldersLookup:
    """Returns owner info for each token account by index."""
    def __init__(self, owners_by_account):
        self.owners_by_account = owners_by_account

    def __call_response(self, method, params):
        return {"value": {"data": {"parsed": {"info": {"owner": self.owners_by_account.get(params[0])}}}}}


def _make_holders(amounts_pcts, owners_by_account):
    th = FakeTopHoldersLookup(owners_by_account)
    return th, _stub_top_holders(amounts_pcts)


class TestAuthorityReject(unittest.TestCase):
    def test_mint_not_renounced_hard_reject(self):
        cfg = {"top_n_holders": 5, "require_mint_authority_renounced": True,
               "require_freeze_authority_renounced": True}
        with patch.object(holder_analyzer, "_rpc") as rpc:
            rpc.side_effect = lambda m, p: (
                _stub_total_supply() if m == "getTokenSupply"
                else _stub_authorities(mint_renounced=False, freeze_renounced=True)
                if m == "getAccountInfo" and p[0] == "MINT"
                else {"value": []}
            )
            res = holder_analyzer.screen_token("MINT", cfg, use_cache=False)
            self.assertTrue(res["should_reject"])
            self.assertIn("mint authority not renounced", res["reject_reasons"])

    def test_freeze_not_renounced_hard_reject(self):
        cfg = {"top_n_holders": 5, "require_mint_authority_renounced": True,
               "require_freeze_authority_renounced": True}
        with patch.object(holder_analyzer, "_rpc") as rpc:
            rpc.side_effect = lambda m, p: (
                _stub_total_supply() if m == "getTokenSupply"
                else _stub_authorities(mint_renounced=True, freeze_renounced=False)
                if m == "getAccountInfo" and p[0] == "MINT"
                else {"value": []}
            )
            res = holder_analyzer.screen_token("MINT", cfg, use_cache=False)
            self.assertTrue(res["should_reject"])
            self.assertIn("freeze authority not renounced", res["reject_reasons"])

    def test_both_renounced_passes_authority(self):
        cfg = {"top_n_holders": 5, "require_mint_authority_renounced": True,
               "require_freeze_authority_renounced": True}
        # With no holders we get an empty top10, which won't trip top10 > 40.
        # Authorities are renounced → no hard reject.
        with patch.object(holder_analyzer, "_rpc") as rpc:
            rpc.side_effect = lambda m, p: (
                _stub_total_supply() if m == "getTokenSupply"
                else _stub_authorities(mint_renounced=True, freeze_renounced=True)
                if m == "getAccountInfo" and p[0] == "MINT"
                else {"value": []}
            )
            res = holder_analyzer.screen_token("MINT", cfg, use_cache=False)
            self.assertFalse(res["should_reject"])
            self.assertTrue(res["mint_authority_renounced"])
            self.assertTrue(res["freeze_authority_renounced"])


class TestTop10Concentration(unittest.TestCase):
    def _run_with_holders(self, amounts_pcts, owners_by_account):
        cfg = {"top_n_holders": len(amounts_pcts),
               "require_mint_authority_renounced": True,
               "require_freeze_authority_renounced": True,
               "top10_holder_max_pct": 40}
        with patch.object(holder_analyzer, "_rpc") as rpc:
            owners_lookup = {acc: own for acc, own, _a, _p in amounts_pcts}
            def side_effect(method, params):
                if method == "getTokenSupply":
                    return _stub_total_supply()
                if method == "getAccountInfo":
                    if params[0] == "MINT":
                        return _stub_authorities(True, True)
                    return {"value": {"data": {"parsed": {"info": {"owner": owners_lookup.get(params[0])}}}}}
                if method == "getTokenLargestAccounts":
                    return {"value": [{"address": acc, "uiAmount": amt} for acc, _o, amt, _p in amounts_pcts]}
                return None
            rpc.side_effect = side_effect
            return holder_analyzer.screen_token("MINT", cfg, use_cache=False)

    def test_low_concentration_passes(self):
        # 5 holders, each ~10%, top10 = 50%
        amounts = [("A1", "W1", 100_000_000, 10),
                   ("A2", "W2", 100_000_000, 10),
                   ("A3", "W3", 100_000_000, 10),
                   ("A4", "W4", 100_000_000, 10),
                   ("A5", "W5", 100_000_000, 10)]
        # Need more than 10 holders to test top10 specifically; with 5 the sum
        # is 50% which is >40 → should_reject on top10
        res = self._run_with_holders(amounts, {a: o for a, o, _x, _y in amounts})
        self.assertTrue(res["should_reject"])
        self.assertTrue(any("top10 holders" in r for r in res["reject_reasons"]))

    def test_healthy_distribution_passes(self):
        # 12 holders, each ~8% → top10 = 80% → still over 40... use lower pct
        # Use raw amounts summing to less. Let's craft 10 holders at 3% each.
        amounts = []
        for i in range(10):
            amounts.append((f"A{i}", f"W{i}", 30_000_000, 3.0))  # 3% each → top10 = 30%
        res = self._run_with_holders(amounts, {a: o for a, o, _x, _y in amounts})
        self.assertFalse(res["should_reject"])
        self.assertLessEqual(res["top10_pct"], 40)


class TestRiskScore(unittest.TestCase):
    def test_risk_score_clamped_to_10(self):
        # All unknowns → all buckets add 0.6 weight = 4*2.0 + 0.6 + 0.6 = ~9.1
        cfg = {"top_n_holders": 5,
               "require_mint_authority_renounced": True,
               "require_freeze_authority_renounced": True}
        with patch.object(holder_analyzer, "_rpc") as rpc:
            rpc.side_effect = lambda m, p: (
                _stub_total_supply() if m == "getTokenSupply"
                else None  # forces unknowns for everything else
            )
            res = holder_analyzer.screen_token("MINT", cfg, use_cache=False)
            self.assertLessEqual(res["risk_score"], 10.0)
            self.assertGreater(res["risk_score"], 0)


class TestCaching(unittest.TestCase):
    def test_cache_hit_avoids_rpc(self):
        holder_analyzer.reset_cache()
        cfg = {"top_n_holders": 5, "require_mint_authority_renounced": True,
               "require_freeze_authority_renounced": True}
        call_count = {"n": 0}
        def counting(m, p):
            call_count["n"] += 1
            if m == "getTokenSupply":
                return _stub_total_supply()
            if m == "getAccountInfo" and p[0] == "MINT":
                return _stub_authorities(True, True)
            return {"value": []}
        with patch.object(holder_analyzer, "_rpc", side_effect=counting):
            holder_analyzer.screen_token("MINT", cfg, use_cache=True)
            n1 = call_count["n"]
            holder_analyzer.screen_token("MINT", cfg, use_cache=True)
            n2 = call_count["n"]
            self.assertEqual(n1, n2, "second call should hit cache, not RPC")
        holder_analyzer.reset_cache()

    def test_cache_evicts_old_entries_when_over_cap(self):
        """Regression: the screen cache must not grow unbounded over a 24/7
        run. Once past _SCREEN_CACHE_MAX_SIZE the oldest entries are evicted."""
        holder_analyzer.reset_cache()
        cfg = {"top_n_holders": 1, "require_mint_authority_renounced": True,
               "require_freeze_authority_renounced": True}
        def stub(m, p):
            if m == "getTokenSupply":
                return _stub_total_supply()
            if m == "getAccountInfo" and p[0] == "MINT":
                return _stub_authorities(True, True)
            return {"value": []}
        orig_max = holder_analyzer._SCREEN_CACHE_MAX_SIZE
        holder_analyzer._SCREEN_CACHE_MAX_SIZE = 3
        try:
            with patch.object(holder_analyzer, "_rpc", side_effect=stub):
                for i in range(6):
                    holder_analyzer.screen_token(f"MINT_{i}", cfg, use_cache=True)
            self.assertLessEqual(
                len(holder_analyzer._screen_cache),
                holder_analyzer._SCREEN_CACHE_MAX_SIZE,
                "cache should be bounded by _SCREEN_CACHE_MAX_SIZE")
        finally:
            holder_analyzer._SCREEN_CACHE_MAX_SIZE = orig_max
            holder_analyzer.reset_cache()


if __name__ == "__main__":
    unittest.main()