"""
Pacesetter dashboard data API — Vercel serverless function (Python, stdlib only).

  GET  /api/dash     Authorization: Bearer <DASHBOARD_PASSWORD>
  POST /api/dash     same auth; body {"plan": {...}} saves training-plan settings

Returns the latest snapshot of runs/tracks/plays plus any plays the cloud
logger has buffered in Redis since that snapshot. The snapshot comes from
Redis (key pacesetter:snapshot, uploaded by `scripts/export_dashboard.py --push`)
when present, otherwise from the copy bundled at deploy time (_snapshot.json.gz).
Nothing is served without the password; the snapshot is never a static file.

Training-plan settings (goal time, runs per week, long-run day, peak cap,
per-session done/skip/move) live in Redis under pacesetter:plan so every
device sees the same plan. They're validated here before saving.

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
import re
import time
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler
from pathlib import Path

SNAPSHOT = Path(__file__).with_name("_snapshot.json.gz")
PLAYS_KEY = "pacesetter:plays"
META_KEY = "pacesetter:meta"
SNAP_KEY = "pacesetter:snapshot"   # base64(gzip(json))
PLAN_KEY = "pacesetter:plan"
MAX_BODY = 32_000

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


def _plan() -> dict | None:
    try:
        res = _redis(["GET", PLAN_KEY])
        return json.loads(res[0]) if res and res[0] else None
    except Exception:  # noqa: BLE001 — the dashboard falls back to defaults
        return None


_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}\|(q|e[1-3]|long|rw[1-3])$")


def _clean_plan(p: object) -> dict:
    """Validate plan settings; raises ValueError on anything unexpected."""
    if not isinstance(p, dict):
        raise ValueError("plan must be an object")
    goal = p.get("goal_sec")
    if goal is not None and not (isinstance(goal, (int, float)) and 7200 <= goal <= 25200):
        raise ValueError("goal_sec must be between 2:00:00 and 7:00:00")
    rpw = p.get("runs_per_week", 4)
    if rpw not in (3, 4, 5):
        raise ValueError("runs_per_week must be 3, 4 or 5")
    long_day = p.get("long_day", "sun")
    if long_day not in ("sat", "sun"):
        raise ValueError("long_day must be sat or sun")
    cap = p.get("peak_cap_km", 32)
    if not (isinstance(cap, (int, float)) and 16 <= cap <= 36):
        raise ValueError("peak_cap_km must be between 16 and 36")
    sessions = p.get("sessions") or {}
    if not isinstance(sessions, dict) or len(sessions) > 300:
        raise ValueError("sessions must be an object with at most 300 entries")
    out_s = {}
    for k, v in sessions.items():
        if not _DAY.match(str(k)) or not isinstance(v, dict):
            raise ValueError(f"bad session key {str(k)[:40]}")
        status, move = v.get("status"), v.get("move")
        if status not in (None, "done", "skipped"):
            raise ValueError("status must be done or skipped")
        if move is not None and move not in range(7):
            raise ValueError("move must be a weekday index 0-6")
        entry = {k2: v2 for k2, v2 in (("status", status), ("move", move)) if v2 is not None}
        if entry:
            out_s[k] = entry
    return {"goal_sec": goal, "runs_per_week": rpw, "long_day": long_day,
            "peak_cap_km": cap, "sessions": out_s, "updated": int(time.time() * 1000)}


class handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: object) -> None:
        data = json.dumps(body, separators=(",", ":")).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _authed(self) -> bool:
        pw = os.environ.get("DASHBOARD_PASSWORD", "")
        got = self.headers.get("Authorization", "")
        if not pw or not hmac.compare_digest(got.encode(), f"Bearer {pw}".encode()):
            time.sleep(0.6)  # slow down guessing
            self._send(401, {"error": "unauthorized"})
            return False
        return True

    def do_GET(self) -> None:
        if not self._authed():
            return
        try:
            snap, src = _snapshot()
            last_ms = max((p[1] for p in snap["plays"]), default=0)
            return self._send(200, {**snap, "source": src, "live": _live(last_ms), "plan": _plan()})
        except Exception as e:  # noqa: BLE001
            return self._send(500, {"error": str(e)[:300]})

    def do_POST(self) -> None:
        if not self._authed():
            return
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if n <= 0 or n > MAX_BODY:
                return self._send(413, {"error": "body missing or too large"})
            plan = _clean_plan(json.loads(self.rfile.read(n)).get("plan"))
        except (ValueError, AttributeError) as e:
            return self._send(400, {"error": str(e)[:200]})
        try:
            if _redis(["SET", PLAN_KEY, json.dumps(plan, separators=(",", ":"))]) is None:
                return self._send(503, {"error": "storage not configured (KV_REST_API_URL / KV_REST_API_TOKEN)"})
        except Exception as e:  # noqa: BLE001
            return self._send(502, {"error": f"couldn't save: {str(e)[:200]}"})
        return self._send(200, {"plan": plan})
