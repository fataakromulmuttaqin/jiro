#!/usr/bin/env python3
"""Tests for cabal_seeds loading + cabal_detector seed integration."""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cabal_detector as cd


def _profile(wallet, *, pnl=0.0, win=False):
    return {
        "wallet": wallet,
        "label": wallet[:6],
        "first_buy_ts": 1000,
        "realized_pnl_sol": pnl,
        "win": win,
    }


class TestLoadSeedCabals(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.original_path = cd.CABAL_SEED_PATH
        cd.CABAL_SEED_PATH = os.path.join(self.tmpdir, "cabal_seeds.json")

    def tearDown(self):
        import shutil
        cd.CABAL_SEED_PATH = self.original_path
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_missing_returns_empty(self):
        """No file → empty dict, no error."""
        self.assertEqual(cd.load_seed_cabals(), {})

    def test_loads_valid_seed_file(self):
        """Valid JSON {addr: name} → returned as dict."""
        with open(cd.CABAL_SEED_PATH, "w") as f:
            json.dump({
                "FUNDER_A": "CashCartel",
                "FUNDER_B": "MevSquad",
            }, f)
        result = cd.load_seed_cabals()
        self.assertEqual(result, {
            "FUNDER_A": "CashCartel",
            "FUNDER_B": "MevSquad",
        })

    def test_corrupt_json_returns_empty(self):
        """Bad JSON → empty dict, no crash."""
        with open(cd.CABAL_SEED_PATH, "w") as f:
            f.write("not json {{{")
        self.assertEqual(cd.load_seed_cabals(), {})

    def test_non_dict_returns_empty(self):
        """File contains a list (not dict) → empty."""
        with open(cd.CABAL_SEED_PATH, "w") as f:
            json.dump(["FUNDER_A", "FUNDER_B"], f)
        self.assertEqual(cd.load_seed_cabals(), {})


class TestSeedBoostInDetect(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.original_path = cd.CABAL_SEED_PATH
        cd.CABAL_SEED_PATH = os.path.join(self.tmpdir, "cabal_seeds.json")
        # Seed a known cabal funder
        with open(cd.CABAL_SEED_PATH, "w") as f:
            json.dump({"FUNDER_KNOWN": "KnownCartel"}, f)

    def tearDown(self):
        import shutil
        cd.CABAL_SEED_PATH = self.original_path
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_known_funder_labeled(self):
        """Two wallets, shared funder is in seed DB → cluster labeled with name."""
        h = [_profile("W1"), _profile("W2")]
        result = cd.detect_clusters(
            holders=h,
            funders_map={"FUNDER_KNOWN": ["W1", "W2"]},
            funder_details={},
        )
        cabal = next(c for c in result["clusters"] if c["type"] == "CABAL")
        self.assertEqual(cabal["shared_funder_name"], "KnownCartel")
        self.assertIn("KnownCartel", cabal["reason"])

    def test_unknown_funder_unlabeled(self):
        """Shared funder NOT in seed DB → name stays None."""
        h = [_profile("W1"), _profile("W2")]
        result = cd.detect_clusters(
            holders=h,
            funders_map={"FUNDER_UNKNOWN": ["W1", "W2"]},
            funder_details={},
        )
        cabal = next(c for c in result["clusters"] if c["type"] == "CABAL")
        self.assertIsNone(cabal["shared_funder_name"])
        # Reason should still mention 'shared funder' marker
        self.assertIn("shared funder", cabal["reason"])
        # And mention a short prefix of the funder
        self.assertIn("FUNDER", cabal["reason"])


if __name__ == "__main__":
    unittest.main()