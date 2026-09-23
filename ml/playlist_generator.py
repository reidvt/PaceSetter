"""
Playlist generator — v2.

Key changes from v1:
  - No Spotify recommendation API (deprecated)
  - Cadence-BPM locking: scores candidate songs by how well their BPM
    matches the runner's typical cadence (1:1, half-time, double-time)
  - Pre-cue injection: at each hill onset marker, the next song selection
    is forced toward high-energy, high-tempo tracks regardless of model output
  - Track scoring = LinUCB bandit score × cadence fitness × audio proximity
  - Discovery source: filtered subsets of the library the user hasn't heard
    recently (last 5 runs), rather than the deprecated recommendations endpoint
"""

from __future__ import annotations

import random
from typing import Optional

import numpy as np

from data.models import AudioFeatures, CourseSegment, PlaylistSlot, Track
from ml.preference_model import PreferenceModel
from processing.course_analyzer import (
    compute_km_remaining, segment_duration_min,
)
from processing.features import (
    TARGET_CADENCE, audio_vec, build_query_vector, denormalize_targets,
)

MAX_BPM_JUMP   = 25.0    # BPM step allowed between consecutive songs
DISCOVERY_RATIO = 0.20   # fraction of slots from the "less-heard" tier

# Pre-cue override: when within this distance of a hill onset marker,
# force a high-energy, high-tempo pick
PRE_CUE_WINDOW_KM = 0.15

# BPM locking tolerance: ratio within which a tempo is considered "matched"
BPM_LOCK_TOLERANCE = 0.06   # ±6 % of target BPM


def _cadence_fitness(
    track_bpm:        float,
    target_cadence:   int = TARGET_CADENCE,
    tolerance:        float = BPM_LOCK_TOLERANCE,
) -> float:
    """
    Score 0-1 for how well track_bpm matches running cadence.
    Checks 1:1, half-time (cadence/2), and double-time (cadence×2) relationships.
    """
    targets = [
        target_cadence,          # 1:1  e.g. 180 BPM == 180 spm
        target_cadence / 2.0,    # half-time e.g. 90 BPM for 180 spm cadence
        target_cadence * 2.0,    # double-time (unlikely in practice)
    ]
    best = 0.0
    for t in targets:
        ratio = abs(track_bpm - t) / t
        if ratio <= tolerance:
            best = max(best, 1.0 - ratio / tolerance)
    return best


