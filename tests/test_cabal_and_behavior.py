#!/usr/bin/env python3
"""Tests for cabal_detector.py + behavior_miner.py — pure analytics, no RPC."""

import os
import sys
import unittest
from typing import Any, Dict, List

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cabal_detector as cd
import behavior_miner as bm


# ---------------------------------------------------------------------------
# cabal_detector tests
# ---------------------------------------------------------------------------

def _profile(
    wallet: str,
    *,
    first_buy_ts: int = 1000,
    realized_pnl_sol: float = 0.0,
    win: bool = False,
) -> Dict[str, Any]:
    return {
        "wallet": wallet,
        "label": wallet[:6],
        "first_buy_ts": first_buy_ts,
        "last_action_ts": first_buy_ts + 100,
        "buys_sol": 1.0,
        "sells_sol": 1.0 + realized_pnl_sol,
        "realized_pnl_sol": realized_pnl_sol,
        "win": win,
    }


class TestCoBuyGroups(unittest.TestCase):

    def test_no_holders(self) -> None:
        self.assertEqual(cd._co_buy_groups([], window_s=300), [])

    def test_single_holder(self) -> None:
        h = [_profile("W1", first_buy_ts=1000)]
        self.assertEqual(cd._co_buy_groups(h, window_s=300), [])

    def test_two_within_window(self) -> None:
        h = [
            _profile("W1", first_buy_ts=1000),
            _profile("W2", first_buy_ts=1100),  # 100s later
        ]
        groups = cd._co_buy_groups(h, window_s=300)
        self.assertEqual(len(groups), 1)
        self.assertEqual(set(groups[0]["members"]), {"W1", "W2"})

    def test_outside_window_splits(self) -> None:
        h = [
            _profile("W1", first_buy_ts=1000),
            _profile("W2", first_buy_ts=5000),  # way later
        ]
        groups = cd._co_buy_groups(h, window_s=300)
        self.assertEqual(len(groups), 0)

    def test_three_in_window_groups_together(self) -> None:
        h = [
            _profile("W1", first_buy_ts=1000),
            _profile("W2", first_buy_ts=1100),
            _profile("W3", first_buy_ts=1200),
        ]
        groups = cd._co_buy_groups(h, window_s=300)
        self.assertEqual(len(groups), 1)
        self.assertEqual(len(groups[0]["members"]), 3)


