#!/usr/bin/env python3
"""Integration test — full P1+P2+P3 pipeline with all RPC mocked."""

import json
import os
import sys
import tempfile
import unittest
from typing import Any, Dict, List

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import run_sniper_net as rsn
import wallet_profiler as wp
import fund_flow as ff
import cabal_detector as cd
import behavior_miner as bm
import watchlist_updater as wu


class _MockRpc:
    """One mock for the entire pipeline."""

    def __init__(self) -> None:
        self.txs: Dict[str, Dict[str, Any]] = {}
        self.sigs: Dict[str, List[Dict[str, Any]]] = {}

    def __call__(self, method: str, params: List[Any]) -> Any:
        if method == "getSignaturesForAddress":
            return list(self.sigs.get(params[0], []))
        if method == "getTransaction":
            return self.txs.get(params[0])
        return None


def _mk_sig(sig: str, ts: int) -> Dict[str, Any]:
    return {"signature": sig, "blockTime": ts}


def _mk_xfer(sig: str, ts: int, sender: str, receiver: str, sol: float) -> Dict[str, Any]:
    """A SOL-only transfer tx (no token balances)."""
    return {
        "signature": sig,
        "blockTime": ts,
        "meta": {
            "preBalances": [10_000_000_000, 0],
            "postBalances": [int(10_000_000_000 - sol * 1e9), int(sol * 1e9)],
            "preTokenBalances": [],
            "postTokenBalances": [],
        },
        "message": {"accountKeys": [sender, receiver]},
    }


def _mk_swap(sig: str, ts: int, wallet: str, pool: str, mint: str,
             sol_delta: float, mint_delta: float, mint_pre: float = 0) -> Dict[str, Any]:
    """Simulate a swap that moves the wallet's token balance.

    mint_delta > 0 → buy (pre=0, post=mint_delta)
    mint_delta < 0 → sell (pre=mint_pre, post=pre+mint_delta)
    """
    if mint_delta > 0:
        pre_balances: List = []
        post_balances = [
            {"accountIndex": 0, "mint": mint, "owner": wallet,
             "uiTokenAmount": {"uiAmount": mint_delta}},
        ]
    elif mint_delta < 0:
        pre_balances = [
            {"accountIndex": 0, "mint": mint, "owner": wallet,
             "uiTokenAmount": {"uiAmount": mint_pre}},
        ]
        post_balances = [
            {"accountIndex": 0, "mint": mint, "owner": wallet,
             "uiTokenAmount": {"uiAmount": mint_pre + mint_delta}},
        ]
    else:
        pre_balances = []
        post_balances = []

    return {
        "signature": sig, "blockTime": ts,
        "meta": {
            "preBalances": [10_000_000_000, 100_000_000_000],
            "postBalances": [int(10_000_000_000 + sol_delta * 1e9), 100_000_000_000],
            "preTokenBalances": pre_balances,
            "postTokenBalances": post_balances,
        },
        "message": {"accountKeys": [wallet, pool]},
    }


