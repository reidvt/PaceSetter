"""
Core data models for Pacesetter.
All domain objects flow through these dataclasses — keeping them
in one place makes serialization and schema evolution easy.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional


# ─────────────────────────────────────────────
# Terrain / Course
# ─────────────────────────────────────────────

class SegmentType(str, Enum):
    FLAT     = "flat"       # gradient < 2 %
    MODERATE = "moderate"   # 2–5 %
    STEEP    = "steep"      # 5–10 %
    WALL     = "wall"       # > 10 %
    DESCENT  = "descent"    # negative gradient


@dataclass
class GeoPoint:
    lat:       float
    lon:       float
    elevation: float          # metres
    distance:  float          # cumulative km from start
    timestamp: Optional[datetime] = None


@dataclass
class CourseSegment:
    """A continuous stretch of road with a consistent gradient character."""
    start_km:     float
    end_km:       float
    type:         SegmentType
    avg_gradient: float          # %
    avg_elevation: float         # m
    target_pace:  Optional[float] = None  # min/km, filled by planner


# ─────────────────────────────────────────────
# Workout (from Strava)
# ─────────────────────────────────────────────

class HeartRateZone(int, Enum):
    Z1 = 1   # recovery
    Z2 = 2   # aerobic base
    Z3 = 3   # tempo
    Z4 = 4   # threshold
    Z5 = 5   # max


@dataclass
class WorkoutSample:
    """One GPS/HR sample during a run, already enriched with derived fields."""
    timestamp:      datetime
    lat:            float
    lon:            float
    elevation:      float          # m
    distance:       float          # cumulative km
    instant_pace:   float          # min/km
    heart_rate:     Optional[int]  # bpm
    hr_zone:        Optional[HeartRateZone]
    gradient:       float          # % over last 100 m
    cadence:        Optional[int]  = None  # total steps/min (both feet)


@dataclass
class Workout:
    id:            str
    name:          str
    start_time:    datetime
    total_km:      float
    elapsed_sec:   int
    avg_pace:      float           # min/km
    avg_hr:        Optional[float]
    samples:       list[WorkoutSample] = field(default_factory=list)
    strava_url:    str = ""


# ─────────────────────────────────────────────
# Music (from Spotify)
# ─────────────────────────────────────────────

@dataclass
class AudioFeatures:
    """Spotify audio analysis for a single track."""
    tempo:          float   # BPM
    energy:         float   # 0–1  (intensity / loudness)
    valence:        float   # 0–1  (positivity / happiness)
    danceability:   float   # 0–1
    acousticness:   float   # 0–1  (acoustic vs. electronic)
    loudness:       float   # dB  (typically -60 to 0)
    speechiness:    float   # 0–1
    # Provenance — added for the Deezer/ReccoBeats hybrid pipeline.
    # Optional with defaults so old rows (all-librosa) still deserialise fine.
    source:          str = "librosa"          # "reccobeats" | "librosa" | "hybrid"
    tempo_source:    str = "librosa"          # which source the tempo came from
    tempo_agree:     Optional[bool] = None    # librosa vs reccobeats tempo agreed (~octave-aware)


@dataclass
class Track:
    id:       str
    name:     str
    artist:   str
    album:    str
    duration_ms: int
    features: Optional[AudioFeatures] = None
    preview_url: Optional[str] = None
    isrc:     Optional[str] = None


@dataclass
class PlayedSegment:
    """
    A track that was playing during a known time window on a run.
    Created by aligning Spotify recently-played timestamps with
    Strava GPS samples.
    """
    track:             Track
    workout_id:        str
    started_at:        datetime       # wall-clock
    distance_start_km: float
    distance_end_km:   float
    avg_pace:          float          # min/km while track played
    avg_hr:            Optional[float]
    avg_gradient:      float          # % — negative = descent
    segment_type:      SegmentType
    avg_cadence:       Optional[float] = None   # total spm (both feet)
    pace_deviation:    Optional[float] = None   # actual - target (min/km)
    hr_variability:    Optional[float] = None   # std of HR during segment


# ─────────────────────────────────────────────
# ML / Recommendation
# ─────────────────────────────────────────────

@dataclass
class PlaylistSlot:
    """One song assigned to a stretch of the planned course."""
    track:           Track
    start_km:        float
    end_km:          float
    segment_type:    SegmentType
    score:           float        # model confidence (0–1)
    is_discovery:    bool         # True → Spotify recommendation, not personal history
