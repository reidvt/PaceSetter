"""
SQLite persistence layer for Pacesetter.

Uses a single file database so everything is portable — no server needed.
Tables are created on first run.  The schema is intentionally flat
(JSON blobs for arrays) so we can iterate quickly without migrations.
"""

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Generator, Optional

from data.models import (
    AudioFeatures, PlayedSegment, SegmentType, Track, Workout, WorkoutSample,
    HeartRateZone,
)

DB_PATH = Path("pacesetter.db")

DDL = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS tracks (
    id           TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    artist       TEXT NOT NULL,
    album        TEXT NOT NULL,
    duration_ms  INTEGER,
    features_json TEXT,        -- serialised AudioFeatures or NULL
    preview_url  TEXT,
    isrc         TEXT
);

CREATE TABLE IF NOT EXISTS workouts (
    id           TEXT PRIMARY KEY,
    name         TEXT,
    start_time   TEXT NOT NULL,
    total_km     REAL,
    elapsed_sec  INTEGER,
    avg_pace     REAL,
    avg_hr       REAL,
    strava_url   TEXT,
    samples_json TEXT          -- serialised list[WorkoutSample]
);

CREATE TABLE IF NOT EXISTS played_segments (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    track_id            TEXT NOT NULL,
    workout_id          TEXT NOT NULL,
    started_at          TEXT NOT NULL,
    distance_start_km   REAL,
    distance_end_km     REAL,
    avg_pace            REAL,
    avg_hr              REAL,
    avg_gradient        REAL,
    segment_type        TEXT,
    FOREIGN KEY (track_id)  REFERENCES tracks(id),
    FOREIGN KEY (workout_id) REFERENCES workouts(id)
);

CREATE INDEX IF NOT EXISTS idx_ps_workout ON played_segments(workout_id);
CREATE INDEX IF NOT EXISTS idx_ps_track   ON played_segments(track_id);

-- Raw listening log (from the cloud play logger + direct Spotify pulls).
-- Pruned to plays that overlap a run; see prune_play_log().
CREATE TABLE IF NOT EXISTS play_log (
    track_id     TEXT NOT NULL,
    played_at    TEXT NOT NULL,   -- ISO-8601 UTC, when the track ENDED
    played_at_ms INTEGER NOT NULL,
    duration_ms  INTEGER NOT NULL,
    name         TEXT,
    artist       TEXT,
    album        TEXT,
    isrc         TEXT,
    PRIMARY KEY (track_id, played_at)
);
CREATE INDEX IF NOT EXISTS idx_pl_time ON play_log(played_at_ms);
"""


@contextmanager
def get_conn(path: Path = DB_PATH) -> Generator[sqlite3.Connection, None, None]:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db(path: Path = DB_PATH) -> None:
    with get_conn(path) as conn:
        conn.executescript(DDL)
        _migrate(conn)


def _migrate(conn: sqlite3.Connection) -> None:
    """Lightweight column migrations for existing databases (no migration
    framework — the schema is small and CREATE TABLE IF NOT EXISTS doesn't
    add new columns to a table that already exists)."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(tracks)").fetchall()}
    if "isrc" not in cols:
        conn.execute("ALTER TABLE tracks ADD COLUMN isrc TEXT")


# ─────────────────────────────────────────────
# Track CRUD
# ─────────────────────────────────────────────

def _features_to_json(f: Optional[AudioFeatures]) -> Optional[str]:
    if f is None:
        return None
    return json.dumps(f.__dict__)


def _json_to_features(raw: Optional[str]) -> Optional[AudioFeatures]:
    if not raw:
        return None
    d = json.loads(raw)
    return AudioFeatures(**d)


def upsert_track(t: Track, path: Path = DB_PATH) -> None:
    with get_conn(path) as conn:
        conn.execute(
            """INSERT INTO tracks VALUES (?,?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET
                 features_json=COALESCE(excluded.features_json, tracks.features_json),
                 preview_url=COALESCE(excluded.preview_url, tracks.preview_url),
                 isrc=COALESCE(excluded.isrc, tracks.isrc)""",
            (t.id, t.name, t.artist, t.album, t.duration_ms,
             _features_to_json(t.features), t.preview_url, t.isrc),
        )


