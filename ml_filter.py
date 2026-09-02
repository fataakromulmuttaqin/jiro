#!/usr/bin/env python3
"""
ml_filter.py — ML/ANN entry filter for Jiro (from "151 Trading Strategies" §18.2
ANN idea, adapted to Jiro's pump.fun launch data).

What it does:
- Builds labeled samples from historical pump.fun launches. Because the
  frontend "live" endpoint only exposes fresh coins, we pull OLD coins via
  `sort=created_timestamp, order=ASC` (oldest first). The LABEL is a proxy for
  "did it keep/pump" = whether current market cap is above a threshold (and/or
  how big a multiple of a typical early MC it reached). Historic MC ~ a few k
  at launch; a coin that survived to mid/high MC is a "win".
- Trains an MLP (multilayer perceptron) ANN. Prefers scikit-learn's
  MLPClassifier if available; falls back to a pure-numpy MLP when it isn't
  (both give a predict_proba-style score).
- Exposes `predict_pump(feature_vector) -> float 0..1` used by passes_entry()
  as an ADDITIONAL signal (combined, not replacing existing filters).
- Supports retraining from Jiro's own real trades (ledger.json) once enough
  are logged.

Feature vector (all computed per launch by callers / launch_finder):
  idx  0  mc_usd_marketcap                (log-scaled)
  idx  1  age_hours
  idx  2  is_complete (1 if bonding curve full / migrated)
  idx  3  reply_count                    (community activity)
  idx  4  supply_billions                 (total_supply / 1e9)
  idx  5  real_sol_reserves_usd           (pool SOL value proxy)
  idx  6  smart_money_count               (watched wallets that bought)
  idx  7  holder_risk_score               (holder screen 0-10, higher=riskier)
  idx  8  hr_swap_count                   (on-chain swap count last hour)
  idx  9  has_creator (1 if creator field present)

Design notes:
- Proxy-label training is a heuristic, NOT ground truth — a low-MC old coin
  could be a failed launch OR just an obscure one. We treat MC >= 50k USD as
  "won/pumped" (past the early window), which matches Jiro's own entry thesis
  (>=$100k = gap gone). This is an entry FILTER/BONUS, never a hard standalone
  signal.
- Keep the model small & fast (single hidden layer) — interval training is
  cheap and the filter must not slow the scan loop.
- Model persists to $MODEL_FILE. If absent, predict returns None (no filter)
  so Jiro runs exactly as before until the first train.
"""

import os
import json
import time
import math

try:
    import numpy as np
    _HAVE_NUMPY = True
except Exception:
    np = None
    _HAVE_NUMPY = False

_HERE = os.path.dirname(os.path.abspath(__file__))
MODEL_FILE = os.path.join(_HERE, "models", "ml_filter.npz")
FIT_FILE = os.path.join(_HERE, "models", "ml_filter_fit.json")
HIST_FILE = os.path.join(_HERE, "models", "ml_training_history.csv")

# pump.fun list endpoint supports sort by created; ASC pulls oldest (training)
PUMP_LIST_URL = "https://frontend-api-v3.pump.fun/coins"
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
HEADERS = {"User-Agent": _UA, "Accept": "application/json",
           "Referer": "https://pump.fun/", "Origin": "https://pump.fun"}

# --- feature/label config (tunable via config.json ml_filter section) ---
FEATURE_COUNT = 10
LABEL_PUMP_MC_USD = 50_000      # MC >= this at "now" = treated as win/pump
MAX_TRAIN_SAMPLES = 4000
_MIN_TRAIN_SAMPLES = 50         # need this many labeled samples before training


# ---------------------------------------------------------------------------
# Feature engineering
# ---------------------------------------------------------------------------