class PlaylistGenerator:

    def __init__(
        self,
        model:             PreferenceModel,
        library:           list[Track],
        athlete_avg_pace:  float,
        target_cadence:    int = TARGET_CADENCE,
        recently_heard:    Optional[set[str]] = None,   # track IDs from last 5 runs
    ) -> None:
        self._model           = model
        self._library         = [t for t in library if t.features is not None]
        self._avg_pace        = athlete_avg_pace
        self._target_cadence  = target_cadence
        self._recently_heard  = recently_heard or set()
        self._lib_matrix      = self._build_lib_matrix()

    def _build_lib_matrix(self) -> np.ndarray:
        if not self._library:
            return np.empty((0, 4))
        return np.vstack([audio_vec(t.features) for t in self._library])

    # ─────────────────────────────────────────────
    # Main generate
    # ─────────────────────────────────────────────

    def generate(
        self,
        segments:       list[CourseSegment],
        total_km:       float,
        pre_cue_markers: Optional[list[float]] = None,
        discovery_ratio: float = DISCOVERY_RATIO,
    ) -> list[PlaylistSlot]:
        """
        Generate the full course playlist.
        pre_cue_markers: list of km positions where hill onset occurs.
        """
        markers = set(pre_cue_markers or [])
        slots:      list[PlaylistSlot] = []
        used_ids:   set[str]           = set()
        prev_tempo: Optional[float]    = None
        elapsed_km: float              = 0.0

        for seg in segments:
            seg_min = segment_duration_min(seg)
            if seg_min < 0.1:
                continue

            # Check if this segment starts within a pre-cue window
            is_pre_cue = any(
                abs(seg.start_km - m) <= PRE_CUE_WINDOW_KM for m in markers
            )

            km_rem    = compute_km_remaining(seg, total_km)
            query     = build_query_vector(
                segment=seg,
                km_remaining_pct=km_rem,
                athlete_avg_pace=self._avg_pace,
                elapsed_km=elapsed_km,
                total_km=total_km,
            )
            target_y  = self._model.predict_target(query)
            target_af = denormalize_targets(target_y)

            if is_pre_cue:
                target_af = _override_for_hill(target_af)
                target_y  = audio_vec(target_af)

            remaining_min = seg_min
            cursor_km     = seg.start_km

            while remaining_min > 0.5 and cursor_km < seg.end_km:
                # Discovery: pick from tracks NOT heard in recent runs
                use_discovery = (
                    random.random() < discovery_ratio
                    and bool(self._recently_heard)
                )
                track = self._pick_track(
                    query=query,
                    target_y=target_y,
                    prev_tempo=prev_tempo,
                    used_ids=used_ids,
                    force_fresh=use_discovery,
                    is_pre_cue=is_pre_cue,
                )
                if track is None:
                    break

                duration_min = track.duration_ms / 60_000
                pace         = seg.target_pace or self._avg_pace
                track_end_km = min(
                    cursor_km + duration_min / pace,
                    seg.end_km,
                )
                score = self._score_track(query, track, target_y)

                slots.append(PlaylistSlot(
                    track=track,
                    start_km=round(cursor_km, 3),
                    end_km=round(track_end_km, 3),
                    segment_type=seg.type,
                    score=score,
                    is_discovery=(track.id not in self._recently_heard
                                  and track.id in used_ids),
                ))

                used_ids.add(track.id)
                prev_tempo    = track.features.tempo
                remaining_min -= duration_min
                cursor_km     = track_end_km

            elapsed_km = seg.end_km

        return slots

    # ─────────────────────────────────────────────
    # Track selection
    # ─────────────────────────────────────────────

    def _pick_track(
        self,
        query:        np.ndarray,
        target_y:     np.ndarray,
        prev_tempo:   Optional[float],
        used_ids:     set[str],
        force_fresh:  bool,
        is_pre_cue:   bool,
        top_n:        int = 20,
    ) -> Optional[Track]:
        """
        Score all eligible library tracks and return the best.

        Score = bandit_score × cadence_fitness × audio_proximity
        (geometric mean keeps each factor decisive)
        """
        candidates = [
            t for t in self._library
            if t.id not in used_ids
            and (not force_fresh or t.id not in self._recently_heard)
        ]
        if not candidates:
            # Relax constraints
            candidates = [t for t in self._library if t.id not in used_ids]
        if not candidates:
            candidates = self._library

        scored: list[tuple[float, Track]] = []
        for track in candidates:
            f = track.features
            if f is None:
                continue

            # BPM continuity
            if prev_tempo and abs(f.tempo - prev_tempo) > MAX_BPM_JUMP:
                if not is_pre_cue:   # pre-cue ignores continuity — energy leap is intentional
                    continue

            bandit    = self._model.score_track(query, audio_vec(f), track.id)
            cad_fit   = _cadence_fitness(f.tempo, self._target_cadence)
            proximity = 1.0 - float(np.linalg.norm(audio_vec(f) - target_y)) / 2.0
            proximity = max(0.0, proximity)

            # Geometric mean of three scores (all [0,1])
            composite = (max(bandit, 0) * max(cad_fit, 0.05) * proximity) ** (1 / 3)
            scored.append((composite, track))

        if not scored:
            return None

        scored.sort(key=lambda x: -x[0])
        # Add light stochasticity — sample from top-5 with score-weighted probability
        top = scored[:5]
        weights = np.array([s for s, _ in top], dtype=np.float64)
        weights = np.clip(weights, 1e-6, None)
        weights /= weights.sum()
        chosen = np.random.choice(len(top), p=weights)
        return top[chosen][1]

    def _score_track(
        self,
        query:    np.ndarray,
        track:    Track,
        target_y: np.ndarray,
    ) -> float:
        av       = audio_vec(track.features)
        bandit   = self._model.score_track(query, av, track.id)
        cad_fit  = _cadence_fitness(track.features.tempo, self._target_cadence)
        prox     = max(0.0, 1.0 - float(np.linalg.norm(av - target_y)) / 2.0)
        return round((max(bandit, 0) * max(cad_fit, 0.05) * prox) ** (1/3), 3)

    # ─────────────────────────────────────────────
    # Formatting
    # ─────────────────────────────────────────────

    @staticmethod
    def format_playlist(slots: list[PlaylistSlot]) -> str:
        lines = [
            f"{'km':>10}  {'terrain':<10}  {'BPM':>5}  {'⚡':>5}  {'★':>5}  track"
        ]
        lines.append("─" * 76)
        for s in slots:
            f    = s.track.features
            disc = " 🔭" if s.is_discovery else ""
            lines.append(
                f"{s.start_km:>4.2f}–{s.end_km:<5.2f}  "
                f"{s.segment_type.value:<10}  "
                f"{f.tempo:>5.0f}  "
                f"{f.energy:>5.2f}  "
                f"{s.score:>5.3f}  "
                f"{s.track.artist} – {s.track.name}{disc}"
            )
        return "\n".join(lines)


# ─────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────

def _override_for_hill(af: AudioFeatures) -> AudioFeatures:
    """
    Force audio feature targets toward a high-energy, driving profile
    suitable for mentally priming an uphill effort.
    """
    return AudioFeatures(
        tempo=max(af.tempo, 155.0),
        energy=max(af.energy, 0.82),
        valence=max(af.valence, 0.55),
        danceability=max(af.danceability, 0.65),
        acousticness=min(af.acousticness, 0.20),
        loudness=af.loudness,
        speechiness=af.speechiness,
    )