class TestDetectClusters(unittest.TestCase):

    def test_solo_default(self) -> None:
        """No funders, no co-buy → all SOLO singletons."""
        h = [_profile("W1", first_buy_ts=1000), _profile("W2", first_buy_ts=5000)]
        result = cd.detect_clusters(holders=h, funders_map={}, funder_details={})
        # Two SOLO clusters
        self.assertEqual(result["summary"]["n_solo"], 2)
        self.assertEqual(result["summary"]["n_cabal"], 0)

    def test_shared_funder_creates_cabal(self) -> None:
        """Two holders share a funder → CABAL cluster."""
        h = [_profile("W1"), _profile("W2")]
        result = cd.detect_clusters(
            holders=h,
            funders_map={"FUNDER_A": ["W1", "W2"]},
            funder_details={},
        )
        # One cluster (deduped) with 2 members, type CABAL
        cabal_clusters = [c for c in result["clusters"] if c["type"] == "CABAL"]
        self.assertEqual(len(cabal_clusters), 1)
        self.assertEqual(len(cabal_clusters[0]["wallets"]), 2)

    def test_known_cabal_seed_boosts_score(self) -> None:
        """Seed DB match → score gets +0.3."""
        h = [_profile("W1"), _profile("W2")]
        # monkeypatch load_seed_cabals
        original = cd.load_seed_cabals
        cd.load_seed_cabals = lambda: {"FUNDER_A": "CashCartel"}
        try:
            result = cd.detect_clusters(
                holders=h,
                funders_map={"FUNDER_A": ["W1", "W2"]},
                funder_details={},
            )
            cabal = next(c for c in result["clusters"] if c["type"] == "CABAL")
            self.assertEqual(cabal["shared_funder_name"], "CashCartel")
            self.assertIn("CashCartel", cabal["reason"])
        finally:
            cd.load_seed_cabals = original

    def test_co_buy_alone_marks_solo(self) -> None:
        """Two wallets co-buy within window but NO shared funder → SOLO.

        Co-buy alone is a weak signal — cabal_score = 0.2 for 2 wallets.
        SUSPECT threshold is 0.3. To reach SUSPECT, need 3+ co-buying wallets
        (score 0.4) OR a shared funder (score 0.5+).
        """
        h = [
            _profile("W1", first_buy_ts=1000),
            _profile("W2", first_buy_ts=1100),
        ]
        result = cd.detect_clusters(holders=h, funders_map={}, funder_details={})
        solos = [c for c in result["clusters"] if c["type"] == "SOLO"]
        self.assertEqual(len(solos), 1)  # single deduped cluster
        self.assertEqual(len(solos[0]["wallets"]), 2)
        self.assertIn("co-buy", solos[0]["reason"])

    def test_three_co_buy_marks_suspect(self) -> None:
        """Three co-buying wallets → SUSPECT_CLUSTER (score 0.4)."""
        h = [
            _profile("W1", first_buy_ts=1000),
            _profile("W2", first_buy_ts=1100),
            _profile("W3", first_buy_ts=1200),
        ]
        result = cd.detect_clusters(holders=h, funders_map={}, funder_details={})
        suspects = [c for c in result["clusters"] if c["type"] == "SUSPECT_CLUSTER"]
        self.assertEqual(len(suspects), 1)
        self.assertEqual(len(suspects[0]["wallets"]), 3)

    def test_funder_and_cobuy_dedup(self) -> None:
        """Two holders are both shared-funder AND co-buy → single cluster."""
        h = [
            _profile("W1", first_buy_ts=1000),
            _profile("W2", first_buy_ts=1100),
        ]
        result = cd.detect_clusters(
            holders=h,
            funders_map={"FUNDER_A": ["W1", "W2"]},
            funder_details={"FUNDER_A": {"first_seen_as_funder": 900}},
        )
        # Should have exactly one cluster of size 2 (deduped)
        size2 = [c for c in result["clusters"] if len(c["wallets"]) == 2]
        self.assertEqual(len(size2), 1)

    def test_all_winners_boosts_score(self) -> None:
        """All-winners cluster gets +0.2."""
        h = [
            _profile("W1", realized_pnl_sol=1.0, win=True),
            _profile("W2", realized_pnl_sol=2.0, win=True),
        ]
        result = cd.detect_clusters(
            holders=h,
            funders_map={"FUNDER_A": ["W1", "W2"]},
            funder_details={"FUNDER_A": {"first_seen_as_funder": 900}},
        )
        cabal = next(c for c in result["clusters"] if c["type"] == "CABAL")
        self.assertIn("all winners", cabal["reason"])

    def test_all_losers_penalized(self) -> None:
        """All-losers cluster gets -0.2."""
        h = [
            _profile("W1", realized_pnl_sol=-1.0, win=False),
            _profile("W2", realized_pnl_sol=-0.5, win=False),
        ]
        result = cd.detect_clusters(
            holders=h,
            funders_map={"FUNDER_A": ["W1", "W2"]},
            funder_details={"FUNDER_A": {"first_seen_as_funder": 900}},
        )
        cabal = next(c for c in result["clusters"] if c["type"] == "CABAL")
        self.assertIn("all losers", cabal["reason"])

    def test_analyze_report_mutates(self) -> None:
        """analyze_report returns same dict with .cabal key added."""
        report = {
            "top_holders": [_profile("W1"), _profile("W2")],
            "funders": {"F1": ["W1", "W2"]},
            "funder_details": {},
        }
        out = cd.analyze_report(report)
        self.assertIn("cabal", out)
        self.assertEqual(out, report)  # same object


# ---------------------------------------------------------------------------
# behavior_miner tests
# ---------------------------------------------------------------------------