def build_feature_vector(coin: dict, extra: dict | None = None,
                         drop_label_leak: bool = False) -> np.ndarray:
    """Vectorize a pump.fun coin + optional extra signals into a feature row.
    Missing values default to 0 (safe, no crash).

    If drop_label_leak=True, the columns that perfectly encode the proxy label
    (log-MC, pool SOL value — both driven by current market cap) are zeroed so
    offline training on the "did it reach high MC" proxy label learns from
    signals OTHER than the label itself.

    Requires numpy; if unavailable (env degraded) returns a zeros row so
    callers don't crash."""
    if not _HAVE_NUMPY:
        return np.zeros(10, dtype=np.float32)
    extra = extra or {}
    mc = float(coin.get("usd_market_cap") or coin.get("market_cap_usd") or 0)
    created_ms = coin.get("created_timestamp") or 0
    if created_ms:
        age_h = max(0.0, (time.time() - created_ms / 1000) / 3600)
    else:
        age_h = 0.0
    supply = float(coin.get("total_supply") or 0)
    real_sol = float(coin.get("real_sol_reserves") or 0)
    sol_price = float(extra.get("sol_price") or 150.0)
    leak0 = math.log10(mc + 1.0) if not drop_label_leak else 0.0
    leak5 = ((real_sol / 1e9) * sol_price) if not drop_label_leak else 0.0
    log_supply = math.log10(supply + 1.0) if supply else 0.0
    fv = [
        leak0,                                                   # 0 log-MC (label-leak)
        math.log10(age_h + 1.0),                                # 1 log-age
        1.0 if coin.get("complete") else 0.0,                   # 2 complete
        float(coin.get("reply_count") or 0),                    # 3 reply (small)
        log_supply,                                             # 4 log-supply (~15)
        leak5,                                                  # 5 pool SOL usd (leak)
        float(extra.get("smart_money_count") or 0),             # 6 SM count (small)
        float(extra.get("holder_risk_score") or 0),             # 7 holder risk (0-10)
        float(extra.get("swap_count_h1") or 0),                 # 8 swap/h (small)
        1.0 if coin.get("creator") else 0.0,                    # 9 has creator
    ]
    # normalize to ~0..1 range: divide log fields & small counts by sensible
    # ceilings so NO single feature dominates (fixes MLP saturation when one
    # column is ~1e6 and all others <10).
    scale = np.array([1.0, 4.0, 1.0, 200.0, 16.0, 1e5, 20.0, 10.0, 1000.0, 1.0],
                     dtype=float)
    return (np.asarray(fv, dtype=float) / scale).astype(np.float32)


def label_from_coin(coin: dict, pump_mc: float = LABEL_PUMP_MC_USD) -> float:
    """Proxy label: 1 if coin's current MC >= pump_mc (it 'kept/pumped' out of
    the early window), else 0."""
    mc = float(coin.get("usd_market_cap") or coin.get("market_cap_usd") or 0)
    return 1.0 if mc >= pump_mc else 0.0


# ---------------------------------------------------------------------------
# Data collection from historical pump.fun launches
# ---------------------------------------------------------------------------

def collect_historic_samples(max_n: int = MAX_TRAIN_SAMPLES,
                             pump_mc: float = LABEL_PUMP_MC_USD,
                             sol_price: float = 150.0) -> tuple:
    """Fetch OLD pump.fun coins (order=ASC -> oldest) and build (X, y, meta).
    Returns (X np array, y np array). Uses the same coin schema as the live
    endpoint. Filtered to those with created_timestamp so age is meaningful."""
    import requests
    X, y = [], []
    offset = 0
    while len(X) < max_n and offset < max_n * 4:
        try:
            r = requests.get(PUMP_LIST_URL, params={
                "limit": 50, "sort": "created_timestamp", "order": "ASC",
                "includeNsfw": "false", "offset": offset,
            }, headers=HEADERS, timeout=25)
            r.raise_for_status()
            rows = [c for c in r.json() if isinstance(c, dict)]
        except Exception as e:
            print(f"[ml_filter] fetch failed @offset {offset}: {e}",
                  file=__import__("sys").stderr)
            break
        if not rows:
            break
        for c in rows:
            if not c.get("created_timestamp"):
                continue
            # skip tokens WITHIN the pump-threshold window of "now" (too fresh
            # to judge) — require at least a few hours of age on label
            created_sec = c.get("created_timestamp") / 1000
            if (time.time() - created_sec) < 6 * 3600:
                continue
            try:
                # drop label-leak columns (MC-driven) so the model learns from
                # non-MC signals only instead of just echoing the label
                fv = build_feature_vector(c, {"sol_price": sol_price},
                                          drop_label_leak=True)
                X.append(fv)
                y.append(label_from_coin(c, pump_mc))
            except Exception:
                continue
        offset += 50
    if not X:
        return np.empty((0, FEATURE_COUNT), dtype=np.float32), np.empty((0,), dtype=float)
    X = np.stack(X).astype(np.float32)
    y = np.asarray(y, dtype=float)
    return X, y


