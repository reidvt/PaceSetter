"""
Pacesetter — Marathon Music Intelligence
=========================================

Commands:
  sync      Pull Strava + Spotify history → align → store
  analyze   Run local MIR (librosa) on liked-song preview URLs → store audio features
  train     (Re)train the LinUCB bandit model from stored aligned data
  plan      Generate + optionally push a playlist for a planned course (GPX)
  stats     Model stats and top songs per terrain type

Quick start:
  cp .env.example .env                        # fill in your API keys
  python -m data.strava_client --auth         # one-time Strava OAuth
  python main.py sync                         # pull runs + listening history
  python main.py analyze                      # extract BPM/energy via librosa
  python main.py train                        # fit the bandit
  python main.py plan course.gpx --goal 3h45m --name "Boston 2025"
"""

import argparse
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Windows consoles default to cp1252 and choke on the UI glyphs below.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from data.database import (
    get_all_tracks_with_features, get_all_workouts, get_played_segments,
    init_db, insert_played_segments, upsert_track, upsert_workout,
)
from data.map_client import MapClient
from data.models import AudioFeatures, CourseSegment, Track
from data.spotify_client import SpotifyClient
from data.strava_client import StravaClient
from ml.playlist_generator import PlaylistGenerator
from ml.preference_model import PreferenceModel
from processing.alignment import align_music_to_workouts
from processing.course_analyzer import (
    assign_paces, compute_precue_markers, course_summary,
    load_course_cache, save_course_cache, segment_course_50m,
)
from processing.features import build_training_matrix

import numpy as np


# ─────────────────────────────────────────────
# sync
# ─────────────────────────────────────────────

def cmd_sync(args) -> None:
    print("▶ Initialising database …")
    init_db()

    sp = SpotifyClient()
    st = StravaClient()

    # ── Strava runs (last 90 days) ─────────────────────────────────────────
    print("▶ Fetching Strava runs (last 90 days) …")
    cutoff = int((datetime.now(tz=timezone.utc) - timedelta(days=90)).timestamp())
    acts   = st.fetch_run_activities(after_unix=cutoff, max_activities=200)
    print(f"  {len(acts)} run(s) found")

    workouts = []
    for i, act in enumerate(acts, 1):
        print(f"  [{i}/{len(acts)}] {act['name']}  ({act['distance']/1000:.1f} km)")
        wo = st.build_workout(act)
        if wo:
            upsert_workout(wo)
            workouts.append(wo)
        time.sleep(0.5)

    if not workouts:
        print("  No valid workouts. Exiting."); return

    # ── Spotify: recently played ────────────────────────────────────────────
    earliest   = min(wo.start_time for wo in workouts)
    after_ms   = int(earliest.timestamp() * 1000) - 60_000
    print("▶ Fetching Spotify recently played …")
    played     = sp.fetch_recently_played(after_unix_ms=after_ms)
    print(f"  {len(played)} plays found")

    # Build lightweight Track objects (no audio features yet — analyze does that)
    tracks_by_id: dict[str, Track] = {}
    for item in played:
        raw = item["track"]
        tid = raw["id"]
        t = Track(
            id=tid, name=raw["name"],
            artist=", ".join(a["name"] for a in raw["artists"]),
            album=raw["album"]["name"],
            duration_ms=raw["duration_ms"],
            preview_url=raw.get("preview_url"),
        )
        upsert_track(t)
        tracks_by_id[tid] = t

    # ── Alignment ──────────────────────────────────────────────────────────
    print("▶ Aligning music to workouts …")
    # Alignment needs features for filtering; use a permissive pass here —
    # features are checked again at training time.
    segments = align_music_to_workouts(played, workouts, tracks_by_id)
    print(f"  {len(segments)} aligned segment(s)")
    if segments:
        insert_played_segments(segments)

    # ── Liked songs library ────────────────────────────────────────────────
    print("▶ Syncing liked songs …")
    saved = sp.fetch_saved_tracks(limit=2000)
    for t in saved:
        upsert_track(t)
    print(f"  {len(saved)} liked tracks stored")
    print("✓ Sync complete.  Next step:  python main.py analyze")


# ─────────────────────────────────────────────
# analyze  (MIR via librosa)
# ─────────────────────────────────────────────

