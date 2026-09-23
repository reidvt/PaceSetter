"""
Workout ↔ Music alignment — v2.

Changes from v1:
  - Computes avg_cadence per played window (both-feet spm from Strava)
  - Computes pace_deviation = actual_pace - target_pace (signed, min/km)
  - Computes hr_variability = std(HR) during window — feeds bandit reward
  - Handles Spotify's played_at semantics: it marks when a track ENDED,
    so play_start = played_at − duration_ms.
"""

from __future__ import annotations

import bisect
from datetime import datetime, timedelta
from typing import Optional

import numpy as np

from data.models import PlayedSegment, SegmentType, Track, Workout, WorkoutSample


def _classify_gradient(grad: float) -> SegmentType:
    if grad > 10:    return SegmentType.WALL
    if grad > 5:     return SegmentType.STEEP
    if grad > 2:     return SegmentType.MODERATE
    if grad < -1:    return SegmentType.DESCENT
    return SegmentType.FLAT


def _samples_in_window(
    samples: list[WorkoutSample],
    start:   datetime,
    end:     datetime,
) -> list[WorkoutSample]:
    if not samples:
        return []
    timestamps = [s.timestamp for s in samples]
    lo = bisect.bisect_left(timestamps, start)
    hi = bisect.bisect_left(timestamps, end)
    return samples[lo:hi]


def _avg(vals: list) -> Optional[float]:
    clean = [v for v in vals if v is not None]
    return float(np.mean(clean)) if clean else None


def _std(vals: list) -> Optional[float]:
    clean = [v for v in vals if v is not None]
    return float(np.std(clean)) if len(clean) > 1 else None


def align_music_to_workouts(
    played_items:  list[dict],
    workouts:      list[Workout],
    tracks_by_id:  dict[str, Track],
) -> list[PlayedSegment]:
    """
    Align Spotify play history to Strava GPS samples.
    Returns PlayedSegment list (only windows with ≥2 Strava samples).
    """
    # Build (start, end, workout) index
    wo_index: list[tuple[datetime, datetime, Workout]] = [
        (wo.start_time, wo.start_time + timedelta(seconds=wo.elapsed_sec), wo)
        for wo in workouts
    ]

    segments: list[PlayedSegment] = []

    for item in played_items:
        raw      = item["track"]
        track_id = raw["id"]
        track    = tracks_by_id.get(track_id)
        if track is None or track.features is None:
            continue

        # played_at = when the track finished (Spotify convention)
        played_at  = _parse_dt(item["played_at"])
        duration   = timedelta(milliseconds=raw["duration_ms"])
        play_start = played_at - duration
        play_end   = played_at

        for wo_start, wo_end, wo in wo_index:
            overlap_start = max(play_start, wo_start)
            overlap_end   = min(play_end,   wo_end)
            if overlap_end <= overlap_start:
                continue

            window = _samples_in_window(wo.samples, overlap_start, overlap_end)
            if len(window) < 2:
                continue

            paces    = [s.instant_pace for s in window if s.instant_pace < 20]
            hrs      = [s.heart_rate for s in window]
            grads    = [s.gradient for s in window]
            cadences = [s.cadence for s in window]

            avg_pace    = _avg(paces) or wo.avg_pace
            avg_hr      = _avg(hrs)
            avg_grad    = _avg(grads) or 0.0
            avg_cadence = _avg([c for c in cadences if c])
            hr_var      = _std(hrs)

            # pace_deviation = actual_pace − target_pace
            # Use workout average as proxy for target when no plan exists
            pace_dev = avg_pace - wo.avg_pace

            seg = PlayedSegment(
                track=track,
                workout_id=wo.id,
                started_at=overlap_start,
                distance_start_km=window[0].distance,
                distance_end_km=window[-1].distance,
                avg_pace=avg_pace,
                avg_hr=avg_hr,
                avg_gradient=round(avg_grad, 2),
                segment_type=_classify_gradient(avg_grad),
                avg_cadence=avg_cadence,
                pace_deviation=round(pace_dev, 3),
                hr_variability=round(hr_var, 2) if hr_var else None,
            )
            segments.append(seg)

    return segments


def _parse_dt(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))
