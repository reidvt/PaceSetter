"""
Contextual preference model — GBM regression (replaces LinUCB).

Architecture
────────────
We want: given a run context x (grade, fatigue components, cadence, km
remaining, terrain type), predict the audio feature vector y (tempo, energy,
valence, danceability) that the athlete rewards most.

This is an offline supervised regression problem, not a sequential bandit
problem. GradientBoostingRegressor is the right tool:

  • Non-linear — hills and fatigue interact in ways linear models miss.
  • Sample-weighted — high-reward segments are emphasised in training.
  • No UCB overhead — exploration happens via the playlist generator's
    top-5 stochastic sampling, which is where it should live.
  • Interpretable — feature_importances_ is available for debugging.

One GBR is fitted per audio-feature output dimension (tempo, energy, valence,
danceability) so sample_weight is passed correctly to each estimator.

Two-stage fallback
──────────────────
  Cold (<20 samples): inverse-distance k-NN in audio space.
  Warm / Hot (20+):   per-dimension GBR (100 estimators, depth 3).

Public API (same as the old LinUCB model — no callers need to change)
──────────────────────────────────────────────────────────────────────
  model.update(x, audio_vec, reward, track_id)
  model.score_track(x, audio_vec, track_id)
  model.predict_target(x)
  model.fit_batch(X, y, rewards)
  model.save() / PreferenceModel.load()
"""

from __future__ import annotations

import pickle
from pathlib import Path
from typing import Any, Optional

import numpy as np

from processing.features import ACTION_DIM, STATE_DIM

MODEL_PATH  = Path("pacesetter_model.pkl")
COLD_THRESH = 20


