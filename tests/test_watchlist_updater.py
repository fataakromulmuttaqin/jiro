#!/usr/bin/env python3
"""Tests for watchlist_updater.py — file-IO mocked via tmp dirs."""

import json
import os
import sys
import tempfile
import unittest
from typing import Any, Dict, List

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import watchlist_updater as wu


def _profile(wallet: str, *, pnl: float, win: bool, mint: str = "MINT_X") -> Dict[str, Any]:
    return {
        "wallet": wallet,
        "label": wallet[:6],
        "mint": mint,
        "first_buy_ts": 1000,
        "buys_sol": 1.0,
        "sells_sol": 1.0 + pnl,
        "realized_pnl_sol": pnl,
        "win": win,
    }


class TestWatchlistUpdater(unittest.TestCase):

    def setUp(self) -> None:
        # isolate each test with its own tmp watchlist file
        self.tmpdir = tempfile.mkdtemp()
        self.watchlist_path = os.path.join(self.tmpdir, "watchlist.json")
        self.diff_path = os.path.join(self.tmpdir, "watchlist_diff.json")
        wu.WATCHLIST_PATH = self.watchlist_path
        wu.WATCHLIST_DIFF_PATH = self.diff_path
        wu.MIN_WATCHLIST_AGE_HOURS = 0  # let everything be prune-eligible in tests
        wu.WINNER_MIN_PNL_SOL = 0.05
        wu.LOSER_MAX_PNL_SOL = -0.05

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_empty_promote(self) -> None:
        """No existing watchlist, one winner → watchlist gets 1 entry."""
        report = {"mint": "M1", "top_holders": [_profile("W1", pnl=1.0, win=True)]}
        diff = wu.update_from_report(report)
        self.assertEqual(len(diff["added"]), 1)
        self.assertEqual(diff["added"][0]["address"], "W1")
        self.assertEqual(diff["final_size"], 1)
        self.assertEqual(len(diff["pruned"]), 0)
        # file actually written
        self.assertTrue(os.path.exists(self.watchlist_path))

    def test_no_double_add(self) -> None:
        """If wallet already in watchlist, don't re-add."""
        # seed watchlist
        with open(self.watchlist_path, "w") as f:
            json.dump([
                {"address": "W1", "label": "existing", "added_ts": 0, "source": "manual"}
            ], f)
        report = {"mint": "M1", "top_holders": [_profile("W1", pnl=1.0, win=True)]}
        diff = wu.update_from_report(report)
        self.assertEqual(len(diff["added"]), 0)
        self.assertEqual(diff["final_size"], 1)

    def test_loser_pruned(self) -> None:
        """Existing wallet observed losing → pruned."""
        # seed: W1 is in watchlist, added 2 days ago
        with open(self.watchlist_path, "w") as f:
            json.dump([
                {"address": "W1", "label": "existing", "added_ts": 0, "source": "sniper_net"}
            ], f)
        report = {"mint": "M1", "top_holders": [_profile("W1", pnl=-0.5, win=False)]}
        diff = wu.update_from_report(report)
        self.assertEqual(len(diff["pruned"]), 1)
        self.assertEqual(diff["pruned"][0]["address"], "W1")
        self.assertEqual(diff["final_size"], 0)

    def test_manual_label_protected(self) -> None:
        """Entries with [manual] label are never pruned."""
        with open(self.watchlist_path, "w") as f:
            json.dump([
                {"address": "W1", "label": "[manual] my favorite", "added_ts": 0, "source": "manual"}
            ], f)
        report = {"mint": "M1", "top_holders": [_profile("W1", pnl=-0.5, win=False)]}
        diff = wu.update_from_report(report)
        self.assertEqual(len(diff["pruned"]), 0)
        self.assertEqual(diff["final_size"], 1)

    def test_fresh_entry_not_pruned(self) -> None:
        """Entries added < MIN_AGE_HOURS ago are protected from pruning."""
        wu.MIN_WATCHLIST_AGE_HOURS = 24
        with open(self.watchlist_path, "w") as f:
            json.dump([
                {"address": "W1", "label": "fresh", "added_ts": int(__import__("time").time()), "source": "sniper_net"}
            ], f)
        report = {"mint": "M1", "top_holders": [_profile("W1", pnl=-0.5, win=False)]}
        diff = wu.update_from_report(report)
        self.assertEqual(len(diff["pruned"]), 0)
        self.assertEqual(diff["final_size"], 1)

    def test_max_entries_cap(self) -> None:
        """If watchlist is full, no new additions."""
        wu.WATCHLIST_MAX_ENTRIES = 2
        with open(self.watchlist_path, "w") as f:
            json.dump([
                {"address": f"W{i}", "label": f"slot{i}", "added_ts": 0, "source": "manual"}
                for i in range(2)
            ], f)
        report = {
            "mint": "M1",
            "top_holders": [
                _profile("NEW1", pnl=10.0, win=True),
                _profile("NEW2", pnl=20.0, win=True),
            ],
        }
        diff = wu.update_from_report(report)
        self.assertEqual(len(diff["added"]), 0)
        self.assertEqual(diff["final_size"], 2)

    def test_diff_log_appended(self) -> None:
        """Every update writes to diff log."""
        report = {"mint": "M1", "top_holders": [_profile("W1", pnl=1.0, win=True)]}
        wu.update_from_report(report)
        # run again with different wallet
        report2 = {"mint": "M2", "top_holders": [_profile("W2", pnl=2.0, win=True)]}
        wu.update_from_report(report2)
        with open(self.diff_path) as f:
            log_data = json.load(f)
        self.assertEqual(len(log_data), 2)
        self.assertEqual(log_data[0]["added"][0]["address"], "W1")
        self.assertEqual(log_data[1]["added"][0]["address"], "W2")

    def test_mixed_report(self) -> None:
        """Promote winner, prune loser, ignore neutral — all in one report."""
        wu.MIN_WATCHLIST_AGE_HOURS = 0
        # seed with W_LOSER already in watchlist
        with open(self.watchlist_path, "w") as f:
            json.dump([
                {"address": "W_LOSER", "label": "old loser", "added_ts": 0, "source": "sniper_net"}
            ], f)
        report = {
            "mint": "M1",
            "top_holders": [
                _profile("W_WINNER", pnl=1.5, win=True),    # promote
                _profile("W_LOSER", pnl=-1.0, win=False),    # prune
                _profile("W_NEUTRAL", pnl=0.01, win=False),  # skip (pnl too small)
            ],
        }
        diff = wu.update_from_report(report)
        self.assertEqual(len(diff["added"]), 1)
        self.assertEqual(diff["added"][0]["address"], "W_WINNER")
        self.assertEqual(len(diff["pruned"]), 1)
        self.assertEqual(diff["pruned"][0]["address"], "W_LOSER")
        self.assertEqual(diff["final_size"], 1)

    def test_label_format(self) -> None:
        """Winner label includes date and pnl."""
        wu.MIN_WATCHLIST_AGE_HOURS = 0
        report = {"mint": "M1", "top_holders": [_profile("W1", pnl=1.234, win=True)]}
        diff = wu.update_from_report(report)
        label = diff["added"][0]["label"]
        self.assertTrue(label.startswith("JSN winner"))
        self.assertIn("+1.234", label)

    def test_corrupt_watchlist_returns_empty(self) -> None:
        """If watchlist.json is unreadable, treat as empty."""
        with open(self.watchlist_path, "w") as f:
            f.write("not valid json {{{")
        result = wu._load_watchlist()
        self.assertEqual(result, [])


if __name__ == "__main__":
    unittest.main()