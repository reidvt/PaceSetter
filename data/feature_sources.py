"""
Hybrid audio-feature fetcher — replaces the deprecated Spotify
/audio-features endpoint using three free sources, combined:

  1. ReccoBeats  — a trained model over the Spotify catalogue. Takes a
     Spotify track ID directly, returns the full old-Spotify feature set
     (tempo, energy, valence, danceability, acousticness, loudness,
     speechiness). No audio download needed. This is the primary source
     for energy/valence/danceability, where our own librosa proxies are
     weakest.
  2. Deezer (by ISRC) — resolves a Spotify track to a Deezer track via its
     ISRC and returns a 30-second preview MP3. Used to run OUR librosa
     tempo extraction as a cross-check against ReccoBeats' tempo (BPM is
     the one feature where an independent, inspectable measurement is
     worth having — ReccoBeats/any model can get tempo wrong by an octave
     just like librosa can).
  3. iTunes Search API — fallback preview source for tracks Deezer misses
     (matched by artist + title, since iTunes has no ISRC lookup).

Merge policy, per track:
  - If ReccoBeats has data: use its energy/valence/danceability/
    acousticness/loudness/speechiness. Use ITS tempo, UNLESS a librosa
    tempo is also available and they disagree — see _reconcile_tempo().
  - If ReccoBeats has no data but a preview (Deezer/iTunes) does: fall
    back to full librosa extraction (audio.mir_engine._extract), same as
    the original pipeline.
  - If neither has anything: track stays unfeatured.

Every stored AudioFeatures records `source` and `tempo_source` so you can
tell later which numbers came from where, and `tempo_agree` flags tracks
worth spot-checking by ear.

NOTE ON NETWORK ACCESS: this file was written and unit-tested with mocked
HTTP responses only — I could not reach api.reccobeats.com, api.deezer.com
or itunes.apple.com from either the cloud sandbox or your machine's shell
(both got 403 from the egress proxy — an organization network policy, not
a bug in this code). Run `python -m data.feature_sources --selftest` on a
plain terminal / network to confirm it works against the real APIs before
trusting it on your full library.
"""

from __future__ import annotations

import argparse
import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional

from data.models import AudioFeatures

logger = logging.getLogger(__name__)

TIMEOUT = 10
_UA = "Pacesetter/1.0 (+https://github.com/reidvt/PaceSetter)"


def _get_json(url: str) -> Optional[dict]:
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        logger.warning(f"GET {url} -> HTTP {e.code}")
        return None
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        logger.warning(f"GET {url} -> {e}")
        return None


# ─────────────────────────────────────────────
# ReccoBeats
# ─────────────────────────────────────────────

RECCOBEATS_BASE = "https://api.reccobeats.com/v1"


def reccobeats_features(spotify_id: str) -> Optional[dict]:
    """
    Returns raw ReccoBeats audio-feature fields for a Spotify track ID,
    or None if ReccoBeats doesn't have the track.

    ReccoBeats first needs its own internal track id, resolved from the
    Spotify id, then the feature lookup uses that internal id.
    """
    resolved = _get_json(
        f"{RECCOBEATS_BASE}/track?ids={urllib.parse.quote(spotify_id)}"
    )
    content = (resolved or {}).get("content") or []
    if not content:
        return None
    recco_id = content[0].get("id")
    if not recco_id:
        return None
    feats = _get_json(f"{RECCOBEATS_BASE}/track/{recco_id}/audio-features")
    return feats


def reccobeats_to_audio_features(raw: dict) -> AudioFeatures:
    return AudioFeatures(
        tempo=float(raw.get("tempo", 120.0)),
        energy=float(raw.get("energy", 0.5)),
        valence=float(raw.get("valence", 0.5)),
        danceability=float(raw.get("danceability", 0.5)),
        acousticness=float(raw.get("acousticness", 0.5)),
        loudness=float(raw.get("loudness", -10.0)),
        speechiness=float(raw.get("speechiness", 0.05)),
        source="reccobeats",
        tempo_source="reccobeats",
    )


# ─────────────────────────────────────────────
# Deezer (preview by ISRC)
# ─────────────────────────────────────────────

def deezer_preview_by_isrc(isrc: str) -> Optional[str]:
    data = _get_json(f"https://api.deezer.com/track/isrc:{urllib.parse.quote(isrc)}")
    if not data or data.get("error"):
        return None
    return data.get("preview") or None


# ─────────────────────────────────────────────
# iTunes (fallback preview by artist + title)
# ─────────────────────────────────────────────

def itunes_preview(artist: str, title: str) -> Optional[str]:
    term = urllib.parse.quote(f"{artist} {title}")
    data = _get_json(
        f"https://itunes.apple.com/search?term={term}&media=music&entity=song&limit=1"
    )
    results = (data or {}).get("results") or []
    if not results:
        return None
    return results[0].get("previewUrl")


