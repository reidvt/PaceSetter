"""
Client for the cloud play-logger buffer (see cloud/api/plays.py).

    python -m data.play_log --status          # what's buffered in the cloud
    python -m data.play_log --refresh-token   # print token to paste into Vercel

Env vars (in .env):
    PACESETTER_LOGGER_URL     e.g. https://pacesetter-logger.vercel.app
    PACESETTER_LOGGER_SECRET  same value as CRON_SECRET in Vercel
"""

from __future__ import annotations

import argparse
import json
import os
import urllib.request
from datetime import datetime
from typing import Optional


def configured() -> bool:
    return bool(os.environ.get("PACESETTER_LOGGER_URL")
                and os.environ.get("PACESETTER_LOGGER_SECRET"))


def _call(method: str, path: str) -> dict:
    base = os.environ["PACESETTER_LOGGER_URL"].rstrip("/")
    req = urllib.request.Request(
        f"{base}{path}",
        headers={"Authorization": f"Bearer {os.environ['PACESETTER_LOGGER_SECRET']}"},
        method=method,
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def fetch_cloud_plays(since_ms: int = 0) -> list[dict]:
    """Compact play records: track_id, played_at, duration_ms, name, artist, album, isrc."""
    return _call("GET", f"/api/plays?since={since_ms}")["plays"]


def purge_cloud_plays(before_ms: int) -> int:
    return _call("DELETE", f"/api/plays?before={before_ms}")["purged"]


def cloud_status() -> dict:
    return _call("GET", "/api/plays?action=status")


def trigger_log_now() -> dict:
    return _call("GET", "/api/plays?action=log")


def played_at_ms(played_at: str) -> int:
    return int(datetime.fromisoformat(played_at.replace("Z", "+00:00")).timestamp() * 1000)


def to_spotify_item(rec: dict) -> dict:
    """Reshape a compact record into the play_history shape alignment expects."""
    return {
        "played_at": rec["played_at"],
        "track": {
            "id": rec["track_id"],
            "name": rec["name"],
            "duration_ms": rec["duration_ms"],
            "artists": [{"name": rec["artist"]}],
            "album": {"name": rec.get("album", "")},
            "external_ids": {"isrc": rec.get("isrc")},
        },
    }


def from_spotify_item(item: dict) -> Optional[dict]:
    """Inverse of to_spotify_item, for plays fetched straight from Spotify."""
    t = item.get("track") or {}
    if not t.get("id"):
        return None
    return {
        "track_id": t["id"],
        "played_at": item["played_at"],
        "duration_ms": t["duration_ms"],
        "name": t["name"],
        "artist": ", ".join(a["name"] for a in t.get("artists", [])),
        "album": (t.get("album") or {}).get("name", ""),
        "isrc": (t.get("external_ids") or {}).get("isrc"),
    }


if __name__ == "__main__":
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--log-now", action="store_true")
    ap.add_argument("--refresh-token", action="store_true",
                    help="print the Spotify refresh token from the local cache")
    a = ap.parse_args()
    if a.refresh_token:
        print(json.load(open(".spotify_token_cache"))["refresh_token"])
    elif a.log_now:
        print(json.dumps(trigger_log_now(), indent=2))
    else:
        print(json.dumps(cloud_status(), indent=2))
