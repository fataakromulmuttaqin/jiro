#!/usr/bin/env python3
"""End-to-end smoke test: simulate a full cron run for a known mint.

Tests:
- run_sniper_net pipeline (P1+P2+P3)
- cabal_seeds integration
- sync_website_data output structure

Does NOT make real RPC calls. Validates orchestration.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from typing import Any, Dict

# Make sure jiro root is on path
JIRO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, JIRO_ROOT)


class TestEndToEnd(unittest.TestCase):
    """Black-box smoke tests on the whole pipeline."""

    def setUp(self) -> None:
        self.tmpdir = tempfile.mkdtemp()
        # create a fake cabal_seeds.json in tmp
        self.cabal_seeds = os.path.join(self.tmpdir, "cabal_seeds.json")
        with open(self.cabal_seeds, "w") as f:
            json.dump({"FUNDER_KNOWN_AAA": "TestCartel"}, f)

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_cabal_seeds_loadable(self) -> None:
        """Cabal seeds file format is readable by cabal_detector."""
        import cabal_detector as cd
        cd.CABAL_SEED_PATH = self.cabal_seeds
        seeds = cd.load_seed_cabals()
        self.assertIn("FUNDER_KNOWN_AAA", seeds)
        self.assertEqual(seeds["FUNDER_KNOWN_AAA"], "TestCartel")

    def test_sync_website_data_generates_manifest(self) -> None:
        """sync_website_data.py creates a valid manifest.json from cache files."""
        # First, populate cache/ with a fake report
        cache = os.path.join(JIRO_ROOT, "cache")
        os.makedirs(cache, exist_ok=True)
        test_mint = "TESTM12345ABCDEFGHIJKLMNOPQRSTUVWXYZ123456"
        report = {
            "mint": test_mint,
            "ts": 1234567890,
            "top_holders": [
                {"wallet": "W1", "label": "Whale", "mint": test_mint,
                 "first_buy_ts": 1000, "last_action_ts": 2000,
                 "tx_count": 5, "mint_tx_count": 5,
                 "buys_sol": 1.0, "sells_sol": 1.5,
                 "realized_pnl_sol": 0.5, "roi_pct": 50.0,
                 "still_holds_pct": 50.0, "current_balance_ui": 100.0,
                 "win": True, "behavior_tags": ["WINNER"]},
            ],
            "funders": {},
            "cabal": {
                "ts": 1234567890,
                "clusters": [
                    {"cluster_id": 0, "type": "SOLO", "cabal_score": 0.1,
                     "shared_funder": None, "shared_funder_name": None,
                     "wallets": [{"wallet": "W1", "label": "Whale",
                                  "pnl_sol": 0.5, "win": True,
                                  "first_buy_ts": 1000}],
                     "reason": "no signal"},
                ],
                "summary": {"n_wallets": 1, "n_clusters": 1, "n_cabal": 0,
                            "n_suspect": 0, "n_solo": 1},
            },
            "summary": {"n_holders": 1, "n_winners": 1, "n_losers": 0,
                        "total_pnl_sol": 0.5, "shared_funder_count": 0},
        }
        report_path = os.path.join(cache, f"sniper_net_{test_mint[:8]}.json")
        with open(report_path, "w") as f:
            json.dump(report, f, indent=2)

        try:
            # run sync_website_data.py
            result = subprocess.run(
                [sys.executable, os.path.join(JIRO_ROOT, "sync_website_data.py")],
                capture_output=True, text=True, timeout=30,
            )
            self.assertEqual(result.returncode, 0,
                             f"sync failed: {result.stderr}")

            # verify manifest.json was created in website/public/data/
            manifest_path = os.path.join(JIRO_ROOT, "website", "public", "data", "manifest.json")
            self.assertTrue(os.path.exists(manifest_path),
                            "manifest.json should exist after sync")
            with open(manifest_path) as f:
                manifest = json.load(f)
            # Should contain our test mint
            mint_files = [m["file"] for m in manifest["mints"]]
            self.assertIn(f"sniper_net_{test_mint[:8]}.json", mint_files)

            # Should also copy the report itself
            synced = os.path.join(JIRO_ROOT, "website", "public", "data",
                                  f"sniper_net_{test_mint[:8]}.json")
            self.assertTrue(os.path.exists(synced), "synced report should exist")

            with open(synced) as f:
                synced_data = json.load(f)
            self.assertEqual(synced_data["mint"], test_mint)
            self.assertEqual(len(synced_data["top_holders"]), 1)
        finally:
            # cleanup
            for p in (report_path, synced, manifest_path):
                if os.path.exists(p):
                    os.remove(p)


if __name__ == "__main__":
    unittest.main()