"""
Feature engineering — v3.

State vector S_t  (STATE_DIM = 12)
───────────────────────────────────
  [0]   grade_norm      — avg_gradient / 30, clamped [-1, 1]
  [1]   dist_pct        — distance_km / total_km              ← was part of fatigue scalar
  [2]   hr_frac         — (avg_hr / max_hr − 0.60) / 0.40    ← separated out
  [3]   pace_stress     — max(0, actual_pace−target) / target ← separated out
  [4]   cadence_delta   — (actual − target_cadence) / target, clamped [−0.3, 0.3]
  [5]   km_remaining    — 1.0 at start → 0.0 at finish
  [6-10] seg_type onehot — flat, moderate, steep, wall, descent

The three fatigue components are now separate dimensions so the model can
learn their individual weights from athlete data rather than inheriting
hand-tuned coefficients (0.5 / 0.3 / 0.2).

Audio action vector A_t  (ACTION_DIM = 4)
──────────────────────────────────────────
  [0]  tempo_norm   — BPM / 200
  [1]  energy       — 0–1
  [2]  valence      — 0–1
  [3]  danceability — 0–1

Reward  r ∈ [−1, 1]
────────────────────
  1 − |pace_deviation| − hr_variability / 10   (clipped)
"""

from __future__ import annotations

from typing import Optional

import numpy as np

from data.models import AudioFeatures, CourseSegment, PlayedSegment, SegmentType

TARGET_CADENCE = 180   # spm; personalised in PlaylistGenerator from Strava cadence data
MAX_HR_DEFAULT = 190   # bpm; override via athlete profile (≈ 220 − age)

SEG_ORDER = [
    SegmentType.FLAT, SegmentType.MODERATE, SegmentType.STEEP,
    SegmentType.WALL, SegmentType.DESCENT,
]

STATE_DIM  = 12   # grade(1) + dist_pct(1) + hr_frac(1) + pace_stress(1)
              #         + cadence_delta(1) + km_remaining(1) + onehot(5) + bias(1 — implicit)
ACTION_DIM = 4    # tempo_n, energy, valence, danceability


# ─────────────────────────────────────────────
# Individual fatigue components (separated)
# ─────────────────────────────────────────────

def _dist_pct(distance_km: float, total_km: float) -> float:
    """Fraction of total distance completed, [0, 1]."""
    return float(np.clip(distance_km / max(total_km, 0.01), 0.0, 1.0))


def _hr_frac(avg_hr: Optional[float], max_hr: int = MAX_HR_DEFAULT) -> float:
    """
    Normalised HR above aerobic threshold, [0, 1].
    0 = at or below 60% max HR (easy);  1 = at or above max HR (redline).
    """
    if avg_hr is None:
        return 0.0
    return float(np.clip((avg_hr / max_hr - 0.60) / 0.40, 0.0, 1.0))


def _pace_stress(actual_pace: float, target_pace: float) -> float:
    """
    Positive when athlete is running slower than target, [0, 1].
    0 = on or ahead of pace;  1 = 100% behind pace (wildly slow).
    """
    if target_pace <= 0:
        return 0.0
    stress = (actual_pace - target_pace) / target_pace
    return float(np.clip(stress, 0.0, 1.0))


def cadence_delta(actual_cadence: Optional[float], target: int = TARGET_CADENCE) -> float:
    """Normalised cadence deviation from target, clamped [−0.3, 0.3]."""
    if actual_cadence is None:
        return 0.0
    return float(np.clip((actual_cadence - target) / target, -0.3, 0.3))


def _seg_onehot(seg_type: SegmentType) -> list[float]:
    return [1.0 if seg_type == s else 0.0 for s in SEG_ORDER]


# ─────────────────────────────────────────────
# Build training matrix from PlayedSegments
# ─────────────────────────────────────────────

