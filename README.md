# Pacesetter

**Terrain-aware marathon playlist generation from your own running and listening data.**

Pacesetter learns which music helps you run well on different terrain. It lines up your Strava runs with your Spotify listening history, scores each song by how you performed while it played, and then builds a Spotify playlist for a target race course, with higher-energy tracks queued just before each climb.

> **Status: early prototype (pre-alpha).** Data ingestion and course analysis work. The audio-feature and model-training stages are written but have not yet produced results on real data. See [Current progress](#current-progress).

---

## How it works

```
 Strava runs ─┐
              ├─► align ─► per-song performance segments ─┐
 Spotify plays┘                                           ├─► train model ─► plan playlist ─► Spotify
                    audio analysis (librosa) ─► BPM,      │         ▲
                    energy, valence, danceability ────────┘         │
                                                      GPX course ─► 50 m terrain segments + hill pre-cues
```

1. **Sync.** Pulls the last 90 days of Strava runs (pace, heart rate, cadence, elevation) and your Spotify recently-played and liked tracks.
2. **Analyze.** Extracts tempo, energy, valence and danceability from audio with `librosa`. This replaces Spotify's deprecated `/audio-features` endpoint.
3. **Align.** Matches each played song to the stretch of the run it covered and records pace deviation, heart-rate variability and terrain type.
4. **Train.** Fits a preference model that maps runner state (grade, distance covered, HR, pace stress, cadence, segment type) to the audio profile that worked best. It uses k-NN below 20 samples and per-feature gradient boosting above that.
5. **Plan.** Splits a GPX course into 50 m segments, assigns goal paces adjusted for effort, places pre-cues about 15 s before each climb, and fills the course with tracks from your library.

## Project structure

| Path | Purpose |
|---|---|
| `main.py` | CLI entry point (`sync`, `analyze`, `train`, `plan`, `stats`) |
| `data/strava_client.py` | Strava OAuth and activity/stream fetching |
| `data/spotify_client.py` | Spotify history, liked songs, playlist creation |
| `data/map_client.py` | GPX parsing and Open-Elevation lookups |
| `data/database.py`, `data/models.py` | SQLite storage and domain dataclasses |
| `audio/mir_engine.py` | Local music information retrieval with librosa |
| `processing/alignment.py` | Song ↔ workout time alignment |
| `processing/course_analyzer.py` | Course segmentation, pacing, hill pre-cues |
| `processing/features.py` | State/action vectors and reward function |
| `ml/preference_model.py` | k-NN / gradient-boosting preference model |
| `ml/playlist_generator.py` | Slot-filling playlist builder with discovery mix |
| `cloud/api/plays.py` | Vercel function: hourly Spotify play logger (Redis buffer) |
| `data/play_log.py` | Local client for the cloud play buffer |

## Setup

Requires **Python 3.10+** (developed on 3.13).

```bash
git clone https://github.com/<your-username>/pacesetter.git
cd pacesetter
python -m venv .venv
.venv\Scripts\activate          # Windows  (macOS/Linux: source .venv/bin/activate)
pip install -r requirements.txt
```

Credentials:

1. Create a Spotify app at <https://developer.spotify.com/dashboard> with redirect URI `http://localhost:8888/callback`.
2. Create a Strava API app at <https://www.strava.com/settings/api>.
3. Copy `.env.example` to `.env` and fill in both sets of keys.
4. Run the one-time Strava authorization: `python -m data.strava_client --auth`

## Usage

```bash
python main.py sync                                        # pull runs + listening history
python main.py analyze [--local-dir PATH]                  # extract audio features
python main.py train                                       # fit the preference model
python main.py plan course.gpx --goal 3h45m --name "Race"  # build a playlist
python main.py plan course.gpx --goal 3h45m --no-push      # preview without touching Spotify
python main.py stats                                       # model mode + top songs per terrain
python main.py plays                                       # play-logger status, songs per run
python main.py align                                       # re-align plays to runs after analyze
```

## Play logger (listening history)

Spotify only remembers your last 50 plays. A small serverless function in `cloud/` pulls them every hour and holds them in a short-lived Redis buffer. `python main.py sync` downloads them into the local SQLite `play_log` table and then deletes them from the cloud. Your full history lives only on your machine.

**Storage policy:** plays that don't overlap a Strava run (±10 min) are deleted locally once they're older than 7 days. The 7 days cover runs you haven't uploaded yet. The cloud buffer also drops anything older than 30 days, in case sync isn't run. One play is about 200 bytes.

**Setup (one time):**

1. Vercel → *Add New Project* → import this repo → set **Root Directory** to `cloud`.
2. In that project: *Storage* → *Create* → **Upstash for Redis** (free tier) → connect it to the project. This adds `KV_REST_API_URL` / `KV_REST_API_TOKEN` automatically.
3. Add these environment variables to the project:
   - `SPOTIFY_CLIENT_ID`, `SPOTIFY_CLIENT_SECRET` (same as `.env`)
   - `SPOTIFY_REFRESH_TOKEN`: print it with `python -m data.play_log --refresh-token`
   - `CRON_SECRET`: any long random string
4. Redeploy. Then in GitHub → repo *Settings → Secrets → Actions*, add `LOGGER_URL` (your `https://….vercel.app`) and `CRON_SECRET`. The workflow in `.github/workflows/log-plays.yml` calls the logger hourly. A daily Vercel cron is included as a backup.
5. Add `PACESETTER_LOGGER_URL` and `PACESETTER_LOGGER_SECRET` to your local `.env`.
6. Test: `python -m data.play_log --log-now`, then `python main.py plays`.

`python main.py plays` shows how many songs were captured for each recent run and warns if the logger may have missed plays.

## Dashboard (web front end)

`cloud/` also serves a password-protected dashboard: run map with the songs laid over the route (streets or topographic basemap), elevation and pace profiles with song bands, listening stats, music-vs-pace, and an interactive view of the k-NN preference model.

It also has a Philadelphia Marathon section (Nov 22, 2026): a countdown, a Riegel-projected finish time, and a training plan built in the browser from your logged runs. The plan has four runs a week, adds no more than 3.2 km a week to the long run up to a 32 km cap, includes a mid-build cutback, and finishes with a two-week taper. It recalculates whenever the snapshot updates. You can edit the plan on the page: goal time, runs per week (3–5), long-run day, peak cap, and marking each session done, skipped or moved. `POST /api/dash` validates those settings and saves them in Redis (`pacesetter:plan`), so every device shows the same plan.

| File | Purpose |
|---|---|
| `cloud/index.html`, `cloud/app.js` | Front end (Leaflet + hand-built SVG charts) |
| `cloud/api/dash.py` | Password-gated data API; merges live plays from the logger's Redis buffer |
| `scripts/export_dashboard.py` | Builds the snapshot of runs/tracks/plays from `pacesetter.db` |

1. Add `DASHBOARD_PASSWORD` to the Vercel project's environment variables.
2. `python scripts/export_dashboard.py` writes `cloud/api/_snapshot.json.gz` (git-ignored, because it holds GPS routes). Deploy with `vercel --prod` from `cloud/` to bundle it.
3. Once Upstash is connected, add `KV_REST_API_URL` / `KV_REST_API_TOKEN` to your local `.env`, then run `python scripts/export_dashboard.py --push` after each `sync`. The dashboard picks up the new snapshot within a minute, with no redeploy.

## Current progress

| Stage | Status | Notes |
|---|---|---|
| Strava sync | ✅ Working | 14 runs ingested |
| Spotify liked-songs sync | ✅ Working | ~1,200 tracks ingested |
| GPX course analysis + pre-cues | ✅ Implemented | 50 m segments, 200 m grade smoothing, cached profiles |
| Audio feature extraction | ⚠️ Blocked | 0 of ~1,200 tracks have a `preview_url`, so `analyze` has nothing to process |
| Song ↔ run alignment | ⚠️ Blocked | Needs audio features; 0 aligned segments so far |
| Model training | ⏳ Not yet run | Waiting on aligned segments |
| Playlist generation / push | ⏳ Untested end-to-end | Depends on trained model and featured library |

### Known issues

- **Spotify preview clips are unavailable.** Spotify no longer returns `preview_url` for most apps, which cuts off the main audio source for `analyze`. The rest of the pipeline waits on this.
- **Recently-played history is shallow.** Spotify's recently-played endpoint only covers a short window, so older runs can't be matched to music.
- **Documentation drift.** `main.py` still describes the model as a LinUCB bandit; it is now k-NN / gradient boosting. `STATE_DIM` is set to 12, but the vector has 11 components.
- **No tests yet.**

## Roadmap

- [ ] Replace the audio source: local-library scanning (`--local-dir`) as the main path, plus a third-party BPM/key metadata source as a fallback
- [x] Log Spotify plays continuously so alignment has more history than the API window gives (`cloud/`, needs deploying)
- [ ] Run the first end-to-end training pass and check the reward signal
- [ ] Fix documentation drift and the `STATE_DIM` mismatch
- [ ] Add unit tests for alignment, course segmentation and goal-time parsing
- [ ] Add a sample GPX course and a demo mode that runs without API keys

## Privacy

API keys (`.env`), OAuth token caches, the local SQLite database and trained model files are excluded by `.gitignore` and are never committed. Each user runs Pacesetter against their own accounts.

## Author

Reid Van Trieste