class TestClassifyWallet(unittest.TestCase):

    def test_bundler(self) -> None:
        """Single big tx → BUNDLER."""
        p = _profile("W", first_buy_ts=1000)
        p["mint_tx_count"] = 1
        p["buys_sol"] = 10.0
        result = bm.classify_wallet(p)
        self.assertEqual(result["tag"], "BUNDLER")

    def test_sniper(self) -> None:
        """Single tx entry (but smaller) → SNIPER (precedence after bundler)."""
        p = _profile("W", first_buy_ts=1000)
        p["mint_tx_count"] = 1
        p["buys_sol"] = 0.5  # below whale threshold
        result = bm.classify_wallet(p)
        self.assertEqual(result["tag"], "SNIPER")

    def test_early_exit(self) -> None:
        """Bought and sold within 30 min → EARLY_EXIT."""
        p = _profile("W", first_buy_ts=1000)
        p["last_action_ts"] = 1100  # 100s later
        p["mint_tx_count"] = 2
        p["buys_sol"] = 1.0
        p["sells_sol"] = 1.5
        p["realized_pnl_sol"] = 0.5
        p["still_holds_pct"] = 0.0
        result = bm.classify_wallet(p)
        self.assertEqual(result["tag"], "EARLY_EXIT")

    def test_diamond_hand(self) -> None:
        """Still holds 90%+ → DIAMOND_HAND.

        Note: SNIPER triggers before DIAMOND_HAND when mint_tx_count == 1.
        To reach DIAMOND_HAND, need multiple txs but no sells yet.
        """
        p = _profile("W", first_buy_ts=1000)
        p["mint_tx_count"] = 2  # not SNIPER
        p["buys_sol"] = 0.3  # small buy, not WHALE
        p["sells_sol"] = 0.0
        p["still_holds_pct"] = 95.0
        result = bm.classify_wallet(p)
        self.assertEqual(result["tag"], "DIAMOND_HAND")

    def test_whale(self) -> None:
        """Big position → WHALE (after diamond_hand)."""
        p = _profile("W", first_buy_ts=1000)
        p["mint_tx_count"] = 3  # multiple txs, not bundler/sniper
        p["buys_sol"] = 6.0
        p["sells_sol"] = 4.0
        p["realized_pnl_sol"] = -2.0
        p["still_holds_pct"] = 30.0  # not diamond hand
        result = bm.classify_wallet(p)
        self.assertEqual(result["tag"], "WHALE")

    def test_exit_liquidity(self) -> None:
        """Lost money on a sell → EXIT_LIQUIDITY."""
        p = _profile("W", first_buy_ts=1000)
        p["last_action_ts"] = 100000  # held a while
        p["mint_tx_count"] = 3
        p["buys_sol"] = 1.0
        p["sells_sol"] = 0.5  # lost money
        p["realized_pnl_sol"] = -0.5
        p["still_holds_pct"] = 0.0
        result = bm.classify_wallet(p)
        self.assertEqual(result["tag"], "EXIT_LIQUIDITY")

    def test_winner_fallback(self) -> None:
        """No special pattern, made money → WINNER.
        Note: SWING requires holding 1-7 days. To avoid SWING, keep last_action_ts
        within hours, not days."""
        p = _profile("W", first_buy_ts=1000)
        p["last_action_ts"] = 4000  # held ~50 min
        p["mint_tx_count"] = 2
        p["buys_sol"] = 1.0
        p["sells_sol"] = 2.0
        p["realized_pnl_sol"] = 1.0
        p["still_holds_pct"] = 0.0
        result = bm.classify_wallet(p)
        self.assertEqual(result["tag"], "WINNER")

    def test_loser_fallback(self) -> None:
        """No special pattern, lost money, never sold → LOSER.

        Note: SNIPER triggers when mint_tx_count == 1 regardless of buy size.
        To reach EXIT_LIQUIDITY (or LOSER fallback), use multiple txs.
        Also note: EARLY_EXIT requires held_seconds <= EARLY_EXIT_WINDOW_S
        AND sells_sol > 0 AND still_holds < 10%. EXIT_LIQUIDITY requires
        pnl < 0 AND sells_sol > 0. Held > 30 min to skip EARLY_EXIT.
        """
        p = _profile("W", first_buy_ts=1000)
        p["last_action_ts"] = 100000  # held ~27.5h, past EARLY_EXIT window
        p["mint_tx_count"] = 2  # not SNIPER
        p["buys_sol"] = 1.0
        p["sells_sol"] = 0.5  # lost money → EXIT_LIQUIDITY triggers first
        p["realized_pnl_sol"] = -0.5
        p["still_holds_pct"] = 0.0
        result = bm.classify_wallet(p)
        # EXIT_LIQUIDITY has higher precedence than LOSER fallback
        self.assertEqual(result["tag"], "EXIT_LIQUIDITY")

    def test_classify_all(self) -> None:
        profiles = [
            # mint_tx_count=2, holds 95% → DIAMOND_HAND (not SNIPER)
            {**_profile("W1", first_buy_ts=1000), "mint_tx_count": 2, "buys_sol": 0.5, "sells_sol": 0.0, "realized_pnl_sol": 0.0, "still_holds_pct": 95.0},
            # mint_tx_count=1, big buy → BUNDLER
            {**_profile("W2", first_buy_ts=1000), "mint_tx_count": 1, "buys_sol": 10.0, "sells_sol": 0.0, "realized_pnl_sol": 0.0, "still_holds_pct": 100.0},
        ]
        result = bm.classify_all(profiles)
        tags = [c["tag"] for c in result]
        self.assertEqual(tags[0], "DIAMOND_HAND")
        self.assertEqual(tags[1], "BUNDLER")

    def test_merge_into_profiles_mutates(self) -> None:
        profiles = [{**_profile("W1", first_buy_ts=1000), "behavior_tags": []}]
        classifications = bm.classify_all(profiles)
        result = bm.merge_into_profiles(profiles, classifications)
        self.assertEqual(len(profiles[0]["behavior_tags"]), 1)
        self.assertIn("behavior_reason", profiles[0])
        self.assertEqual(result, profiles)


if __name__ == "__main__":
    unittest.main()