def build_training_matrix(
    segments:         list[PlayedSegment],
    athlete_avg_pace: float,
    total_course_km:  float,
    target_cadence:   int = TARGET_CADENCE,
    max_hr:           int = MAX_HR_DEFAULT,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """
    Returns (X, y, rewards):
      X       — (N, STATE_DIM)  state vectors
      y       — (N, ACTION_DIM) audio action vectors
      rewards — (N,)            reward in [−1, 1]
    """
    X_rows: list[list[float]] = []
    y_rows: list[list[float]] = []
    r_rows: list[float]       = []

    for seg in segments:
        f = seg.track.features
        if f is None:
            continue

        dp  = _dist_pct(seg.distance_start_km, total_course_km)
        hrf = _hr_frac(seg.avg_hr, max_hr)
        ps  = _pace_stress(seg.avg_pace, athlete_avg_pace)
        cd  = cadence_delta(seg.avg_cadence, target_cadence)
        krp = max(0.0, 1.0 - seg.distance_start_km / max(total_course_km, 0.01))

        x = [
            float(np.clip(seg.avg_gradient / 30.0, -1.0, 1.0)),
            dp,
            hrf,
            ps,
            cd,
            krp,
            *_seg_onehot(seg.segment_type),
        ]
        y = [
            f.tempo / 200.0,
            f.energy,
            f.valence,
            f.danceability,
        ]

        pace_dev = abs(seg.pace_deviation or 0.0)
        hr_var   = (seg.hr_variability or 0.0) / 10.0
        reward   = float(np.clip(1.0 - pace_dev - hr_var, -1.0, 1.0))

        X_rows.append(x)
        y_rows.append(y)
        r_rows.append(reward)

    if not X_rows:
        return (
            np.empty((0, STATE_DIM), dtype=np.float32),
            np.empty((0, ACTION_DIM), dtype=np.float32),
            np.empty(0, dtype=np.float32),
        )

    return (
        np.array(X_rows, dtype=np.float32),
        np.array(y_rows, dtype=np.float32),
        np.array(r_rows, dtype=np.float32),
    )


# ─────────────────────────────────────────────
# Query vector for planned segments (at race time)
# ─────────────────────────────────────────────

def build_query_vector(
    segment:          CourseSegment,
    km_remaining_pct: float,
    athlete_avg_pace: float,
    elapsed_km:       float = 0.0,
    total_km:         float = 42.195,
    avg_hr_frac:      float = 0.75,   # assumed HR as fraction of max, pre-race default
    target_cadence:   int   = TARGET_CADENCE,
    max_hr:           int   = MAX_HR_DEFAULT,
) -> np.ndarray:
    """
    Build the 12-D state vector for a planned course segment.
    HR and cadence are estimated at planning time (no live sensor data yet).
    """
    pace = segment.target_pace or athlete_avg_pace
    dp   = _dist_pct(elapsed_km, total_km)
    ps   = _pace_stress(pace, athlete_avg_pace)   # planned pace stress

    # At planning time, avg_hr_frac is the expected HR at elapsed_km
    # (linearly rising from 0.65 at start to 0.90 at finish)
    estimated_hr_frac = 0.65 + dp * 0.25   # 0.65 → 0.90 over the race
    hrf = float(np.clip((estimated_hr_frac - 0.60) / 0.40, 0.0, 1.0))

    x = [
        float(np.clip(segment.avg_gradient / 30.0, -1.0, 1.0)),
        dp,
        hrf,
        ps,
        0.0,            # cadence_delta unknown at planning time
        km_remaining_pct,
        *_seg_onehot(segment.type),
    ]
    return np.array(x, dtype=np.float32)


# ─────────────────────────────────────────────
# Audio vector helpers
# ─────────────────────────────────────────────

def audio_vec(f: AudioFeatures) -> np.ndarray:
    return np.array([
        f.tempo / 200.0,
        f.energy,
        f.valence,
        f.danceability,
    ], dtype=np.float32)


def denormalize_targets(y: np.ndarray) -> AudioFeatures:
    return AudioFeatures(
        tempo=float(y[0]) * 200.0,
        energy=float(y[1]),
        valence=float(y[2]),
        danceability=float(y[3]),
        acousticness=0.0,
        loudness=-10.0,
        speechiness=0.0,
    )
