"""
Pacesetter play logger — Vercel serverless function (Python, stdlib only).

Spotify's recently-played endpoint only remembers the last 50 plays. This
function is hit on a schedule (hourly), grabs those 50 and stores them in an
Upstash Redis sorted set (score = played_at in unix ms). Duplicates collapse
automatically, so overlapping pulls are harmless.

The cloud copy is only a short-lived buffer: `python main.py sync` downloads
the plays to your local SQLite database and then purges them here.

Routes (all require  Authorization: Bearer <CRON_SECRET>):
  GET    /api/log                      pull latest 50 plays from Spotify → store
  GET    /api/plays?since=<unix_ms>    list buffered plays (JSON)
  GET    /api/plays?action=status      count / newest / oldest / gap warning
  DELETE /api/plays?before=<unix_ms>   purge plays with played_at <= before

Env vars (set in the Vercel project):
  SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET, SPOTIFY_REFRESH_TOKEN
  CRON_SECRET                         shared secret (Vercel cron sends it too)
  KV_REST_API_URL / KV_REST_API_TOKEN (added by the Upstash integration;
  UPSTASH_REDIS_REST_URL / UPSTASH_REDIS_REST_TOKEN also accepted)
  BUFFER_RETENTION_DAYS               optional, default 30
"""

from __future__ import annotations

import base64
import hmac
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler

PLAYS_KEY = "pacesetter:plays"
TOKEN_KEY = "pacesetter:spotify_refresh_token"
META_KEY = "pacesetter:meta"


# ─────────────────────────────────────────────
# Upstash Redis (REST)
# ─────────────────────────────────────────────

def _redis_cfg() -> tuple[str, str]:
    url = os.environ.get("KV_REST_API_URL") or os.environ.get("UPSTASH_REDIS_REST_URL")
    tok = os.environ.get("KV_REST_API_TOKEN") or os.environ.get("UPSTASH_REDIS_REST_TOKEN")
    if not url or not tok:
        raise RuntimeError("Redis env vars missing (KV_REST_API_URL / KV_REST_API_TOKEN)")
    return url.rstrip("/"), tok


