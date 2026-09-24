"""
Export a compact snapshot of pacesetter.db for the web dashboard.

    python scripts/export_dashboard.py            # writes cloud/api/_snapshot.json.gz
    python scripts/export_dashboard.py --push     # ...and uploads it to Upstash Redis

With --push the live dashboard picks up the new snapshot within a minute, no
redeploy needed. Needs KV_REST_API_URL / KV_REST_API_TOKEN in .env (copy them
from the Vercel project's Upstash integration).

The snapshot is bundled with the dashboard API function (cloud/api/dash.py),
which only serves it behind DASHBOARD_PASSWORD. It is never a public static file.
"""

from __future__ import annotations

import argparse
import base64
import gzip
import os
import urllib.request
import json
import sqlite3
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "pacesetter.db"
OUT = ROOT / "cloud" / "api" / "_snapshot.json.gz"
MAX_PTS = 160


def _ts(s: str) -> float:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def _downsample(samples: list[dict], n: int) -> list[dict]:
    if len(samples) <= n:
        return samples
    step = (len(samples) - 1) / (n - 1)
    return [samples[round(i * step)] for i in range(n)]


def _delta(xs: list[int]) -> list[int]:
    return [xs[0]] + [b - a for a, b in zip(xs, xs[1:])] if xs else []


def _load_env() -> None:
    env = ROOT / ".env"
    if not env.exists():
        return
    for line in env.read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def push(blob: bytes) -> None:
    _load_env()
    url = os.environ.get("KV_REST_API_URL") or os.environ.get("UPSTASH_REDIS_REST_URL")
    tok = os.environ.get("KV_REST_API_TOKEN") or os.environ.get("UPSTASH_REDIS_REST_TOKEN")
    if not url or not tok:
        raise SystemExit("[export] --push needs KV_REST_API_URL and KV_REST_API_TOKEN in .env")
    req = urllib.request.Request(
        f"{url.rstrip('/')}/pipeline",
        data=json.dumps([["SET", "pacesetter:snapshot", base64.b64encode(blob).decode()]]).encode(),
        headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        out = json.loads(r.read())
    if "error" in out[0]:
        raise SystemExit(f"[export] Redis error: {out[0]['error']}")
    print("[export] pushed snapshot to Redis → dashboard updates within a minute")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--push", action="store_true", help="also upload the snapshot to Upstash Redis")
    args = ap.parse_args()

    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row

    runs = []
    for w in c.execute("SELECT * FROM workouts ORDER BY start_time"):
        samples = json.loads(w["samples_json"] or "[]")
        samples = [s for s in samples if s.get("lat") is not None]
        if not samples:
            continue
        t0 = _ts(samples[0]["timestamp"])
        pts = _downsample(samples, MAX_PTS)
        runs.append({
            "id": w["id"], "name": w["name"], "start": w["start_time"],
            "km": round(w["total_km"] or 0, 3), "sec": w["elapsed_sec"],
            "pace": round(w["avg_pace"] or 0, 3), "hr": w["avg_hr"],
            "url": w["strava_url"] or f"https://www.strava.com/activities/{w['id']}",
            "t0": t0,
            # Delta-encoded integers keep the bundle small; the page decodes them.
            "t":   _delta([round(_ts(s["timestamp"]) - t0) for s in pts]),
            "lat": _delta([round(s["lat"] * 1e5) for s in pts]),
            "lon": _delta([round(s["lon"] * 1e5) for s in pts]),
            "ele": _delta([round((s.get("elevation") or 0) * 10) for s in pts]),
            "d":   _delta([round((s.get("distance") or 0) * 1000) for s in pts]),
            "p":   [round(min(s.get("instant_pace") or 0, 15) * 100) for s in pts],
            "hrs": [s.get("heart_rate") for s in pts] if any(s.get("heart_rate") for s in pts) else None,
        })

    plays = [
        [r["track_id"], r["played_at_ms"], r["duration_ms"], r["name"], r["artist"]]
        for r in c.execute("SELECT * FROM play_log ORDER BY played_at_ms")
    ]
    play_ids = {p[0] for p in plays} | {
        r[0] for r in c.execute("SELECT track_id FROM played_segments")
    }

    tracks = {}
    lib_artists: Counter = Counter()
    lib_tempos = []
    n_lib = 0
    for t in c.execute("SELECT * FROM tracks"):
        n_lib += 1
        for a in (t["artist"] or "").split(", "):
            lib_artists[a] += 1
        f = json.loads(t["features_json"]) if t["features_json"] else None
        if f:
            lib_tempos.append(round(f.get("tempo") or 0))
        if f or t["id"] in play_ids:
            tracks[t["id"]] = {
                "n": t["name"], "a": t["artist"], "dur": t["duration_ms"],
                "f": None if not f else {
                    "tempo": f.get("tempo"), "energy": f.get("energy"),
                    "valence": f.get("valence"), "dance": f.get("danceability"),
                    "acoustic": f.get("acousticness"),
                },
            }

    snap = {
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "runs": runs,
        "plays": plays,
        "tracks": tracks,
        "library": {
            "size": n_lib,
            "with_features": sum(1 for t in tracks.values() if t["f"]),
            "top_artists": lib_artists.most_common(15),
        },
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(snap, separators=(",", ":")).encode()
    blob = gzip.compress(raw, 9)
    OUT.write_bytes(blob)
    print(f"[export] {len(runs)} runs, {len(plays)} plays, {len(tracks)} tracks "
          f"→ {OUT.relative_to(ROOT)} ({OUT.stat().st_size/1024:.0f} KB gz, {len(raw)/1024:.0f} KB raw)")

    if args.push:
        push(blob)


if __name__ == "__main__":
    main()