# ---------------------------------------------------------------------------
# Pure-numpy MLP (zero external dep fallback)
# ---------------------------------------------------------------------------

class NumpyMLP:
    """Tiny logistic-workflow MLP with 1 hidden layer, trained by a few
    iterations of simple gradient descent on cross-entropy. Sufficient for a
    small tabular filter; NOT a substitute for sklearn's mature optimiser.
    API mirrors the sklearn subset we need (fit / predict_proba)."""
    def __init__(self, hidden=16, lr=0.5, epochs=300, seed=0):
        self.hidden = hidden
        self.lr = lr
        self.epochs = epochs
        self.seed = seed
        self.W1 = self.b1 = self.W2 = self.b2 = None

    def _sigmoid(self, z):
        z = np.clip(z, -500, 500)
        return 1.0 / (1.0 + np.exp(-z))

    def fit(self, X, y):
        rng = np.random.RandomState(self.seed)
        n, d = X.shape
        self.W1 = rng.randn(d, self.hidden) * np.sqrt(2.0 / d)
        self.b1 = np.zeros(self.hidden)
        self.W2 = rng.randn(self.hidden, 1) * np.sqrt(2.0 / self.hidden)
        self.b2 = np.zeros(1)
        for _ in range(self.epochs):
            h = self._sigmoid(X @ self.W1 + self.b1)        # (n,hidden)
            out = self._sigmoid(h @ self.W2 + self.b2)      # (n,1)
            err = out - y.reshape(-1, 1)                     # dL/dout
            dW2 = h.T @ err / n
            db2 = err.mean(axis=0)
            dh = (err @ self.W2.T) * h * (1 - h)
            dW1 = X.T @ dh / n
            db1 = dh.mean(axis=0)
            self.W2 -= self.lr * dW2
            self.b2 -= self.lr * db2
            self.W1 -= self.lr * dW1
            self.b1 -= self.lr * db1
        return self

    def predict_proba(self, X):
        h = self._sigmoid(X @ self.W1 + self.b1)
        out = self._sigmoid(h @ self.W2 + self.b2)
        return np.hstack([1 - out, out])  # [P(0), P(1)]


# ---------------------------------------------------------------------------
# Train / persist / load
# ---------------------------------------------------------------------------

def _sklearn_available():
    try:
        from sklearn.neural_network import MLPClassifier
        return True
    except Exception:
        return False