def cmd_analyze(args) -> None:
    """
    Download 30-second Spotify preview clips and extract audio features
    using librosa.  This is the replacement for the deprecated /audio-features.
    Results are cached in .mir_cache/ so tracks are only processed once.
    """
    try:
        import librosa  # noqa: F401
    except ImportError:
        print("librosa not installed. Run:  pip install librosa soundfile")
        sys.exit(1)

    from audio.mir_engine import batch_analyse
    from data.database import get_all_tracks_with_features, get_conn

    init_db()

    # Load all tracks that have a preview_url but no features yet
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, preview_url FROM tracks "
            "WHERE preview_url IS NOT NULL AND features_json IS NULL"
        ).fetchall()

    if not rows:
        print("  All tracks already analysed (or none have preview_url).")
        return

    print(f"▶ Analysing {len(rows)} tracks via librosa (preview URLs) …")
    items = [(r["id"], r["preview_url"]) for r in rows]
    feats_map = batch_analyse(items)
    print(f"  {len(feats_map)} / {len(rows)} analysed successfully")

    # Persist
    for track_id, feats in feats_map.items():
        with get_conn() as conn:
            import json
            conn.execute(
                "UPDATE tracks SET features_json=? WHERE id=?",
                (json.dumps(feats.__dict__), track_id),
            )

    # Optional: also scan a local music directory
    if args.local_dir:
        from audio.mir_engine import scan_local_library
        ld = Path(args.local_dir)
        local_map = scan_local_library(ld)
        print(f"  Local library: {len(local_map)} files analysed")

    print("✓ Analysis complete.  Next step:  python main.py train")


# ─────────────────────────────────────────────
# train
# ─────────────────────────────────────────────

def cmd_train(args) -> None:
    from data.models import PlayedSegment, SegmentType
    import json

    print("▶ Loading aligned segments …")
    raw = get_played_segments()
    if not raw:
        print("  No aligned segments. Run sync + analyze first."); return

    workouts  = get_all_workouts()
    avg_pace  = sum(wo.avg_pace for wo in workouts) / len(workouts) if workouts else 8.0
    total_km  = max((wo.total_km for wo in workouts), default=42.195)

    from datetime import datetime as _dt

    segs = []
    for row in raw:
        feats = json.loads(row["features_json"])
        t = Track(id=row["track_id"], name="", artist="", album="",
                  duration_ms=0, features=AudioFeatures(**feats))
        s = PlayedSegment(
            track=t, workout_id=row["workout_id"],
            started_at=_dt.fromisoformat(row["started_at"]),
            distance_start_km=row["distance_start_km"],
            distance_end_km=row["distance_end_km"],
            avg_pace=row["avg_pace"], avg_hr=row["avg_hr"],
            avg_gradient=row["avg_gradient"],
            segment_type=SegmentType(row["segment_type"]),
        )
        segs.append(s)

    print(f"  {len(segs)} segment(s)  |  avg pace {avg_pace:.2f} min/km")
    X, y, rewards = build_training_matrix(segs, avg_pace, total_km)

    if len(X) == 0:
        print("  No valid feature rows (tracks may lack audio features)."); return

    model = PreferenceModel()
    model.fit_batch(X, y, rewards)
    model.save()
    print(f"✓ Model trained on {len(X)} rows.")


# ─────────────────────────────────────────────
# plan
# ─────────────────────────────────────────────

def _parse_goal(s: str) -> float:
    s = s.strip().lower()
    if "h" in s:
        parts = s.replace("m", "").split("h")
        return float(parts[0]) * 60 + (float(parts[1]) if parts[1] else 0)
    if ":" in s:
        p = s.split(":")
        return (float(p[0]) * 60 + float(p[1]) + (float(p[2]) / 60 if len(p) > 2 else 0))
    return float(s)