def get_track(track_id: str, path: Path = DB_PATH) -> Optional[Track]:
    with get_conn(path) as conn:
        row = conn.execute("SELECT * FROM tracks WHERE id=?", (track_id,)).fetchone()
    if not row:
        return None
    return Track(
        id=row["id"], name=row["name"], artist=row["artist"],
        album=row["album"], duration_ms=row["duration_ms"],
        features=_json_to_features(row["features_json"]),
        preview_url=row["preview_url"],
        isrc=row["isrc"],
    )


def get_all_tracks_with_features(path: Path = DB_PATH) -> list[Track]:
    with get_conn(path) as conn:
        rows = conn.execute(
            "SELECT * FROM tracks WHERE features_json IS NOT NULL"
        ).fetchall()
    return [
        Track(
            id=r["id"], name=r["name"], artist=r["artist"],
            album=r["album"], duration_ms=r["duration_ms"],
            features=_json_to_features(r["features_json"]),
        )
        for r in rows
    ]


# ─────────────────────────────────────────────
# Workout CRUD
# ─────────────────────────────────────────────

def _sample_to_dict(s: WorkoutSample) -> dict:
    return {
        "timestamp": s.timestamp.isoformat(),
        "lat": s.lat, "lon": s.lon,
        "elevation": s.elevation,
        "distance": s.distance,
        "instant_pace": s.instant_pace,
        "heart_rate": s.heart_rate,
        "hr_zone": s.hr_zone.value if s.hr_zone else None,
        "gradient": s.gradient,
        "cadence": s.cadence,
    }


def _dict_to_sample(d: dict) -> WorkoutSample:
    return WorkoutSample(
        timestamp=datetime.fromisoformat(d["timestamp"]),
        lat=d["lat"], lon=d["lon"],
        elevation=d["elevation"], distance=d["distance"],
        instant_pace=d["instant_pace"], heart_rate=d["heart_rate"],
        hr_zone=HeartRateZone(d["hr_zone"]) if d["hr_zone"] else None,
        gradient=d["gradient"],
        cadence=d.get("cadence"),
    )


def upsert_workout(w: Workout, path: Path = DB_PATH) -> None:
    with get_conn(path) as conn:
        conn.execute(
            """INSERT INTO workouts VALUES (?,?,?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET
                 samples_json=excluded.samples_json,
                 avg_pace=excluded.avg_pace,
                 avg_hr=excluded.avg_hr""",
            (w.id, w.name, w.start_time.isoformat(), w.total_km, w.elapsed_sec,
             w.avg_pace, w.avg_hr, w.strava_url,
             json.dumps([_sample_to_dict(s) for s in w.samples])),
        )


def get_all_workouts(path: Path = DB_PATH) -> list[Workout]:
    with get_conn(path) as conn:
        rows = conn.execute("SELECT * FROM workouts").fetchall()
    results = []
    for r in rows:
        samples = [_dict_to_sample(d) for d in json.loads(r["samples_json"] or "[]")]
        results.append(Workout(
            id=r["id"], name=r["name"],
            start_time=datetime.fromisoformat(r["start_time"]),
            total_km=r["total_km"], elapsed_sec=r["elapsed_sec"],
            avg_pace=r["avg_pace"], avg_hr=r["avg_hr"],
            strava_url=r["strava_url"], samples=samples,
        ))
    return results


# ─────────────────────────────────────────────
# Played Segments CRUD
# ─────────────────────────────────────────────

