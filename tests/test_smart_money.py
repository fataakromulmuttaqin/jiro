#!/usr/bin/env python3
"""Unit tests for smart_money with mocked RPC."""

import sys
import os
import time
import unittest
import tempfile
from unittest.mock import patch, mock_open

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import smart_money


class TestLoadWatchlist(unittest.TestCase):
    def test_missing_file_returns_empty(self):
        smart_money._recent_buys.clear()
        result = smart_money.load_watchlist(path="/nonexistent/path.json")
        self.assertEqual(result, [])

    def test_valid_file_filters_empty_addresses(self):
        data = '[{"address": "Addr1", "label": "L1"}, {"address": "", "label": "skipped"}]'
        with patch("smart_money.os.path.exists", return_value=True), \
             patch("smart_money.open", mock_open(read_data=data)):
            result = smart_money.load_watchlist(path="watchlist.json")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["address"], "Addr1")

    def test_invalid_json_returns_empty(self):
        with patch("smart_money.os.path.exists", return_value=True), \
             patch("smart_money.open", mock_open(read_data="not json")):
            result = smart_money.load_watchlist(path="watchlist.json")
        self.assertEqual(result, [])


class TestExtractBuysFromTx(unittest.TestCase):
    def test_detects_balance_increase(self):
        tx = {
            "meta": {
                "preTokenBalances": [
                    {"accountIndex": 1, "mint": "MINT_X", "owner": "OWNER",
                     "uiTokenAmount": {"uiAmount": 10}}
                ],
                "postTokenBalances": [
                    {"accountIndex": 1, "mint": "MINT_X", "owner": "OWNER",
                     "uiTokenAmount": {"uiAmount": 25}}
                ],
            }
        }
        mints = smart_money._extract_buys_from_tx(tx, "OWNER")
        self.assertEqual(mints, ["MINT_X"])

    def test_ignores_balance_decrease(self):
        tx = {
            "meta": {
                "preTokenBalances": [
                    {"accountIndex": 1, "mint": "MINT_X", "owner": "OWNER",
                     "uiTokenAmount": {"uiAmount": 25}}
                ],
                "postTokenBalances": [
                    {"accountIndex": 1, "mint": "MINT_X", "owner": "OWNER",
                     "uiTokenAmount": {"uiAmount": 10}}
                ],
            }
        }
        mints = smart_money._extract_buys_from_tx(tx, "OWNER")
        self.assertEqual(mints, [])

    def test_ignores_other_owners(self):
        tx = {
            "meta": {
                "preTokenBalances": [],
                "postTokenBalances": [
                    {"accountIndex": 1, "mint": "MINT_X", "owner": "SOMEONE_ELSE",
                     "uiTokenAmount": {"uiAmount": 25}}
                ],
            }
        }
        mints = smart_money._extract_buys_from_tx(tx, "OWNER")
        self.assertEqual(mints, [])

    def test_handles_missing_meta(self):
        self.assertEqual(smart_money._extract_buys_from_tx({}, "OWNER"), [])
        self.assertEqual(smart_money._extract_buys_from_tx({"meta": {}}, "OWNER"), [])


class TestCheckConvergence(unittest.TestCase):
    def setUp(self):
        smart_money._recent_buys.clear()
        smart_money._seen_signatures.clear()

    def test_no_recent_buys_no_convergence(self):
        result = smart_money.check_convergence("MINT_X", min_wallets=2, window_seconds=900)
        self.assertFalse(result["converged"])
        self.assertEqual(result["wallet_count"], 0)

    def test_one_wallet_bought_no_convergence(self):
        now = time.time()
        smart_money._recent_buys.append(
            {"wallet": "W1", "label": "L1", "mint": "MINT_X", "ts": now, "signature": "s1"}
        )
        result = smart_money.check_convergence("MINT_X", min_wallets=2, window_seconds=900)
        self.assertFalse(result["converged"])

    def test_two_wallets_bought_convergence_hit(self):
        now = time.time()
        smart_money._recent_buys.append(
            {"wallet": "W1", "label": "CashCat", "mint": "MINT_X", "ts": now, "signature": "s1"}
        )
        smart_money._recent_buys.append(
            {"wallet": "W2", "label": "SniperA", "mint": "MINT_X", "ts": now - 60, "signature": "s2"}
        )
        result = smart_money.check_convergence("MINT_X", min_wallets=2, window_seconds=900)
        self.assertTrue(result["converged"])
        self.assertEqual(result["wallet_count"], 2)
        labels = {w["label"] for w in result["wallets"]}
        self.assertEqual(labels, {"CashCat", "SniperA"})

    def test_old_buys_outside_window_ignored(self):
        now = time.time()
        smart_money._recent_buys.append(
            {"wallet": "W1", "label": "L1", "mint": "MINT_X", "ts": now - 3600, "signature": "s1"}
        )
        smart_money._recent_buys.append(
            {"wallet": "W2", "label": "L2", "mint": "MINT_X", "ts": now - 3600, "signature": "s2"}
        )
        result = smart_money.check_convergence("MINT_X", min_wallets=2, window_seconds=900)
        self.assertFalse(result["converged"])

    def test_different_mints_dont_cross_count(self):
        now = time.time()
        smart_money._recent_buys.append(
            {"wallet": "W1", "label": "L1", "mint": "MINT_A", "ts": now, "signature": "s1"}
        )
        smart_money._recent_buys.append(
            {"wallet": "W2", "label": "L2", "mint": "MINT_B", "ts": now, "signature": "s2"}
        )
        result = smart_money.check_convergence("MINT_A", min_wallets=2, window_seconds=900)
        self.assertFalse(result["converged"])


class TestPollWatchlist(unittest.TestCase):
    def setUp(self):
        smart_money._recent_buys.clear()
        smart_money._seen_signatures.clear()

    def test_empty_watchlist_returns_no_buys(self):
        result = smart_money.poll_watchlist(watchlist=[])
        self.assertEqual(result, [])

    def test_deduplicates_signatures_across_calls(self):
        wallet = {"address": "W1", "label": "L1"}
        sigs = [{"signature": "sig1", "blockTime": int(time.time())}]
        tx = {
            "meta": {
                "preTokenBalances": [],
                "postTokenBalances": [
                    {"accountIndex": 0, "mint": "MINT_X", "owner": "W1",
                     "uiTokenAmount": {"uiAmount": 100}}
                ],
            }
        }
        def rpc(method, params):
            if method == "getSignaturesForAddress":
                return sigs
            if method == "getTransaction":
                return tx
            return None
        with patch.object(smart_money, "_rpc", side_effect=rpc):
            r1 = smart_money.poll_watchlist(watchlist=[wallet])
            r2 = smart_money.poll_watchlist(watchlist=[wallet])
        self.assertEqual(len(r1), 1)
        self.assertEqual(len(r2), 0, "second poll should not re-detect same sig")


if __name__ == "__main__":
    unittest.main()