class PreferenceModel:

    def __init__(self) -> None:
        self._n:          int                     = 0
        self._X:          Optional[np.ndarray]    = None
        self._y:          Optional[np.ndarray]    = None
        self._r:          Optional[np.ndarray]    = None
        # Per-output-dimension GBR estimators (one per audio feature)
        self._estimators: list[Any]               = []
        # Online-update buffer (accumulated between explicit retrains)
        self._buf_X:      list[np.ndarray]        = []
        self._buf_y:      list[np.ndarray]        = []
        self._buf_r:      list[float]             = []

    # ─────────────────────────────────────────────
    # Training
    # ─────────────────────────────────────────────

    def fit_batch(
        self,
        X:       np.ndarray,   # (N, STATE_DIM)
        y:       np.ndarray,   # (N, ACTION_DIM)
        rewards: np.ndarray,   # (N,)  in [-1, 1]
    ) -> None:
        """Batch (re)train from aligned-segment data."""
        self._X = X.copy()
        self._y = y.copy()
        self._r = rewards.copy()
        self._n = len(X)

        if self._n < COLD_THRESH:
            print(f"[model] {self._n} samples — cold start (k-NN only, need {COLD_THRESH}+)")
            self._estimators = []
            return

        # Map rewards from [-1, 1] → sample weights in (0, 2]
        # Low or negative reward = low weight, not excluded entirely
        weights = np.clip(rewards + 1.0, 0.05, 2.0)

        try:
            from sklearn.ensemble import GradientBoostingRegressor
        except ImportError:
            raise ImportError(
                "scikit-learn required: pip install scikit-learn"
            )

        self._estimators = []
        for col in range(ACTION_DIM):
            gbr = GradientBoostingRegressor(
                n_estimators=100,
                max_depth=3,
                learning_rate=0.1,
                subsample=0.8,
                min_samples_leaf=3,
                random_state=42,
            )
            gbr.fit(X, y[:, col], sample_weight=weights)
            self._estimators.append(gbr)

        mode = "hot" if self._n >= 50 else "warm"
        print(f"[model] GBM fitted on {self._n} samples (mode: {mode})")

    def update(
        self,
        x:         np.ndarray,
        audio_vec: np.ndarray,
        reward:    float,
        track_id:  str = "",
    ) -> None:
        """
        Buffer a single online observation.
        Call fit_batch() periodically to incorporate buffered data.
        The buffer is flushed and included on the next fit_batch() call.
        """
        self._buf_X.append(x.copy())
        self._buf_y.append(audio_vec.copy())
        self._buf_r.append(float(reward))
        self._n += 1

    def flush_buffer_and_refit(self) -> None:
        """Merge buffered online updates into the training set and refit."""
        if not self._buf_X:
            return
        new_X = np.array(self._buf_X, dtype=np.float32)
        new_y = np.array(self._buf_y, dtype=np.float32)
        new_r = np.array(self._buf_r, dtype=np.float32)

        X = np.vstack([self._X, new_X]) if self._X is not None else new_X
        y = np.vstack([self._y, new_y]) if self._y is not None else new_y
        r = np.concatenate([self._r, new_r]) if self._r is not None else new_r

        self._buf_X.clear()
        self._buf_y.clear()
        self._buf_r.clear()
        self.fit_batch(X, y, r)

    # ─────────────────────────────────────────────
    # Scoring / prediction
    # ─────────────────────────────────────────────

    def predict_target(self, x: np.ndarray) -> np.ndarray:
        """
        Return the audio feature vector predicted to maximise reward for
        context x. Used by playlist_generator to search the library.
        """
        if not self._estimators:
            # Cold: return training mean or sensible marathon defaults
            if self._y is not None and len(self._y) > 0:
                return self._y.mean(axis=0).astype(np.float32)
            return np.array([0.70, 0.75, 0.60, 0.65], dtype=np.float32)

        pred = np.array(
            [est.predict(x.reshape(1, -1))[0] for est in self._estimators],
            dtype=np.float32,
        )
        return np.clip(pred, 0.0, 1.0)

    def score_track(
        self,
        x:         np.ndarray,
        audio_vec: np.ndarray,
        track_id:  str = "",
    ) -> float:
        """
        Return a [0, 1] quality score for playing this track in context x.

        Score = 1 − (L2 distance between audio_vec and predicted target) /
                     sqrt(ACTION_DIM)

        Cold mode falls back to k-NN proximity in audio space.
        """
        if not self._estimators or self._n < COLD_THRESH:
            return self._knn_proximity(audio_vec)

        target   = self.predict_target(x)
        dist     = float(np.linalg.norm(audio_vec - target))
        max_dist = float(np.sqrt(ACTION_DIM))   # distance when vectors are opposite corners
        return float(np.clip(1.0 - dist / (max_dist + 1e-6), 0.0, 1.0))

    def _knn_proximity(self, audio_vec: np.ndarray, k: int = 10) -> float:
        """Inverse-distance proximity to k nearest training audio vectors."""
        if self._y is None or len(self._y) == 0:
            return 0.5
        dists = np.linalg.norm(self._y - audio_vec, axis=1)
        top_k = np.sort(dists)[:k]
        max_d = float(np.sqrt(ACTION_DIM))
        return float(np.clip(1.0 - np.mean(top_k) / (max_d + 1e-6), 0.0, 1.0))

    # ─────────────────────────────────────────────
    # Diagnostics
    # ─────────────────────────────────────────────

    @property
    def n_samples(self) -> int:
        return self._n

    @property
    def is_fitted(self) -> bool:
        return bool(self._estimators)

    def feature_importances(self) -> Optional[np.ndarray]:
        """
        Return mean feature importance across output dimensions.
        Shape: (STATE_DIM,). Returns None when in cold mode.
        """
        if not self._estimators:
            return None
        imps = np.array([e.feature_importances_ for e in self._estimators])
        return imps.mean(axis=0)

    # ─────────────────────────────────────────────
    # Persistence
    # ─────────────────────────────────────────────

    def save(self, path: Path = MODEL_PATH) -> None:
        payload = {
            "n":          self._n,
            "X":          self._X,
            "y":          self._y,
            "r":          self._r,
            "estimators": self._estimators,
        }
        with open(path, "wb") as f:
            pickle.dump(payload, f, protocol=5)
        print(f"[model] Saved → {path}  ({self._n} samples, "
              f"{'fitted' if self._estimators else 'cold'})")

    @classmethod
    def load(cls, path: Path = MODEL_PATH) -> "PreferenceModel":
        m = cls()
        if not path.exists():
            print("[model] No saved model — starting fresh (cold mode).")
            return m
        with open(path, "rb") as f:
            d = pickle.load(f)
        m._n          = d["n"]
        m._X          = d.get("X")
        m._y          = d.get("y")
        m._r          = d.get("r")
        m._estimators = d.get("estimators") or []
        mode = "cold" if not m._estimators else ("hot" if m._n >= 50 else "warm")
        print(f"[model] Loaded — {m._n} samples, mode={mode}")
        return m