def insert_played_segments(segments: list[PlayedSegment], path: Path = DB_PATH) -> None:
    rows = [
        (seg.track.id, seg.workout_id, seg.started_at.isoformat(),
         seg.distance_start_km, seg.distance_end_km,
         seg.avg_pace, seg.avg_hr, seg.avg_gradient, seg.segment_type.value)
        for seg in segments
    ]
    with get_conn(path) as conn:
        conn.executemany(
            """INSERT OR IGNORE INTO played_segments
               (track_id, workout_id, started_at, distance_start_km,
                distance_end_km, avg_pace, avg_hr, avg_gradient, segment_type)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            rows,
        )


def get_played_segments(path: Path = DB_PATH) -> list[dict]:
    """Return raw dicts joined with track audio features for ML consumption."""
    with get_conn(path) as conn:
        rows = conn.execute("""
            SELECT ps.*, t.features_json
            FROM played_segments ps
            JOIN tracks t ON t.id = ps.track_id
            WHERE t.features_json IS NOT NULL
        """).fetchall()
    return [dict(r) for r in rows]


def clear_played_segments(path: Path = DB_PATH) -> None:
    """Alignment is recomputed from play_log each time, so start clean."""
    with get_conn(path) as conn:
        conn.execute("DELETE FROM played_segments")


# ─────────────────────────────────────────────
# Play log
# ─────────────────────────────────────────────

def insert_plays(records: list[dict], path: Path = DB_PATH) -> int:
    """Insert compact play records; returns number of new rows."""
    rows = [
        (r["track_id"], r["played_at"],
         int(datetime.fromisoformat(r["played_at"].replace("Z", "+00:00")).timestamp() * 1000),
         r["duration_ms"], r.get("name"), r.get("artist"), r.get("album"), r.get("isrc"))
        for r in records
    ]
    with get_conn(path) as conn:
        before = conn.execute("SELECT COUNT(*) FROM play_log").fetchone()[0]
        conn.executemany(
            "INSERT OR IGNORE INTO play_log VALUES (?,?,?,?,?,?,?,?)", rows)
        after = conn.execute("SELECT COUNT(*) FROM play_log").fetchone()[0]
    return after - before


def get_play_log(path: Path = DB_PATH) -> list[dict]:
    with get_conn(path) as conn:
        rows = conn.execute("SELECT * FROM play_log ORDER BY played_at_ms").fetchall()
    return [dict(r) for r in rows]


def prune_play_log(
    workouts: list[Workout],
    grace_days: int = 7,
    margin_min: int = 10,
    path: Path = DB_PATH,
) -> int:
    """
    Delete plays that don't overlap any run (± margin_min) and are older than
    grace_days. The grace period protects plays from runs that haven't been
    uploaded to Strava / synced yet. Returns the number of rows deleted.
    """
    now_ms = int(datetime.now().timestamp() * 1000)
    cutoff_ms = now_ms - grace_days * 86_400_000
    margin_ms = margin_min * 60_000
    windows = [
        (int(w.start_time.timestamp() * 1000) - margin_ms,
         int(w.start_time.timestamp() * 1000) + w.elapsed_sec * 1000 + margin_ms)
        for w in workouts
    ]
    with get_conn(path) as conn:
        rows = conn.execute(
            "SELECT rowid, played_at_ms, duration_ms FROM play_log WHERE played_at_ms < ?",
            (cutoff_ms,),
        ).fetchall()
        doomed = []
        for r in rows:
            end, start = r["played_at_ms"], r["played_at_ms"] - r["duration_ms"]
            if not any(start < w_end and end > w_start for w_start, w_end in windows):
                doomed.append((r["rowid"],))
        conn.executemany("DELETE FROM play_log WHERE rowid=?", doomed)
    return len(doomed)


def get_tracks_missing_features(path: Path = DB_PATH) -> list[Track]:
    with get_conn(path) as conn:
        rows = conn.execute(
            "SELECT * FROM tracks WHERE features_json IS NULL"
        ).fetchall()
    return [
        Track(
            id=r["id"], name=r["name"], artist=r["artist"], album=r["album"],
            duration_ms=r["duration_ms"], preview_url=r["preview_url"], isrc=r["isrc"],
        )
        for r in rows
    ]


def set_track_isrc(track_id: str, isrc: str, path: Path = DB_PATH) -> None:
    with get_conn(path) as conn:
        conn.execute(
            "UPDATE tracks SET isrc=COALESCE(isrc, ?) WHERE id=?", (isrc, track_id)
        )


def set_track_features(track_id: str, features, path: Path = DB_PATH) -> None:
    with get_conn(path) as conn:
        conn.execute(
            "UPDATE tracks SET features_json=? WHERE id=?",
            (_features_to_json(features), track_id),
        )