def cmd_plan(args) -> None:
    gpx_path = Path(args.gpx)
    if not gpx_path.exists():
        print(f"GPX not found: {gpx_path}"); sys.exit(1)

    goal_min = _parse_goal(args.goal)
    cache_path = gpx_path.with_suffix(".course.json")

    # ── Course profile ─────────────────────────────────────────────────────
    if cache_path.exists() and not args.no_cache:
        print(f"▶ Loading cached course profile: {cache_path}")
        segments, pre_cues = load_course_cache(cache_path)
        total_km = segments[-1].end_km
    else:
        print(f"▶ Loading course from GPX: {gpx_path.name}")
        mc       = MapClient()
        points   = mc.load_gpx(gpx_path)
        total_km = points[-1].distance
        segments = segment_course_50m(points)
        segments = assign_paces(segments, goal_min, total_km)
        pre_cues = compute_precue_markers(segments)
        save_course_cache(segments, pre_cues, cache_path)

    print(f"  {total_km:.2f} km  |  goal {goal_min:.0f} min  |  {len(segments)} segments  |  {len(pre_cues)} hill pre-cues")
    print()
    print(course_summary(segments, total_km, pre_cues))
    print()

    # ── Library + model ────────────────────────────────────────────────────
    model   = PreferenceModel.load()
    library = get_all_tracks_with_features()
    print(f"▶ Library: {len(library)} tracks with audio features")

    workouts = get_all_workouts()
    avg_pace = (sum(wo.avg_pace for wo in workouts) / len(workouts)
                if workouts else goal_min / total_km)

    # Recently heard = tracks played in last 5 workouts (for discovery logic)
    recent_ids: set[str] = set()
    for raw in get_played_segments():
        recent_ids.add(raw["track_id"])

    gen   = PlaylistGenerator(
        model=model, library=library,
        athlete_avg_pace=avg_pace,
        recently_heard=recent_ids,
    )
    slots = gen.generate(
        segments=segments,
        total_km=total_km,
        pre_cue_markers=pre_cues,
        discovery_ratio=args.discovery,
    )
    print(f"\n▶ Generated {len(slots)} tracks\n")
    print(gen.format_playlist(slots))

    # ── Push to Spotify ────────────────────────────────────────────────────
    if not args.no_push:
        try:
            sp  = SpotifyClient()
            ids = [s.track.id for s in slots]
            url = sp.create_playlist(
                name=args.name or f"Pacesetter – {gpx_path.stem}",
                track_ids=ids,
                description=f"Pacesetter | {gpx_path.stem} | goal {args.goal}",
            )
            print(f"\n✓ Playlist created → {url}")
        except Exception as exc:
            print(f"\n[warn] Could not push playlist: {exc}")
    else:
        print("\n(--no-push: playlist not created on Spotify)")


# ─────────────────────────────────────────────
# stats
# ─────────────────────────────────────────────

def cmd_stats(args) -> None:
    from data.database import get_track
    import json

    model = PreferenceModel.load()
    n     = model.n_samples
    mode  = "hot" if n >= 50 else "warm" if n >= 20 else "cold"
    print(f"\nModel mode    : {mode}")
    print(f"Training rows : {n}")
    print(f"GBM fitted    : {'yes' if model.is_fitted else 'no (need 20+ samples)'}")

    imps = model.feature_importances()
    if imps is not None:
        feat_names = [
            "grade", "dist_pct", "hr_frac", "pace_stress",
            "cad_delta", "km_rem",
            "seg_flat", "seg_mod", "seg_steep", "seg_wall", "seg_descent",
        ]
        print("\nFeature importances (mean across outputs):")
        for name, imp in sorted(zip(feat_names, imps), key=lambda x: -x[1]):
            bar = "█" * int(imp * 30)
            print(f"  {name:<14} {imp:.3f}  {bar}")

    raw = get_played_segments()
    if not raw:
        print("\nNo aligned data yet. Run sync + analyze + train."); return

    by_type: dict[str, dict[str, int]] = {}
    for row in raw:
        st = row["segment_type"]
        tid = row["track_id"]
        by_type.setdefault(st, {})[tid] = by_type.get(st, {}).get(tid, 0) + 1

    print("\nTop 5 tracks by segment type:\n")
    for seg_type, counts in sorted(by_type.items()):
        top = sorted(counts.items(), key=lambda x: -x[1])[:5]
        print(f"  [{seg_type}]")
        for tid, cnt in top:
            t = get_track(tid)
            label = f"{t.artist} – {t.name}" if t else tid
            print(f"    {cnt:>3}×  {label}")
        print()


# ─────────────────────────────────────────────
# Argument parsing
# ─────────────────────────────────────────────

def main() -> None:
    p   = argparse.ArgumentParser(description="Pacesetter — terrain-aware marathon playlist generator")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("sync",  help="Sync Strava runs + Spotify history")

    an = sub.add_parser("analyze", help="Run local MIR (librosa) on library preview clips")
    an.add_argument("--local-dir", default="", help="Optional: path to a local music directory")

    sub.add_parser("train", help="(Re)train the LinUCB bandit model")

    pl = sub.add_parser("plan", help="Generate a course playlist")
    pl.add_argument("gpx",  help="GPX file of the course")
    pl.add_argument("--goal", default="4h00m", help="Target time e.g. 3h45m")
    pl.add_argument("--name", default="", help="Playlist name on Spotify")
    pl.add_argument("--discovery", type=float, default=0.20,
                    help="Fraction of library-fresh (less-heard) tracks (default 0.20)")
    pl.add_argument("--no-push", action="store_true", help="Print but don't push to Spotify")
    pl.add_argument("--no-cache", action="store_true", help="Ignore saved course cache, re-fetch elevation")

    sub.add_parser("stats", help="Model stats + top songs by terrain")

    args = p.parse_args()
    {"sync": cmd_sync, "analyze": cmd_analyze,
     "train": cmd_train, "plan": cmd_plan, "stats": cmd_stats}[args.cmd](args)


if __name__ == "__main__":
    main()
