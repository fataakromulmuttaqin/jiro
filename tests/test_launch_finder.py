#!/usr/bin/env python3
"""Unit tests for launch_finder — token->narrative matching and dex_pair proxy.
No network; pure logic tests."""

import sys
import os
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import launch_finder as lf

# A fake fresh coin matching "pilly". created_timestamp is relative to now so
# the age-window tests don't drift out of range as the clock advances.
PILLY_COIN = {
    "mint": "PillyPumpMint12345678901234567890123456789012",
    "symbol": "PILLY",
    "name": "Pilly",
    "description": "the alon dev deployed a dog",
    "usd_market_cap": 2797,
    "market_cap_usd": 2797,
    "created_timestamp": int(time.time() * 1000) - 3600_000,  # ~1h ago
    "complete": False,
    "real_sol_reserves": 4000000000,
    "total_supply": 1000000000000000,
    "is_banned": False,
    "creator": "CkqW",
}

# old coin = outside max_age_hours
OLD_COIN = dict(PILLY_COIN, symbol="OLDPILLY", mint="OldPillyMint0000",
                created_timestamp=1)  # 1970 = very old

# migrated/complete coin = already on a DEX (should be skipped)
MIGRATED_COIN = dict(PILLY_COIN, symbol="DEXPILLY", mint="MigratedMint0000000",
                     complete=True)


class TestMatching(unittest.TestCase):
    def setUp(self):
        self.cfg = {"max_age_hours": 6, "max_market_cap_usd": 100000,
                    "min_name_similarity": 0.72}

    def test_exact_symbol_match_found(self):
        coin = lf.match_term_to_coin("pilly", [PILLY_COIN], self.cfg)
        self.assertIsNotNone(coin)
        self.assertEqual(coin["mint"], PILLY_COIN["mint"])
        self.assertGreaterEqual(coin["_match_score"], 0.8)

    def test_old_coin_skipped(self):
        coin = lf.match_term_to_coin("pilly", [OLD_COIN], self.cfg)
        self.assertIsNone(coin, "older than max_age_hours should be skipped")

    def test_migrated_coin_skipped(self):
        coin = lf.match_term_to_coin("pilly", [MIGRATED_COIN], self.cfg)
        self.assertIsNone(coin, "complete (already on DEX) should be skipped")

    def test_no_match_when_absent(self):
        coin = lf.match_term_to_coin("totallyunrelatedxyz", [PILLY_COIN], self.cfg)
        self.assertIsNone(coin)

    def test_high_market_cap_skipped(self):
        big_cfg = dict(self.cfg, max_market_cap_usd=100)  # mc 2797 > cap 100
        coin = lf.match_term_to_coin("pilly", [PILLY_COIN], big_cfg)
        self.assertIsNone(coin)

    def test_choose_best_of_multiple(self):
        similar = dict(PILLY_COIN, symbol="PILL", mint="PillMint0000000")
        coin = lf.match_term_to_coin("pilly", [similar, PILLY_COIN], self.cfg)
        self.assertEqual(coin["mint"], PILLY_COIN["mint"], "exact match should win")


class TestToDexPair(unittest.TestCase):
    def test_proxy_shape(self):
        dp = lf.to_dex_pair(PILLY_COIN)
        self.assertTrue(dp["pump_fun"])
        self.assertGreater(dp["liquidity"]["usd"], 0)
        # price = mc / supply
        self.assertIsNotNone(dp.get("priceUsd"))
        self.assertEqual(dp["raw_base"]["address"], PILLY_COIN["mint"])

    def test_empty_coin_safe(self):
        dp = lf.to_dex_pair({})
        self.assertEqual(dp["liquidity"]["usd"], 0)
        self.assertIsNotNone(dp["raw_base"])


class TestComputeActivityMetrics(unittest.TestCase):
    TF_KEYS = ["1m", "5m", "10m", "15m", "30m", "1h"]

    def test_zero_rpc_returns_positive_volume(self):
        # Even with no on-chain signatures (fresh launch), the estimator must
        # return a positive volume bound, not a hard 0 a user reads as "dead".
        from unittest.mock import patch
        with patch("rpc_client.rpc_call", return_value=[]):
            # PILLY has real_sol_reserves & recent created_timestamp
            m = lf.compute_activity_metrics(PILLY_COIN)
        self.assertIn("swap_by_tf", m)
        self.assertIn("volume_by_tf", m)
        self.assertEqual(m["swap_count"], 0)
        self.assertGreater(m["volume_usd_est"], 0, "estimator should give a bound")
        # per-TF volume should be a smooth rising curve (1m < 5m < ... < 1h)
        vols = [m["volume_by_tf"][k] for k in self.TF_KEYS]
        self.assertEqual(vols, sorted(vols), "per-TF vol should increase with window")

    def test_counts_h1_signatures(self):
        from unittest.mock import patch
        import time
        now = int(time.time())
        old = now - 7200
        # two recent (<1h), one old (>1h)
        sigs = [
            {"blockTime": now - 100},   # 100s ago -> in window
            {"blockTime": now - 50},    # in window
            {"blockTime": old},          # out of window
        ]
        with patch("rpc_client.rpc_call", return_value=sigs):
            m = lf.compute_activity_metrics(PILLY_COIN)
        self.assertEqual(m["swap_count_h1"], 2)
        self.assertIn("swap_by_tf", m)
        self.assertIn("volume_by_tf", m)

    def test_swap_bucketed_per_timeframe(self):
        from unittest.mock import patch
        import time
        now = int(time.time())
        # one sig inside 1m, one inside 5m but outside 1m, one inside 30m
        # but outside 15m => cumulative buckets
        sigs = [
            {"blockTime": now - 30},     # 1m + 5m + 10m + 15m + 30m + 1h
            {"blockTime": now - 250},    # not in 1m, yes in 5m+
            {"blockTime": now - 2000},   # 2000s: in 30m(1800)? no (2000>1800), yes in 1h
        ]
        with patch("rpc_client.rpc_call", return_value=sigs):
            m = lf.compute_activity_metrics(PILLY_COIN)
        sb = m["swap_by_tf"]
        self.assertEqual(sb["1m"], 1)     # only the 30s sig
        self.assertEqual(sb["5m"], 2)     # 30s + 250s
        self.assertEqual(sb["10m"], 2)
        self.assertEqual(sb["15m"], 2)
        self.assertEqual(sb["30m"], 2)    # 2000s outside 30m window
        self.assertEqual(sb["1h"], 3)     # all three


if __name__ == "__main__":
    unittest.main()
