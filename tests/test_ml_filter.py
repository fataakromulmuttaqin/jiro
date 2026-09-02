#!/usr/bin/env python3
"""Unit tests for ml_filter (feature engineering + numpy MLP training/inference)
and retrain_ml (skip logic) without real network calls."""

import sys
import os
import time
import json
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ml_filter


def _coin(mc=3000, age_h=2, complete=False, reply=5, creator="Ckq", sol=1e9):
    return {
        "usd_market_cap": mc,
        "market_cap_usd": mc,
        "created_timestamp": int((time.time() - age_h * 3600) * 1000),
        "complete": complete,
        "reply_count": reply,
        "total_supply": 1_000_000_000_000_000,
        "real_sol_reserves": sol,
        "creator": creator,
    }


class TestFeatureVector(unittest.TestCase):
    def test_shape_and_normalized_range(self):
        fv = ml_filter.build_feature_vector(_coin())
        self.assertEqual(fv.shape, (10,))
        self.assertTrue(all(abs(float(v)) < 2.0 for v in fv),
                        "features should be O(1), not massive/unbounded")

    def test_drop_leak_zeroes_mc_fields(self):
        fv = ml_filter.build_feature_vector(_coin(mc=999999), drop_label_leak=True)
        self.assertEqual(float(fv[0]), 0.0)  # log-MC zeroed
        self.assertEqual(float(fv[5]), 0.0)  # pool SOL value zeroed

    def test_no_numpy_returns_zeros(self):
        class _StubNp:
            float32 = "float32"
            @staticmethod
            def zeros(shape, dtype=None):
                return [0.0] * 10
        with patch.object(ml_filter, "_HAVE_NUMPY", False), \
             patch.object(ml_filter, "np", _StubNp()):
            fv = ml_filter.build_feature_vector(_coin())
        self.assertEqual(len(fv), 10)


class TestNumpyMLPDiscriminates(unittest.TestCase):
    def setUp(self):
        # deterministic, separable synthetic data
        import numpy as np
        rng = np.random.RandomState(7)
        n = 200
        # class 1: high MC, high reply; class 0: low MC, low reply
        x1 = np.column_stack([np.full(n, 5.0) + rng.randn(n)*0.2,
                              np.full(n, 1.0) + rng.randn(n)*0.1,
                              np.zeros((n, 8))])
        x0 = np.column_stack([np.full(n, 2.0) + rng.randn(n)*0.2,
                              np.full(n, 0.3) + rng.randn(n)*0.1,
                              np.zeros((n, 8))])
        self.X = np.vstack([x1, x0]).astype(np.float32)
        self.y = np.hstack([np.ones(n), np.zeros(n)])

    def test_numpy_mlp_learns(self):
        model = ml_filter.NumpyMLP(hidden=8, epochs=500, lr=0.5, seed=1)
        model.fit(self.X, self.y)
        proba = model.predict_proba(self.X)[:, 1]
        # model should separate the two classes well
        self.assertGreater(proba[self.y == 1].mean(), proba[self.y == 0].mean())
        # and not saturate to a single value
        self.assertLess(proba.min(), 0.9)


class TestPredictPump(unittest.TestCase):
    def test_no_model_returns_none(self):
        with patch.object(ml_filter, "load_model", return_value=None):
            self.assertIsNone(ml_filter.predict_pump([0.0]*10))

    def test_wrong_length_returns_none(self):
        self.assertIsNone(ml_filter.predict_pump([0.0]*5))


class TestRetrainSkipLogic(unittest.TestCase):
    def test_skip_when_no_new_samples(self):
        import retrain_ml
        # create dirs; ensure samples file is older than last-train stamp
        os.makedirs(os.path.dirname(retrain_ml._TRADING_SAMPLES), exist_ok=True)
        if os.path.exists(retrain_ml._TRADING_SAMPLES):
            os.remove(retrain_ml._TRADING_SAMPLES)
        # write a last-train time in the future => no new samples after it
        with open(retrain_ml.LAST_TRAIN_FILE, "w") as f:
            f.write(str(time.time() + 3600))
        self.assertFalse(retrain_ml._samples_since_last_train())


if __name__ == "__main__":
    unittest.main()