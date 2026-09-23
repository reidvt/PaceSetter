"""
Strava API client.

Auth: OAuth2 with automatic token refresh via stravalib.
Set env vars:
  STRAVA_CLIENT_ID
  STRAVA_CLIENT_SECRET
  STRAVA_REFRESH_TOKEN   (obtained on first run, then cached)

First-run flow:
  python -m data.strava_client --auth
  → opens browser, paste the code, saves refresh token to .strava_token_cache
"""

import json
import os
import sys
import time
import webbrowser
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import urlencode

import requests

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from data.models import (
    GeoPoint, HeartRateZone, Workout, WorkoutSample,
)

TOKEN_CACHE = Path(".strava_token_cache")
BASE = "https://www.strava.com/api/v3"


def _hr_zone(bpm: Optional[int], max_hr: int = 190) -> Optional[HeartRateZone]:
    if bpm is None:
        return None
    pct = bpm / max_hr
    if pct < 0.60:  return HeartRateZone.Z1
    if pct < 0.70:  return HeartRateZone.Z2
    if pct < 0.80:  return HeartRateZone.Z3
    if pct < 0.90:  return HeartRateZone.Z4
    return HeartRateZone.Z5


class StravaClient:
    def __init__(self) -> None:
        self._client_id     = os.environ["STRAVA_CLIENT_ID"]
        self._client_secret = os.environ["STRAVA_CLIENT_SECRET"]
        self._access_token: Optional[str]  = None
        self._expires_at:   int            = 0
        self._refresh_token: Optional[str] = os.environ.get("STRAVA_REFRESH_TOKEN")
        self._load_cache()

    # ─────────────────────────────────────────────
    # Auth / token management
    # ─────────────────────────────────────────────

    def _load_cache(self) -> None:
        if TOKEN_CACHE.exists():
            d = json.loads(TOKEN_CACHE.read_text())
            self._access_token  = d.get("access_token")
            self._refresh_token = d.get("refresh_token", self._refresh_token)
            self._expires_at    = d.get("expires_at", 0)

    def _save_cache(self) -> None:
        TOKEN_CACHE.write_text(json.dumps({
            "access_token":  self._access_token,
            "refresh_token": self._refresh_token,
            "expires_at":    self._expires_at,
        }))

    def _ensure_token(self) -> str:
        now = int(time.time())
        if self._access_token and self._expires_at > now + 60:
            return self._access_token

        if not self._refresh_token:
            raise RuntimeError(
                "No Strava refresh token. Run:  python -m data.strava_client --auth"
            )

        resp = requests.post("https://www.strava.com/oauth/token", data={
            "client_id":     self._client_id,
            "client_secret": self._client_secret,
            "grant_type":    "refresh_token",
            "refresh_token": self._refresh_token,
        }).json()

        self._access_token  = resp["access_token"]
        self._refresh_token = resp["refresh_token"]
        self._expires_at    = resp["expires_at"]
        self._save_cache()
        return self._access_token

    def _get(self, path: str, **params) -> dict | list:
        token = self._ensure_token()
        r = requests.get(
            f"{BASE}{path}",
            headers={"Authorization": f"Bearer {token}"},
            params=params,
            timeout=15,
        )
        r.raise_for_status()
        return r.json()

    # ─────────────────────────────────────────────
    # Activities
    # ─────────────────────────────────────────────

    def fetch_run_activities(
        self,
        after_unix: Optional[int] = None,
        max_activities: int = 200,
    ) -> list[dict]:
        """Return raw Strava activity summaries for Run type."""
        results: list[dict] = []
        page = 1
        while len(results) < max_activities:
            kwargs: dict = {"per_page": 50, "page": page}
            if after_unix:
                kwargs["after"] = after_unix
            items = self._get("/athlete/activities", **kwargs)
            if not items:
                break
            runs = [a for a in items if a.get("type") == "Run"]
            results.extend(runs)
            page += 1
            time.sleep(0.2)
        return results[:max_activities]

    def fetch_activity_streams(self, activity_id: int) -> dict:
        """
        Fetch GPS + HR + altitude + cadence streams for a single activity.
        Cadence is steps-per-minute for one foot; multiply by 2 for total
        ground-contact cadence (matching running dynamics convention).
        Returns list of stream dicts from Strava.
        """
        keys = "latlng,altitude,heartrate,distance,time,velocity_smooth,cadence"
        return self._get(f"/activities/{activity_id}/streams", keys=keys, resolution="medium")

    # ─────────────────────────────────────────────
    # Build domain objects
    # ─────────────────────────────────────────────

    def build_workout(self, activity: dict) -> Optional[Workout]:
        """Convert a Strava activity dict + its streams into a Workout."""
        act_id   = activity["id"]
        start_dt = datetime.fromisoformat(
            activity["start_date"].replace("Z", "+00:00")
        )
        total_km = activity["distance"] / 1000
        avg_pace = (activity["moving_time"] / 60) / total_km if total_km else 0

        try:
            streams_list = self.fetch_activity_streams(act_id)
        except Exception:
            streams_list = []

        # Index streams by type
        streams: dict[str, list] = {}
        for s in streams_list:
            streams[s["type"]] = s["data"]

        samples = self._build_samples(start_dt, streams)

        return Workout(
            id=str(act_id),
            name=activity.get("name", "Run"),
            start_time=start_dt,
            total_km=total_km,
            elapsed_sec=activity.get("elapsed_time", 0),
            avg_pace=avg_pace,
            avg_hr=activity.get("average_heartrate"),
            strava_url=f"https://www.strava.com/activities/{act_id}",
            samples=samples,
        )

    @staticmethod
    def _build_samples(
        start_dt: datetime,
        streams: dict[str, list],
    ) -> list[WorkoutSample]:
        """Zip parallel stream arrays into WorkoutSample objects."""
        times     = streams.get("time", [])
        latlng    = streams.get("latlng", [])
        altitude  = streams.get("altitude", [])
        distances = streams.get("distance", [])
        velocities= streams.get("velocity_smooth", [])
        heartrates= streams.get("heartrate", [])
        cadences  = streams.get("cadence", [])   # spm (one foot); we double it

        n = min(len(times), len(latlng), len(distances))
        if n == 0:
            return []

        samples: list[WorkoutSample] = []
        prev_elev = altitude[0] if altitude else 0.0
        prev_dist = 0.0

        for i in range(n):
            lat, lon = latlng[i]
            elev     = altitude[i] if altitude else 0.0
            dist_km  = distances[i] / 1000
            vel_ms   = velocities[i] if velocities else 0.0
            pace     = (1 / (vel_ms * 60 / 1000)) if vel_ms > 0.5 else 20.0
            hr       = int(heartrates[i]) if heartrates and i < len(heartrates) else None

            delta_dist = (dist_km - prev_dist) * 1000  # metres
            delta_elev = elev - prev_elev
            gradient   = (delta_elev / delta_dist * 100) if delta_dist > 5 else 0.0
            gradient   = max(-30.0, min(30.0, gradient))  # clamp

            cad_raw = int(cadences[i]) if cadences and i < len(cadences) else None
            cadence = cad_raw * 2 if cad_raw is not None else None   # both feet

            ts = datetime.fromtimestamp(
                start_dt.timestamp() + times[i], tz=timezone.utc
            )
            samples.append(WorkoutSample(
                timestamp=ts, lat=lat, lon=lon,
                elevation=elev, distance=dist_km,
                instant_pace=pace, heart_rate=hr,
                hr_zone=_hr_zone(hr), gradient=gradient,
                cadence=cadence,
            ))
            prev_elev = elev
            prev_dist = dist_km

        return samples


