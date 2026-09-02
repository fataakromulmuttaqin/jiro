#!/usr/bin/env python3
"""
retrain_ml.py — retrain Jiro's ML/ANN pump-probability filter from accumulated
real-trade samples (the JSONL written by trading._append_ml_sample on every
trade close) plus the historic pump.fun proxy set.

Usage (meant for a scheduled cron):
    ./venv/bin/python retrain_ml.py [--force]

Behavior:
- Reads real-trade samples from models/ml_training_samples.jsonl (feature
  rows + win/loss labels from closed trades).
- Always also draws the historical proxy samples from pump.fun (ASC sort) to
  keep the model grounded when there aren't many real trades yet.
- Trains ONLY if real-trade samples were added since the last train, OR
  --force is passed (so we don't hammer re-training every run for no gain).
- On success prints a short summary; exits 0. On failure prints error and
  exits 1 (so a cron/supervisor can alert).
- Never raises for a data absence: with no samples it prints a notice and
  exits 0.
"""

import os
import sys
import json
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ml_filter

HERE = os.path.dirname(os.path.abspath(__file__))
SAMPLES_FILE = ml_filter.MODEL_FILE.replace(".npz", "_samples.jsonl")
# preferred matching path used by trading.py
_TRADING_SAMPLES = os.path.join(HERE, "models", "ml_training_samples.jsonl")
LAST_TRAIN_FILE = os.path.join(HERE, "models", "ml_last_train.txt")


def _load_real_samples():
    """Return (X, y) from the JSONL of real trades. Path = the one trading.py
    writes to; fall back to the other if present."""
    path = None
    for cand in (_TRADING_SAMPLES, SAMPLES_FILE):
        if os.path.exists(cand):
            path = cand
            break
    if not path:
        return None, None, 0
    X, y = [], []
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                d = json.loads(line)
                fv = d.get("features")
                lab = d.get("label")
                if isinstance(fv, list) and lab is not None:
                    X.append(fv)
                    y.append(float(lab))
    except Exception as e:
        print(f"[retrain_ml] error reading samples: {e}", file=sys.stderr)
        return None, None, 0
    return X, y, len(X)


def _samples_since_last_train():
    """Determine whether new real-trade samples were added since last train."""
    try:
        if not os.path.exists(_TRADING_SAMPLES):
            return False
        mtime = os.path.getmtime(_TRADING_SAMPLES)
        if not os.path.exists(LAST_TRAIN_FILE):
            return True
        with open(LAST_TRAIN_FILE) as f:
            last = float(f.read().strip())
        return mtime > last + 1  # file modified after last train
    except Exception:
        return False


def main():
    force = "--force" in sys.argv

    # 1) real trades
    Xr, yr, n_real = _load_real_samples()
    real_xy = (Xr, yr) if Xr and len(Xr) >= 2 else (None, None)

    # 2) decide whether to train BEFORE fetching historic samples (cheap skip,
    #    so a quiet period doesn't hammer the historic fetch every 30 min)
    if not force and not _samples_since_last_train():
        print("[retrain_ml] no new real-trade samples since last train, skipping "
              "(use --force to retrain anyway).")
        sys.exit(0)

    # 3) historic proxy (grounding)
    print("collecting historic proxy samples...")
    Xh, yh = ml_filter.collect_historic_samples(max_n=ml_filter.MAX_TRAIN_SAMPLES)
    print(f"  historic: {len(Xh)} samples")
    if len(Xh) == 0:
        print("[retrain_ml] no historic samples; skipping.")
        sys.exit(0)

    # combine real + historic (historic dominates until real volume grows)
    import numpy as np
    if real_xy[0] is not None:
        X = np.vstack([np.asarray(Xh, dtype=np.float32),
                       np.asarray(real_xy[0], dtype=np.float32)])
        y = np.hstack([np.asarray(yh, dtype=float),
                       np.asarray(real_xy[1], dtype=float)])
    else:
        X, y = np.asarray(Xh, dtype=np.float32), np.asarray(yh, dtype=float)

    res = ml_filter.train(X, y)
    res["real_samples"] = n_real
    print("[retrain_ml] trained:", json.dumps(res))

    # record training timestamp
    try:
        os.makedirs(os.path.dirname(LAST_TRAIN_FILE), exist_ok=True)
        with open(LAST_TRAIN_FILE, "w") as f:
            f.write(str(time.time()))
    except Exception as e:
        print(f"[retrain_ml] warn writing last-train stamp: {e}", file=sys.stderr)

    sys.exit(0)


if __name__ == "__main__":
    main()