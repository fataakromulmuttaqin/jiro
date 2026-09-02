#!/usr/bin/env python3
"""Unit tests for launch_finder — token->narrative matching and dex_pair proxy.
No network; pure logic tests."""

import sys
import os
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import launch_finder as lf

# A fake fresh coin matching "pilly"
PILLY_COIN = {
    "mint": "PillyPumpMint12345678901234567890123456789012",
    "symbol": "PILLY",
    "name": "Pilly",
    "description": "the alon dev deployed a dog",
    "usd_market_cap": 2797,
    "market_cap_usd": 2797,
    "created_timestamp": 1788310526000,  # recent (this test sets age via cfg)
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


if __name__ == "__main__":
    unittest.main()