# ─────────────────────────────────────────────
# First-run auth helper
# ─────────────────────────────────────────────

if __name__ == "__main__":
    if "--auth" in sys.argv:
        client_id = os.environ["STRAVA_CLIENT_ID"]
        params = {
            "client_id":     client_id,
            "response_type": "code",
            "redirect_uri":  "http://localhost/exchange_token",
            "approval_prompt": "auto",
            "scope":         "activity:read_all",
        }
        url = f"https://www.strava.com/oauth/authorize?{urlencode(params)}"

        # Allow non-interactive use:  --auth --code <code>
        code = None
        if "--code" in sys.argv:
            code = sys.argv[sys.argv.index("--code") + 1].strip()

        if not code:
            print(f"Opening: {url}")
            webbrowser.open(url)
            try:
                code = input("Paste the 'code' from the redirect URL: ").strip()
            except EOFError:
                print(
                    "\nNo interactive input available. Open the URL above, authorize, "
                    "then re-run:\n  python -m data.strava_client --auth --code <code>"
                )
                sys.exit(1)

        client_secret = os.environ["STRAVA_CLIENT_SECRET"]
        resp = requests.post("https://www.strava.com/oauth/token", data={
            "client_id":     client_id,
            "client_secret": client_secret,
            "code":          code,
            "grant_type":    "authorization_code",
        }).json()

        if "access_token" not in resp:
            print(f"Auth failed: {resp}")
            sys.exit(1)

        cache = {
            "access_token":  resp["access_token"],
            "refresh_token": resp["refresh_token"],
            "expires_at":    resp["expires_at"],
        }
        TOKEN_CACHE.write_text(json.dumps(cache))
        print(f"[ok] Strava auth saved to {TOKEN_CACHE}")
        print(f"     scope granted; refresh_token={resp['refresh_token']}")
