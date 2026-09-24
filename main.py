"""
Pacesetter — Marathon Music Intelligence
=========================================

Commands:
  sync      Pull Strava + Spotify history → align → store
  analyze   Run local MIR (librosa) on liked-song preview URLs → store audio features
  train     (Re)train the LinUCB bandit model from stored aligned data
  plan      Generate + optionally push a playlist for a planned course (GPX)
  stats     Model stats and top songs per terrain type
  align     Re-align logged plays to runs (after analyze)
  plays     Play-logger status and songs captured per run

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
from typing import Optional

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

from data import play_log
from data.database import (
    clear_played_segments, get_all_tracks_with_features, get_all_workouts,
    get_play_log, get_played_segments, get_track, get_tracks_missing_features,
    init_db, insert_played_segments, insert_plays, prune_play_log,
    set_track_features, upsert_track, upsert_workout,
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
        print("  No new workouts from Strava.")

    # ── Listening history → local play_log ────────────────────────────────
    _collect_plays(sp)

    # ── Alignment (re-run from the full play_log) ──────────────────────────
    _align_from_play_log()

    # ── Prune plays that didn't happen during a run ────────────────────────
    removed = prune_play_log(get_all_workouts())
    if removed:
        print(f"  Pruned {removed} non-run play(s) older than 7 days")

    # ── Liked songs library ────────────────────────────────────────────────
    print("▶ Syncing liked songs …")
    saved = sp.fetch_saved_tracks(limit=2000)
    for t in saved:
        upsert_track(t)
    print(f"  {len(saved)} liked tracks stored")
    print("✓ Sync complete.  Next step:  python main.py analyze")


def _collect_plays(sp: SpotifyClient) -> None:
    """Pull plays from the cloud buffer (if configured) + Spotify directly."""
    records: list[dict] = []
    max_cloud_ms = None
    if play_log.configured():
        print("▶ Pulling plays from cloud logger …")
        cloud = play_log.fetch_cloud_plays()
        records.extend(cloud)
        if cloud:
            max_cloud_ms = max(play_log.played_at_ms(r["played_at"]) for r in cloud)
        print(f"  {len(cloud)} buffered play(s)")
    else:
        print("  (cloud logger not configured — only the last 50 plays are available)")

    print("▶ Fetching Spotify recently played …")
    direct = [r for r in map(play_log.from_spotify_item, sp.fetch_recently_played(max_tracks=50)) if r]
    records.extend(direct)

    new = insert_plays(records)
    print(f"  {new} new play(s) saved locally")

    # Local copy is committed → clear the cloud buffer up to what we pulled.
    if max_cloud_ms is not None:
        purged = play_log.purge_cloud_plays(max_cloud_ms)
        print(f"  Purged {purged} play(s) from cloud buffer")

    # Make sure every played track exists in the tracks table (features come later).
    for r in records:
        upsert_track(Track(id=r["track_id"], name=r["name"], artist=r["artist"],
                           album=r.get("album", ""), duration_ms=r["duration_ms"],
                           isrc=r.get("isrc")))


def _align_from_play_log() -> int:
    print("▶ Aligning music to workouts …")
    items = [play_log.to_spotify_item(r) for r in get_play_log()]
    tracks_by_id = {t.id: t for t in get_all_tracks_with_features()}
    segments = align_music_to_workouts(items, get_all_workouts(), tracks_by_id)
    clear_played_segments()
    if segments:
        insert_played_segments(segments)
    missing = len({i["track"]["id"] for i in items} - set(tracks_by_id))
    print(f"  {len(segments)} aligned segment(s)"
          + (f"  ({missing} played track(s) still need audio features)" if missing else ""))
    return len(segments)


def cmd_align(args) -> None:
    init_db()
    _align_from_play_log()


def cmd_postrun(args) -> None:
    """
    One-shot post-run workflow: sync (last-50 Spotify plays + latest Strava
    runs) → featurize only the newly-played tracks → re-align → flag
    anything that looks off → retrain once there's enough data.

    Run this right after finishing a run and uploading it to Strava.
    Spotify only remembers your last 50 plays, so the sooner this runs,
    the less likely later listening pushes today's songs out of that window.
    """
    print("═" * 60)
    print("POST-RUN  ▸  sync → analyze (played tracks only) → align → train")
    print("═" * 60)
    cmd_sync(args)

    print()
    from data.feature_sources import fetch_features_for_tracks
    played_ids = {r["track_id"] for r in get_play_log()}
    tracks = [t for t in get_tracks_missing_features() if t.id in played_ids]
    if tracks:
        print(f"▶ Fetching audio features for {len(tracks)} newly-played track(s) …")
        feats_map = fetch_features_for_tracks(tracks)
        for track_id, feats in feats_map.items():
            set_track_features(track_id, feats)
        mismatches = sum(1 for f in feats_map.values() if f.tempo_agree is False)
        print(f"  {len(feats_map)} / {len(tracks)} featured")
        if mismatches:
            print(f"  ⚠ {mismatches} track(s) had a tempo disagreement between sources "
                  f"— worth a listen before trusting them in training.")
    else:
        print("  No newly-played tracks need features.")

    print()
    _align_from_play_log()
    _check_latest_run()

    raw = get_played_segments()
    if len(raw) >= 20:
        print()
        cmd_train(args)
    else:
        print(f"\n  {len(raw)} aligned segment(s) total — "
              f"{20 - len(raw)} more needed before gradient boosting kicks in.")


def _check_latest_run() -> None:
    """Coverage + tempo-sanity check on the most recently synced run."""
    import json as _json

    workouts = get_all_workouts()
    if not workouts:
        return
    wo = max(workouts, key=lambda w: w.start_time)
    log = get_play_log()
    s, e = int(wo.start_time.timestamp() * 1000), int(wo.start_time.timestamp() * 1000) + wo.elapsed_sec * 1000
    n_played = sum(1 for r in log if r["played_at_ms"] - r["duration_ms"] < e and r["played_at_ms"] > s)
    expected = max(1, wo.elapsed_sec // 210)  # rough ~3.5 min/track

    print(f"\n▶ Latest run: {wo.name} ({wo.start_time:%Y-%m-%d}, {wo.elapsed_sec // 60} min) "
          f"— {n_played} play(s) captured")
    if n_played < expected * 0.6:
        print(f"  ⚠ that's fewer than the ~{expected} expected for a run this long. If you waited "
              f"a while before running this, later listening may have pushed early songs out of "
              f"Spotify's last-50-play window. Run `postrun` sooner next time.")

    segs = sorted((r for r in get_played_segments() if r["workout_id"] == wo.id),
                  key=lambda r: r["started_at"])
    prev: Optional[tuple[str, float]] = None
    for r in segs:
        tempo = _json.loads(r["features_json"])["tempo"]
        if prev is not None and abs(tempo - prev[1]) >= 35:
            t1, t2 = get_track(prev[0]), get_track(r["track_id"])
            n1 = f"{t1.artist} – {t1.name}" if t1 else prev[0]
            n2 = f"{t2.artist} – {t2.name}" if t2 else r["track_id"]
            print(f"  ⚠ tempo jump {prev[1]:.0f} → {tempo:.0f} BPM: \"{n1}\" → \"{n2}\" "
                  f"— worth a manual listen (could be a bad reading, or a song from after "
                  f"you finished bleeding into the window).")
        prev = (r["track_id"], tempo)


def cmd_plays(args) -> None:
    init_db()
    log = get_play_log()
    wos = get_all_workouts()
    print(f"Local play_log: {len(log)} play(s)")
    if log:
        print(f"  oldest {log[0]['played_at']}   newest {log[-1]['played_at']}")
    if play_log.configured():
        st = play_log.cloud_status()
        print(f"Cloud buffer: {st.get('buffered_plays')} play(s), newest {st.get('newest')}")
        if st.get("last_gap_ms"):
            gap = datetime.fromtimestamp(int(st["last_gap_ms"]) / 1000)
            print(f"  ⚠ possible gap in logging detected at {gap:%Y-%m-%d %H:%M}")
    for w in sorted(wos, key=lambda w: w.start_time)[-5:]:
        s = int(w.start_time.timestamp() * 1000)
        e = s + w.elapsed_sec * 1000
        n = sum(1 for r in log if r["played_at_ms"] - r["duration_ms"] < e and r["played_at_ms"] > s)
        print(f"  {w.start_time:%Y-%m-%d} {w.name[:30]:30s} {w.elapsed_sec//60:4d} min  {n:3d} song(s)")


# ─────────────────────────────────────────────
# analyze  (MIR via librosa)
# ─────────────────────────────────────────────

def cmd_analyze(args) -> None:
    """
    Fetch audio features for every track that doesn't have them yet, via
    the ReccoBeats + Deezer/iTunes hybrid pipeline (data/feature_sources.py).
    ReccoBeats supplies energy/valence/danceability directly (no audio
    needed); a Deezer or iTunes preview clip lets us cross-check tempo with
    our own librosa extraction. See feature_sources.py for the merge policy.

    --legacy falls back to the original librosa-on-Spotify-preview_url path
    (kept for tracks that DO have a Spotify preview_url, which is now rare).
    """
    init_db()

    if args.legacy:
        try:
            import librosa  # noqa: F401
        except ImportError:
            print("librosa not installed. Run:  pip install librosa soundfile")
            sys.exit(1)

        from audio.mir_engine import batch_analyse
        from data.database import get_conn

        with get_conn() as conn:
            rows = conn.execute(
                "SELECT id, preview_url FROM tracks "
                "WHERE preview_url IS NOT NULL AND features_json IS NULL"
            ).fetchall()
        if not rows:
            print("  No tracks with a Spotify preview_url need analysis.")
            return
        print(f"▶ Analysing {len(rows)} tracks via librosa (Spotify preview URLs) …")
        items = [(r["id"], r["preview_url"]) for r in rows]
        feats_map = batch_analyse(items)
        print(f"  {len(feats_map)} / {len(rows)} analysed successfully")
        for track_id, feats in feats_map.items():
            set_track_features(track_id, feats)
        print("✓ Analysis complete.  Next step:  python main.py align")
        return

    from data.feature_sources import fetch_features_for_tracks

    tracks = get_tracks_missing_features()
    if args.played_only:
        played_ids = {r["track_id"] for r in get_play_log()}
        tracks = [t for t in tracks if t.id in played_ids]
    if args.limit:
        tracks = tracks[: args.limit]
    if not tracks:
        print("  All tracks already have audio features."
              if not args.played_only else
              "  All played tracks already have audio features.")
        return

    print(f"▶ Fetching audio features for {len(tracks)} track(s) "
          f"(ReccoBeats + Deezer/iTunes) …")
    feats_map = fetch_features_for_tracks(tracks)

    hybrid = sum(1 for f in feats_map.values() if f.source == "hybrid")
    recco_only = sum(1 for f in feats_map.values() if f.source == "reccobeats")
    librosa_only = sum(1 for f in feats_map.values() if f.source == "librosa")
    mismatches = sum(1 for f in feats_map.values() if f.tempo_agree is False)
    print(f"  {len(feats_map)} / {len(tracks)} featured "
          f"(hybrid: {hybrid}, reccobeats-only: {recco_only}, librosa-only: {librosa_only})")
    if mismatches:
        print(f"  ⚠ {mismatches} track(s) had a tempo disagreement between "
              f"ReccoBeats and librosa — worth a listen before training.")

    for track_id, feats in feats_map.items():
        set_track_features(track_id, feats)

    # Optional: also scan a local music directory
    if args.local_dir:
        from audio.mir_engine import scan_local_library
        ld = Path(args.local_dir)
        local_map = scan_local_library(ld)
        print(f"  Local library: {len(local_map)} files analysed")

    print("✓ Analysis complete.  Next step:  python main.py align")


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

    an = sub.add_parser("analyze", help="Fetch audio features (ReccoBeats + Deezer/iTunes hybrid)")
    an.add_argument("--limit", type=int, default=None,
                     help="Only process the first N un-featured tracks (useful for a quick test)")
    an.add_argument("--legacy", action="store_true",
                     help="Use the old librosa-on-Spotify-preview_url path instead")
    an.add_argument("--local-dir", default="", help="Optional: path to a local music directory")
    an.add_argument("--played-only", action="store_true",
                     help="Only featurize tracks that were actually played during a run "
                          "(play_log), instead of the whole liked-songs library")

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
    sub.add_parser("align", help="Re-align play_log to workouts (run after analyze)")
    sub.add_parser("plays", help="Play-logger status and songs per recent run")
    sub.add_parser("postrun", help="One-shot: sync + analyze --played-only + align + train. "
                                    "Run this right after finishing a run.")

    args = p.parse_args()
    {"sync": cmd_sync, "analyze": cmd_analyze,
     "train": cmd_train, "plan": cmd_plan, "stats": cmd_stats,
     "align": cmd_align, "plays": cmd_plays, "postrun": cmd_postrun}[args.cmd](args)


if __name__ == "__main__":
    main()