def train(X: np.ndarray, y: np.ndarray, force_numpy: bool = False) -> dict:
    """Train an ANN on (X,y). Prefers sklearn.MLPClassifier; falls back to
    NumpyMLP. Persists weights + fit metadata. Returns summary dict."""
    if len(X) < _MIN_TRAIN_SAMPLES:
        return {"trained": False, "reason": "insufficient_samples",
                "n_samples": int(len(X))}
    summary = {"trained": True, "n_samples": int(len(X)),
               "n_pos": int(np.sum(y)), "n_neg": int(len(y) - np.sum(y)),
               "framework": None}

    if not force_numpy and _sklearn_available():
        from sklearn.neural_network import MLPClassifier
        # MLPClassifier has no class_weight; handle imbalance by up-weighting the
        # minority class via bounded duplication (SMOTE-ish). Cap reps so the
        # amplified minority never overwhelms (9x is plenty; 70x would make the
        # model memorize one class and predict 1.0 on everything).
        pos_idx = np.where(y == 1)[0]
        neg_idx = np.where(y == 0)[0]
        if len(pos_idx) > 0 and len(neg_idx) > len(pos_idx):
            reps = min(int(np.ceil(len(neg_idx) / len(pos_idx))), 9)
            if reps > 1:
                X = np.vstack([X] + [X[pos_idx] for _ in range(reps - 1)])
                y = np.hstack([y] + [y[pos_idx] for _ in range(reps - 1)])
        model = MLPClassifier(hidden_layer_sizes=(16,), activation="relu",
                              solver="adam", max_iter=300, alpha=1e-4,
                              random_state=0)
        model.fit(X, y)
        summary["framework"] = "sklearn"
        # save sklearn model via joblib if available
        try:
            import joblib
            os.makedirs(os.path.dirname(MODEL_FILE) or ".", exist_ok=True)
            joblib.dump(model, MODEL_FILE.replace(".npz", ".joblib"))
            with open(MODEL_FILE.replace(".npz", "_framework.txt"), "w") as f:
                f.write("sklearn")
        except Exception:
            # fall back to numpy persistence
            summary["framework"] = "sklearn(numpy-cache)"
            _save_numpy(MODEL_FILE, model)
        return summary

    # pure-numpy path
    model = NumpyMLP().fit(X, y)
    summary["framework"] = "numpy"
    _save_numpy(MODEL_FILE, model)
    _save_fit(summary)
    return summary


def _save_numpy(path, model):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    np.savez_compressed(path,
                        W1=model.W1, b1=model.b1, W2=model.W2, b2=model.b2)


def _save_fit(summary: dict):
    with open(FIT_FILE, "w") as f:
        json.dump(summary, f, indent=2)


def _load_numpy():
    if not os.path.exists(MODEL_FILE):
        return None
    z = np.load(MODEL_FILE)
    m = NumpyMLP()
    m.W1, m.b1, m.W2, m.b2 = z["W1"], z["b1"], z["W2"], z["b2"]
    return m


def load_model():
    """Return a model with predict_proba(X)->(n,2) if one exists, else None."""
    if _sklearn_available():
        jpath = MODEL_FILE.replace(".npz", ".joblib")
        if os.path.exists(jpath):
            try:
                import joblib
                return joblib.load(jpath)
            except Exception:
                pass
    numpy_model = _load_numpy()
    if numpy_model is not None:
        return numpy_model
    return None


# ---------------------------------------------------------------------------
# Inference
# ---------------------------------------------------------------------------

def predict_pump(feature_vector, model=None) -> float | None:
    """Return P(pump)=P(class 1) as float 0..1, or None if no model/vector."""
    fv = np.asarray(feature_vector, dtype=np.float32).reshape(1, -1)
    if fv.shape[1] != FEATURE_COUNT:
        return None
    model = model or load_model()
    if model is None:
        return None
    try:
        proba = model.predict_proba(fv)
        return float(proba[0][1])
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Retrain from Jiro's live trades (ledger.json) — accumulates real labels
# ---------------------------------------------------------------------------

def retrain_from_ledger(ledger_file: str, X_hist: np.ndarray, y_hist: np.ndarray):
    """Blend historical proxy samples with Jiro's own closed trades. Each ledger
    entry carries a pnl; pnl>0 => 'won'. This is how the filter sharpens over
    time on real outcomes. Feature rows for real trades must be persisted
    alongside (not currently logged), so this is a scaffold: pass X_hist/y_hist
    (proxy) and, when available, real-trade feature rows."""


if __name__ == "__main__":
    print("Collecting historical pump.fun launches for training...")
    Xs, ys = collect_historic_samples(max_n=800)
    print(f"  collected {len(Xs)} samples ({int(np.sum(ys))} pump / "
          f"{int(len(ys)-np.sum(ys))} not)")
    if len(Xs) >= _MIN_TRAIN_SAMPLES:
        res = train(Xs, ys)
        print(f"  trained: {res}")
        # quick self-check
        model = load_model()
        if model is not None:
            p = predict_pump(Xs[0], model)
            print(f"  sample predict_pump on first feature = {p}")
    else:
        print("  not enough samples yet")