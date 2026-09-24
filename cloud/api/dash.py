"""
Pacesetter dashboard data API — Vercel serverless function (Python, stdlib only).

  GET /api/dash      Authorization: Bearer <DASHBOARD_PASSWORD>

Returns the latest snapshot of runs/tracks/plays plus any plays the cloud
logger has buffered in Redis since that snapshot. The snapshot comes from
Redis (key pacesetter:snapshot, uploaded by `scripts/export_dashboard.py --push`)
when present, otherwise from the copy bundled at deploy time (_snapshot.json.gz).
Nothing is served without the password; the snapshot is never a static file.

Env vars:
  DASHBOARD_PASSWORD                  required
  KV_REST_API_URL / KV_REST_API_TOKEN optional; when present, live plays are merged in
"""

from __future__ import annotations

import base64
import gzip
import hmac
import json
import os
import time
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler
from pathlib import Path

SNAPSHOT = Path(__file__).with_name("_snapshot.json.gz")
PLAYS_KEY = "pacesetter:plays"
META_KEY = "pacesetter:meta"
SNAP_KEY = "pacesetter:snapshot"   # base64(gzip(json))

_cache: dict = {}


def _snapshot() -> tuple[dict, str]:
    """Pushed snapshot from Redis if there is one (cached 60 s), else the bundled file."""
    now = time.time()
    if _cache.get("at", 0) > now - 60:
        return _cache["snap"], _cache["src"]
    snap, src = None, "bundled"
    try:
        res = _redis(["GET", SNAP_KEY])
        if res and res[0]:
            snap, src = json.loads(gzip.decompress(base64.b64decode(res[0]))), "redis"
    except Exception:  # noqa: BLE001 — fall back to the bundled copy
        pass
    if snap is None:
        if not SNAPSHOT.exists():
            raise RuntimeError("No snapshot yet: run `python scripts/export_dashboard.py --push`")
        snap = json.loads(gzip.decompress(SNAPSHOT.read_bytes()))
    _cache.update(snap=snap, src=src, at=now)
    return snap, src


def _redis(*cmds: list) -> list | None:
    url = os.environ.get("KV_REST_API_URL") or os.environ.get("UPSTASH_REDIS_REST_URL")
    tok = os.environ.get("KV_REST_API_TOKEN") or os.environ.get("UPSTASH_REDIS_REST_TOKEN")
    if not url or not tok:
        return None
    req = urllib.request.Request(
        f"{url.rstrip('/')}/pipeline",
        data=json.dumps([list(map(str, c)) for c in cmds]).encode(),
        headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=8) as r:
        return [item.get("result") for item in json.loads(r.read())]


def _to_ms(played_at: str) -> int:
    return int(datetime.fromisoformat(played_at.replace("Z", "+00:00")).timestamp() * 1000)


def _live(since_ms: int) -> dict:
    try:
        res = _redis(
            ["ZRANGEBYSCORE", PLAYS_KEY, since_ms + 1, "+inf"],
            ["HGETALL", META_KEY],
        )
    except Exception as e:  # noqa: BLE001
        return {"connected": False, "error": str(e)[:200], "plays": []}
    if res is None:
        return {"connected": False, "error": "logger not configured", "plays": []}
    raw, meta = res
    plays = []
    for m in raw or []:
        p = json.loads(m)
        plays.append([p["track_id"], _to_ms(p["played_at"]), p["duration_ms"],
                      p.get("name"), p.get("artist")])
    meta_d = dict(zip(meta[::2], meta[1::2])) if meta else {}
    return {"connected": True, "plays": plays, "meta": meta_d}


class handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: object) -> None:
        data = json.dumps(body, separators=(",", ":")).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        pw = os.environ.get("DASHBOARD_PASSWORD", "")
        got = self.headers.get("Authorization", "")
        if not pw or not hmac.compare_digest(got.encode(), f"Bearer {pw}".encode()):
            time.sleep(0.6)  # slow down guessing
            return self._send(401, {"error": "unauthorized"})
        try:
            snap, src = _snapshot()
            last_ms = max((p[1] for p in snap["plays"]), default=0)
            return self._send(200, {**snap, "source": src, "live": _live(last_ms)})
        except Exception as e:  # noqa: BLE001
            return self._send(500, {"error": str(e)[:300]})