# ─────────────────────────────────────────────
# Tempo reconciliation
# ─────────────────────────────────────────────

def _reconcile_tempo(recco_tempo: float, librosa_tempo: float) -> tuple[float, str, bool]:
    """
    Compare two tempo estimates, octave-aware (a tempo detector reporting
    half or double the true BPM is the single most common failure mode).
    Returns (chosen_tempo, tempo_source, agree).
    """
    candidates = [librosa_tempo, librosa_tempo * 2, librosa_tempo / 2]
    closest = min(candidates, key=lambda t: abs(t - recco_tempo))
    agree = abs(closest - recco_tempo) <= 4.0  # within 4 BPM after octave correction
    # Prefer ReccoBeats when they agree (trained model, less noise-prone);
    # prefer librosa's raw reading when they don't, since a mismatch usually
    # means one of the two mistook the octave, and librosa can be spot-checked.
    if agree:
        return recco_tempo, "reccobeats", True
    return librosa_tempo, "librosa", False


# ─────────────────────────────────────────────
# Per-track pipeline
# ─────────────────────────────────────────────

def fetch_preview_url(isrc: Optional[str], artist: str, name: str) -> Optional[str]:
    if isrc:
        url = deezer_preview_by_isrc(isrc)
        if url:
            return url
    return itunes_preview(artist, name)


def fetch_features(
    spotify_id: str,
    isrc: Optional[str],
    artist: str,
    name: str,
) -> Optional[AudioFeatures]:
    """
    Full hybrid lookup for one track. Network calls only; does not touch
    the database. Returns None if no source had anything usable.
    """
    from audio.mir_engine import analyse_preview_url  # local import: optional librosa dep

    recco_raw = reccobeats_features(spotify_id)
    preview_url = fetch_preview_url(isrc, artist, name)

    librosa_feats = None
    if preview_url:
        librosa_feats = analyse_preview_url(preview_url, spotify_id)

    if recco_raw and librosa_feats:
        feats = reccobeats_to_audio_features(recco_raw)
        tempo, tempo_source, agree = _reconcile_tempo(feats.tempo, librosa_feats.tempo)
        feats.tempo, feats.tempo_source, feats.tempo_agree = tempo, tempo_source, agree
        feats.source = "hybrid"
        return feats

    if recco_raw:
        return reccobeats_to_audio_features(recco_raw)

    if librosa_feats:
        librosa_feats.source = "librosa"
        librosa_feats.tempo_source = "librosa"
        return librosa_feats

    return None


def fetch_features_for_tracks(tracks: list, delay: float = 0.15) -> dict:
    """
    tracks: list of data.models.Track (id, isrc, artist, name).
    Returns {track_id: AudioFeatures}. Rate-limited with a small delay
    between tracks (both APIs are free/unauthenticated — be polite).
    """
    out = {}
    for i, t in enumerate(tracks, 1):
        feats = fetch_features(t.id, t.isrc, t.artist, t.name)
        if feats:
            out[t.id] = feats
            tag = f"[{feats.source}]"
            if feats.tempo_agree is False:
                tag += " ⚠ tempo mismatch"
        else:
            tag = "[no data]"
        print(f"  [{i}/{len(tracks)}] {t.artist} – {t.name}  {tag}")
        time.sleep(delay)
    return out


# ─────────────────────────────────────────────
# Self-test against the real APIs (run manually, outside any sandbox proxy)
# ─────────────────────────────────────────────

_KNOWN_TRACKS = [
    # (spotify_id, isrc, artist, title) — a few well-known, stable tracks
    ("7qiZfU4dY1lWllzX7mPBI3", "USUM71703861", "Ed Sheeran", "Shape of You"),
    ("0VjIjW4GlUZAMYd2vXMi3b", "USUM71703968", "The Weeknd", "Blinding Lights"),
]


def selftest() -> None:
    ok = True
    for spotify_id, isrc, artist, title in _KNOWN_TRACKS:
        print(f"\n--- {artist} – {title} ---")
        recco = reccobeats_features(spotify_id)
        print("ReccoBeats:", "OK" if recco else "MISS", recco if recco else "")
        preview = deezer_preview_by_isrc(isrc)
        print("Deezer preview:", preview or "MISS (trying iTunes)")
        if not preview:
            preview = itunes_preview(artist, title)
            print("iTunes preview:", preview or "MISS")
        if not recco and not preview:
            ok = False
    print("\nSelf-test:", "PASS — at least one source hit for every track" if ok else
          "FAIL — check network / API changes before running on your library")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        selftest()
    else:
        print("Run with --selftest, or call fetch_features_for_tracks() from analyze.")