class TestPipeline(unittest.TestCase):
    """End-to-end: two holders, same funder → CABAL cluster detected."""

    MINT = "MINT_INTEGRATION"
    FUNDER = "WALLET_FUNDER_AAA"
    W1 = "WALLET_TOP1"
    W2 = "WALLET_TOP2"
    POOL = "POOL_DEX"

    def setUp(self) -> None:
        self.mock = _MockRpc()
        wp._rpc = self.mock
        ff._rpc = self.mock
        wp._cache.clear()
        ff._cache.clear()
        cd._cache.clear()
        bm._cache_load() if hasattr(bm, "_cache_load") else None
        # isolate watchlist to a tmp dir
        self.tmpdir = tempfile.mkdtemp()
        wu.WATCHLIST_PATH = os.path.join(self.tmpdir, "watchlist.json")
        wu.WATCHLIST_DIFF_PATH = os.path.join(self.tmpdir, "watchlist_diff.json")
        wu.MIN_WATCHLIST_AGE_HOURS = 0
        wu.WINNER_MIN_PNL_SOL = 0.05

        # Mock holder_analyzer.get_top_holders to return our two wallets
        self._orig_holders = wp.holder_analyzer.get_top_holders if hasattr(wp, "holder_analyzer") else None

        # Inject monkey-patch into profile_top_holders module
        import profile_top_holders as pth
        self._orig_pth_holder = pth.get_top_holders
        pth.get_top_holders = self._fake_holders  # type: ignore[assignment]

    def tearDown(self) -> None:
        import shutil
        import profile_top_holders as pth
        pth.get_top_holders = self._orig_pth_holder  # type: ignore[assignment]
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _fake_holders(self, mint: str, top_n: int) -> List[Dict[str, Any]]:
        return [
            {"address": self.W1, "label": "TopHolder1", "pct": 15.0},
            {"address": self.W2, "label": "TopHolder2", "pct": 10.0},
        ][:top_n]

    def _seed_buys(self) -> None:
        """W1 buys and sells profitably, W2 same. Both got SOL from FUNDER."""
        # W1 funding from FUNDER (no token balances)
        self.mock.sigs[self.W1] = [_mk_sig("fund1", 900), _mk_sig("buy1", 1000), _mk_sig("sell1", 2000)]
        self.mock.sigs[self.W2] = [_mk_sig("fund2", 950), _mk_sig("buy2", 1100), _mk_sig("sell2", 2100)]
        self.mock.txs["fund1"] = _mk_xfer("fund1", 900, self.FUNDER, self.W1, 1.0)
        self.mock.txs["fund2"] = _mk_xfer("fund2", 950, self.FUNDER, self.W2, 1.0)
        # buys at +1 SOL, sells at +2 SOL → 1 SOL profit each
        self.mock.txs["buy1"] = _mk_swap("buy1", 1000, self.W1, self.POOL, self.MINT, -1.0, 1000)
        self.mock.txs["buy2"] = _mk_swap("buy2", 1100, self.W2, self.POOL, self.MINT, -1.0, 1000)
        self.mock.txs["sell1"] = _mk_swap("sell1", 2000, self.W1, self.POOL, self.MINT, 2.0, -1000, mint_pre=1000)
        self.mock.txs["sell2"] = _mk_swap("sell2", 2100, self.W2, self.POOL, self.MINT, 2.0, -1000, mint_pre=1000)

    def test_full_pipeline_produces_cabal_and_promotes(self) -> None:
        """Two winners with same funder → CABAL + both promoted to watchlist."""
        self._seed_buys()
        report = rsn.run_for_mint(self.MINT, top_n=2, use_cache=True, update_watchlist=True)

        # 1. Profiles computed
        self.assertEqual(len(report["top_holders"]), 2)
        for p in report["top_holders"]:
            self.assertTrue(p.get("win"))
            self.assertGreater(p.get("realized_pnl_sol") or 0, 0)

        # 2. Cabal cluster detected
        cabal = report.get("cabal") or {}
        self.assertGreaterEqual(cabal.get("summary", {}).get("n_cabal", 0), 1)
        cabal_clusters = [c for c in cabal.get("clusters", []) if c["type"] == "CABAL"]
        self.assertEqual(len(cabal_clusters), 1)
        self.assertEqual(cabal_clusters[0]["shared_funder"], self.FUNDER)

        # 3. Behavior tagged
        behaviors = report.get("behavior") or []
        self.assertEqual(len(behaviors), 2)
        for b in behaviors:
            self.assertIn(b["tag"], {"WINNER", "EARLY_EXIT", "SWING", "EXIT_LIQUIDITY"})

        # 4. Watchlist updated — both wallets promoted
        diff = report.get("watchlist_diff") or {}
        self.assertEqual(len(diff["added"]), 2)
        added_addrs = {a["address"] for a in diff["added"]}
        self.assertIn(self.W1, added_addrs)
        self.assertIn(self.W2, added_addrs)
        self.assertEqual(diff["final_size"], 2)

        # Watchlist file actually written
        with open(wu.WATCHLIST_PATH) as f:
            wl = json.load(f)
        self.assertEqual(len(wl), 2)
        for entry in wl:
            self.assertTrue(entry["label"].startswith("JSN winner"))
            self.assertEqual(entry["source"], "sniper_net")


if __name__ == "__main__":
    unittest.main()