def redis(*cmds: list) -> list:
    """Run one or more Redis commands in a single pipeline call."""
    url, tok = _redis_cfg()
    req = urllib.request.Request(
        f"{url}/pipeline",
        data=json.dumps([list(map(str, c)) for c in cmds]).encode(),
        headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        out = json.loads(r.read())
    for item in out:
        if "error" in item:
            raise RuntimeError(f"Redis error: {item['error']}")
    return [item.get("result") for item in out]


# ─────────────────────────────────────────────
# Spotify
# ─────────────────────────────────────────────

def _spotify_access_token() -> str:
    stored = redis(["GET", TOKEN_KEY])[0]
    refresh = stored or os.environ["SPOTIFY_REFRESH_TOKEN"]
    basic = base64.b64encode(
        f"{os.environ['SPOTIFY_CLIENT_ID']}:{os.environ['SPOTIFY_CLIENT_SECRET']}".encode()
    ).decode()
    req = urllib.request.Request(
        "https://accounts.spotify.com/api/token",
        data=urllib.parse.urlencode(
            {"grant_type": "refresh_token", "refresh_token": refresh}
        ).encode(),
        headers={"Authorization": f"Basic {basic}",
                 "Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        body = json.loads(r.read())
    # Spotify may rotate the refresh token; keep the newest one.
    new_refresh = body.get("refresh_token")
    if new_refresh and new_refresh != refresh:
        redis(["SET", TOKEN_KEY, new_refresh])
    return body["access_token"]


def _fetch_recent(access_token: str) -> list[dict]:
    req = urllib.request.Request(
        "https://api.spotify.com/v1/me/player/recently-played?limit=50",
        headers={"Authorization": f"Bearer {access_token}"},
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read()).get("items", [])


def _to_ms(played_at: str) -> int:
    return int(datetime.fromisoformat(played_at.replace("Z", "+00:00")).timestamp() * 1000)


def compact(item: dict) -> dict:
    """Keep only what alignment + feature lookup need (~200 bytes per play)."""
    t = item["track"]
    return {
        "track_id": t["id"],
        "played_at": item["played_at"],
        "duration_ms": t["duration_ms"],
        "name": t["name"],
        "artist": ", ".join(a["name"] for a in t.get("artists", [])),
        "album": (t.get("album") or {}).get("name", ""),
        "isrc": (t.get("external_ids") or {}).get("isrc"),
    }


def log_plays() -> dict:
    items = [i for i in _fetch_recent(_spotify_access_token())
             if i.get("track") and i["track"].get("id")]
    newest_before = redis(["ZRANGE", PLAYS_KEY, -1, -1, "WITHSCORES"])[0]
    prev_newest_ms = int(float(newest_before[1])) if newest_before else None

    now_ms = int(time.time() * 1000)
    retention_ms = int(os.environ.get("BUFFER_RETENTION_DAYS", "30")) * 86_400_000

    cmds: list[list] = []
    for it in items:
        rec = compact(it)
        cmds.append(["ZADD", PLAYS_KEY, _to_ms(rec["played_at"]),
                     json.dumps(rec, sort_keys=True, separators=(",", ":"))])
    # Safety net if local sync is never run: drop anything past retention.
    cmds.append(["ZREMRANGEBYSCORE", PLAYS_KEY, "-inf", now_ms - retention_ms])

    # Gap check: if all 50 fetched plays are newer than what we already had,
    # some plays in between may have been missed.
    gap = False
    if items and prev_newest_ms and len(items) >= 50:
        oldest_fetched = min(_to_ms(i["played_at"]) for i in items)
        gap = oldest_fetched > prev_newest_ms
    meta = {"last_run_ms": now_ms, "last_fetched": len(items)}
    if gap:
        meta["last_gap_ms"] = now_ms
    for k, v in meta.items():
        cmds.append(["HSET", META_KEY, k, v])

    results = redis(*cmds)
    added = sum(r for r in results[:len(items)] if isinstance(r, int))
    return {"fetched": len(items), "new": added, "gap_warning": gap}


def list_plays(since_ms: int) -> list[dict]:
    raw = redis(["ZRANGEBYSCORE", PLAYS_KEY, since_ms, "+inf"])[0] or []
    return [json.loads(m) for m in raw]


def purge_plays(before_ms: int) -> int:
    return redis(["ZREMRANGEBYSCORE", PLAYS_KEY, "-inf", before_ms])[0]


def status() -> dict:
    count, oldest, newest, meta = redis(
        ["ZCARD", PLAYS_KEY],
        ["ZRANGE", PLAYS_KEY, 0, 0, "WITHSCORES"],
        ["ZRANGE", PLAYS_KEY, -1, -1, "WITHSCORES"],
        ["HGETALL", META_KEY],
    )
    meta_d = dict(zip(meta[::2], meta[1::2])) if meta else {}
    return {
        "buffered_plays": count,
        "oldest": json.loads(oldest[0])["played_at"] if oldest else None,
        "newest": json.loads(newest[0])["played_at"] if newest else None,
        **meta_d,
    }


# ─────────────────────────────────────────────
# HTTP handler
# ─────────────────────────────────────────────

class handler(BaseHTTPRequestHandler):
    def _authorized(self) -> bool:
        secret = os.environ.get("CRON_SECRET", "")
        got = self.headers.get("Authorization", "")
        return bool(secret) and hmac.compare_digest(got, f"Bearer {secret}")

    def _send(self, code: int, body: object) -> None:
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _query(self) -> dict:
        return dict(urllib.parse.parse_qsl(urllib.parse.urlparse(self.path).query))

    def do_GET(self) -> None:
        if not self._authorized():
            return self._send(401, {"error": "unauthorized"})
        q = self._query()
        try:
            if q.get("action") == "log":
                return self._send(200, log_plays())
            if q.get("action") == "status":
                return self._send(200, status())
            return self._send(200, {"plays": list_plays(int(q.get("since", 0)))})
        except urllib.error.HTTPError as e:
            return self._send(502, {"error": f"upstream {e.code}", "detail": e.read().decode()[:300]})
        except Exception as e:  # noqa: BLE001
            return self._send(500, {"error": str(e)})

    def do_DELETE(self) -> None:
        if not self._authorized():
            return self._send(401, {"error": "unauthorized"})
        q = self._query()
        if "before" not in q:
            return self._send(400, {"error": "before=<unix_ms> required"})
        try:
            return self._send(200, {"purged": purge_plays(int(q["before"]))})
        except Exception as e:  # noqa: BLE001
            return self._send(500, {"error": str(e)})
