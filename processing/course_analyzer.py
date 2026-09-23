"""
Course analyzer — v3.

Changes from v2:
  - 200 m rolling grade smoother applied before pre-cue detection
    (eliminates GPS noise that triggered false inflections on 50 m raw chunks)
  - Offline cache carries a 'version' key; stale or schema-mismatched
    caches are detected and a warning is printed at load time

Hill pre-cue
────────────
A grade inflection is defined as: the rolling 200 m average grade rising
≥ INFLECTION_THRESH % above the preceding stretch.  When one is detected,
a "pre-cue marker" is inserted PRE_CUE_SECONDS of running time before the
inflection point so the high-energy track swap arrives as the slope begins.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Optional

import numpy as np

from data.models import CourseSegment, GeoPoint, SegmentType

CACHE_VERSION = 3   # bump whenever the segment schema changes

# Effort multipliers by terrain (unchanged from v1)
EFFORT_FACTORS: dict[SegmentType, float] = {
    SegmentType.FLAT:     1.00,
    SegmentType.MODERATE: 1.10,
    SegmentType.STEEP:    1.22,
    SegmentType.WALL:     1.35,
    SegmentType.DESCENT:  0.95,
}

# Hill pre-cue
PRE_CUE_SECONDS   = 15       # seconds before grade onset to start the energising track
INFLECTION_THRESH = 2.0      # grade must rise by ≥ 2 % to count as a hill onset


def _classify(grad: float) -> SegmentType:
    if grad > 10:  return SegmentType.WALL
    if grad > 5:   return SegmentType.STEEP
    if grad > 2:   return SegmentType.MODERATE
    if grad < -1:  return SegmentType.DESCENT
    return SegmentType.FLAT


# ─────────────────────────────────────────────
# Segmentation at 50 m resolution
# ─────────────────────────────────────────────

def segment_course_50m(
    points: list[GeoPoint],
) -> list[CourseSegment]:
    """
    Segment the course into 50-metre chunks and classify each by gradient.
    Adjacent chunks of the same type are merged (as before).
    """
    if not points:
        return []

    chunk_km  = 0.050    # 50 m
    total_km  = points[-1].distance
    segments: list[CourseSegment] = []
    km = 0.0

    while km < total_km:
        end_km = min(km + chunk_km, total_km)

        in_window = [p for p in points if km <= p.distance < end_km]
        if not in_window:
            km = end_km
            continue

        elev_start = in_window[0].elevation
        elev_end   = in_window[-1].elevation
        dist_m     = (end_km - km) * 1000
        gradient   = ((elev_end - elev_start) / dist_m * 100) if dist_m > 5 else 0.0
        avg_elev   = float(np.mean([p.elevation for p in in_window]))

        segments.append(CourseSegment(
            start_km=round(km, 4),
            end_km=round(end_km, 4),
            type=_classify(gradient),
            avg_gradient=round(gradient, 2),
            avg_elevation=round(avg_elev, 1),
        ))
        km = end_km

    return _merge_segments(segments)


def _merge_segments(segs: list[CourseSegment]) -> list[CourseSegment]:
    if not segs:
        return []
    merged = [segs[0]]
    for s in segs[1:]:
        last = merged[-1]
        if s.type == last.type:
            merged[-1] = CourseSegment(
                start_km=last.start_km,
                end_km=s.end_km,
                type=last.type,
                avg_gradient=round((last.avg_gradient + s.avg_gradient) / 2, 2),
                avg_elevation=round((last.avg_elevation + s.avg_elevation) / 2, 1),
            )
        else:
            merged.append(s)
    return merged


# ─────────────────────────────────────────────
# Pace assignment
# ─────────────────────────────────────────────

def assign_paces(
    segments:         list[CourseSegment],
    goal_finish_min:  float,
    total_km:         float,
) -> list[CourseSegment]:
    weighted_km = sum(
        (s.end_km - s.start_km) * EFFORT_FACTORS.get(s.type, 1.0)
        for s in segments
    )
    flat_pace = goal_finish_min / max(weighted_km, 0.01)

    return [
        CourseSegment(
            start_km=s.start_km, end_km=s.end_km, type=s.type,
            avg_gradient=s.avg_gradient, avg_elevation=s.avg_elevation,
            target_pace=round(flat_pace * EFFORT_FACTORS.get(s.type, 1.0), 3),
        )
        for s in segments
    ]


# ─────────────────────────────────────────────
# Hill pre-cue engine
# ─────────────────────────────────────────────

def _smooth_gradients(segments: list[CourseSegment], window_m: float = 200.0) -> list[float]:
    """
    Compute a rolling mean gradient using a ~200 m window over the segment list.
    This suppresses GPS noise that causes 50 m chunks to spike ±4–8 % spuriously.

    Returns one smoothed gradient value per segment (same length as input).
    """
    # Build a kilometre-based list of (midpoint_km, gradient) pairs
    mids   = [(s.start_km + s.end_km) / 2 for s in segments]
    grads  = [s.avg_gradient for s in segments]
    window = window_m / 1000.0   # km

    smoothed: list[float] = []
    for i, mid in enumerate(mids):
        # Collect all segments whose midpoint is within ±half-window of this one
        in_window = [
            grads[j] for j, m in enumerate(mids)
            if abs(m - mid) <= window / 2
        ]
        smoothed.append(float(np.mean(in_window)) if in_window else grads[i])
    return smoothed


def compute_precue_markers(
    segments: list[CourseSegment],
) -> list[float]:
    """
    Returns a list of km positions where a "pre-cue" track should start —
    i.e. positions PRE_CUE_SECONDS of running time before a grade inflection.

    A grade inflection = the smoothed 200 m gradient rising ≥ INFLECTION_THRESH %
    AND the current smoothed gradient exceeding 2 % (an actual climb, not noise).
    """
    if not segments:
        return []

    smoothed  = _smooth_gradients(segments)
    markers: list[float] = []
    prev_grad = 0.0

    for i, seg in enumerate(segments):
        sg         = smoothed[i]
        grade_rise = sg - prev_grad
        is_uphill_onset = (
            sg > 2.0                    # genuine uphill (post-smoothing)
            and grade_rise >= INFLECTION_THRESH
        )

        if is_uphill_onset:
            pace       = seg.target_pace or 6.0
            pre_cue_km = (PRE_CUE_SECONDS / 60.0) / pace   # km run in PRE_CUE_SECONDS
            marker_km  = max(0.0, seg.start_km - pre_cue_km)
            markers.append(round(marker_km, 3))

        prev_grad = sg

    return sorted(set(markers))


# ─────────────────────────────────────────────
# Offline cache
# ─────────────────────────────────────────────

def save_course_cache(
    segments:   list[CourseSegment],
    pre_cues:   list[float],
    cache_path: Path,
) -> None:
    """
    Serialise the full course profile to JSON for race-day offline use.
    No network required to load this file; all elevation and pre-cue data
    is embedded.  Version key allows stale-cache detection at load time.
    """
    payload = {
        "version":   CACHE_VERSION,
        "saved_at":  int(time.time()),
        "segments": [
            {
                "start_km":      s.start_km,
                "end_km":        s.end_km,
                "type":          s.type.value,
                "avg_gradient":  s.avg_gradient,
                "avg_elevation": s.avg_elevation,
                "target_pace":   s.target_pace,
            }
            for s in segments
        ],
        "pre_cue_markers_km": pre_cues,
    }
    cache_path.write_text(json.dumps(payload, indent=2))
    print(f"[course] Profile cached → {cache_path}  "
          f"({len(segments)} segments, {len(pre_cues)} pre-cues, version {CACHE_VERSION})")


def load_course_cache(cache_path: Path) -> tuple[list[CourseSegment], list[float]]:
    d = json.loads(cache_path.read_text())

    # Version check
    cached_ver = d.get("version", 1)
    if cached_ver != CACHE_VERSION:
        print(f"[course] ⚠  Cache version mismatch (file={cached_ver}, current={CACHE_VERSION}). "
              f"Re-run without --no-cache to regenerate.")

    # Age check (warn if > 30 days)
    saved_at = d.get("saved_at")
    if saved_at:
        age_days = (time.time() - saved_at) / 86400
        if age_days > 30:
            print(f"[course] ⚠  Cache is {age_days:.0f} days old. "
                  f"Re-run with --no-cache if the course has changed.")

    segments = [
        CourseSegment(
            start_km=r["start_km"], end_km=r["end_km"],
            type=SegmentType(r["type"]),
            avg_gradient=r["avg_gradient"], avg_elevation=r["avg_elevation"],
            target_pace=r.get("target_pace"),
        )
        for r in d["segments"]
    ]
    return segments, d.get("pre_cue_markers_km", [])


# ─────────────────────────────────────────────
# Reporting
# ─────────────────────────────────────────────

def segment_duration_min(segment: CourseSegment) -> float:
    if segment.target_pace is None:
        return 0.0
    return (segment.end_km - segment.start_km) * segment.target_pace


def compute_km_remaining(segment: CourseSegment, total_km: float) -> float:
    midpoint = (segment.start_km + segment.end_km) / 2
    return max(0.0, 1.0 - midpoint / total_km)


def course_summary(segments: list[CourseSegment], total_km: float,
                   pre_cues: Optional[list[float]] = None) -> str:
    lines = [f"{'km':>11}  {'type':<10}  {'grad':>6}  {'pace':>8}"]
    lines.append("─" * 45)
    for s in segments:
        pace_str = f"{s.target_pace:.2f}/km" if s.target_pace else "  ?.??"
        marker   = "  ⚡pre-cue" if pre_cues and any(
            abs(m - s.start_km) < (s.end_km - s.start_km)
            for m in pre_cues
        ) else ""
        lines.append(
            f"{s.start_km:>5.2f}–{s.end_km:<5.2f}  "
            f"{s.type.value:<10}  "
            f"{s.avg_gradient:>+5.1f}%  "
            f"{pace_str:>8}{marker}"
        )

    gain = sum(
        max(0, s.avg_gradient * (s.end_km - s.start_km) * 10)
        for s in segments
    )
    lines += ["", f"Distance : {total_km:.2f} km", f"Elev gain: ~{gain:.0f} m"]
    if pre_cues:
        lines.append(f"Pre-cues : {len(pre_cues)} hill onsets marked")
    return "\n".join(lines)
