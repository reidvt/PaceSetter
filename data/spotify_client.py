"""
Spotify API client — non-deprecated endpoints only.

── Endpoint status ────────────────────────────────────────────────────────────
  ✗ /audio-features     Deprecated Nov 2024 (new apps); use mir_engine instead
  ✗ /audio-analysis     Deprecated Nov 2024 (new apps)
  ✗ /recommendations    Deprecated Nov 2024 (new apps)

  ✓ /me/player/recently-played  — listening history with timestamps
  ✓ /me/tracks                  — liked songs library
  ✓ POST /playlists             — create playlist
  ✓ preview_url on Track object — 30 s MP3 clip, feeds mir_engine

Note on app registration date
──────────────────────────────
The deprecations above apply to apps registered AFTER Nov 27, 2024.
If your Spotify developer app was created before that date, /audio-features
and /recommendations may still work for you.  Check your app's creation date
at https://developer.spotify.com/dashboard — if it pre-dates Nov 2024, you
can temporarily reinstate those calls as a fallback while your MIR cache builds.

preview_url availability
────────────────────────
Approximately 30 % of Spotify tracks return preview_url: null.  This is a
Spotify policy decision (usually label restrictions).  Tracks without a
preview cannot be analysed by mir_engine and will be excluded from generated
playlists.  The fetch methods log a count of null-preview tracks so you can
monitor coverage.  To improve coverage: add those tracks' audio files to a
local music directory and run:  python main.py analyze --local-dir ~/Music

Auth: Authorization Code flow.
Set env vars:
  SPOTIFY_CLIENT_ID
  SPOTIFY_CLIENT_SECRET
  SPOTIFY_REDIRECT_URI  (default: http://localhost:8888/callback)
"""

import logging
import os
import time
from datetime import datetime
from typing import Optional

import spotipy
from spotipy.oauth2 import SpotifyOAuth

from data.models import Track

logger = logging.getLogger(__name__)

SCOPES = " ".join([
    "user-read-recently-played",
    "user-library-read",
    "playlist-modify-private",
    "playlist-modify-public",
])

RECENT_LIMIT = 50


class SpotifyClient:
    def __init__(self) -> None:
        auth = SpotifyOAuth(
            client_id=os.environ["SPOTIFY_CLIENT_ID"],
            client_secret=os.environ["SPOTIFY_CLIENT_SECRET"],
            redirect_uri=os.environ.get(
                "SPOTIFY_REDIRECT_URI", "http://localhost:8888/callback"
            ),
            scope=SCOPES,
            cache_path=".spotify_token_cache",
            open_browser=True,
        )
        self._sp = spotipy.Spotify(auth_manager=auth)

    # ─────────────────────────────────────────────
    # Listening history
    # ─────────────────────────────────────────────

    def fetch_recently_played(
        self,
        after_unix_ms: Optional[int] = None,
        before_unix_ms: Optional[int] = None,
        limit: int = RECENT_LIMIT,
        max_tracks: int = 500,
    ) -> list[dict]:
        """
        Returns raw Spotify play_history objects (up to max_tracks).
        Each item: { "track": {..., "preview_url": "..."}, "played_at": "...Z" }

        Note: Spotify returns played_at as the moment the track *finished*
        playing, not when it started. We subtract duration_ms in alignment.py.
        Pagination is done via the 'before' cursor (unix ms timestamp).
        """
        kwargs: dict = {"limit": limit}
        if after_unix_ms:
            kwargs["after"] = after_unix_ms
        if before_unix_ms:
            kwargs["before"] = before_unix_ms

        results: list[dict] = []
        while len(results) < max_tracks:
            resp  = self._sp.current_user_recently_played(**kwargs)
            items = resp.get("items", [])
            if not items:
                break
            results.extend(items)
            if not resp.get("next"):
                break
            # Cursor: timestamp of the oldest item returned
            oldest = items[-1]["played_at"]
            dt = datetime.fromisoformat(oldest.replace("Z", "+00:00"))
            kwargs = {"limit": limit, "before": int(dt.timestamp() * 1000)}
            time.sleep(0.2)

        return results

    # ─────────────────────────────────────────────
    # Liked songs library
    # ─────────────────────────────────────────────

    def fetch_saved_tracks(self, limit: int = 2000) -> list[Track]:
        """
        Pull the user's full Liked Songs library.
        preview_url is kept — it feeds the MIR engine.
        Tracks with preview_url=None are stored but cannot be analysed;
        their count is logged so you can decide whether to supply local files.
        """
        tracks: list[Track] = []
        null_preview_count  = 0
        offset, batch       = 0, 50

        while len(tracks) < limit:
            resp  = self._sp.current_user_saved_tracks(limit=batch, offset=offset)
            items = resp.get("items", [])
            if not items:
                break
            for item in items:
                t = item["track"]
                if not t or not t.get("id"):
                    continue
                preview = t.get("preview_url")
                if preview is None:
                    null_preview_count += 1
                tracks.append(Track(
                    id=t["id"],
                    name=t["name"],
                    artist=", ".join(a["name"] for a in t["artists"]),
                    album=t["album"]["name"],
                    duration_ms=t["duration_ms"],
                    preview_url=preview,
                ))
            offset += batch
            time.sleep(0.15)

        total = len(tracks)
        if null_preview_count:
            pct = 100 * null_preview_count / max(total, 1)
            logger.warning(
                f"{null_preview_count}/{total} tracks ({pct:.0f}%) have no preview_url "
                f"and cannot be MIR-analysed. "
                f"Supply local files via: python main.py analyze --local-dir ~/Music"
            )
        return tracks

    # ─────────────────────────────────────────────
    # Playlist creation
    # ─────────────────────────────────────────────

    def create_playlist(
        self,
        name: str,
        track_ids: list[str],
        description: str = "",
    ) -> str:
        """Create a new private playlist and return its Spotify URL."""
        user_id = self._sp.me()["id"]
        pl = self._sp.user_playlist_create(
            user=user_id,
            name=name,
            public=False,
            description=description,
        )
        for i in range(0, len(track_ids), 100):
            self._sp.playlist_add_items(pl["id"], track_ids[i : i + 100])
            time.sleep(0.1)
        return pl["external_urls"]["spotify"]

    # ─────────────────────────────────────────────
    # Helpers
    # ─────────────────────────────────────────────

    @staticmethod
    def played_at_to_datetime(played_at: str) -> datetime:
        return datetime.fromisoformat(played_at.replace("Z", "+00:00"))
