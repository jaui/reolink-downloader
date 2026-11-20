# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

Standalone Windows CLI scripts (no package, no tests, no linter configured):

- `reolink-downloader.py` — lists/downloads recordings from Reolink cameras via the local API (`reolink-aio`).
- `timelapse.py` — builds a timelapse MP4 from the downloaded motion-triggered clips using an external ffmpeg binary. Standard library only.
- Detection pipeline: `fetch_clips.py` (per-camera/stream range download) → `detect.py` (stage 1, persons/bikes/vehicles on sub-stream) → `fetch_clips.py --stream main --from-events` → `plates_scan.py` (stage 2, plates on main stream) → `viewer.py` (Flask web UI). `camsettings.py` dumps camera image/night settings (read-only).

The README and all user-facing output in `reolink-downloader.py` are in **German**; `timelapse.py` output/comments are in English. The newer scripts use German output/help. Keep each file's existing language when editing.

Two venvs: the repo `venv` (reolink-aio, keyring, flask) runs `reolink-downloader.py`, `fetch_clips.py`, `camsettings.py`, `viewer.py`; the ALPR venv `c:\localapps\alpr\venv` (CPU torch, ultralytics, fast-alpr, truststore; models in `c:\localapps\alpr\models`) runs `detect.py` and `plates_scan.py`. Both import `FFMPEG`/`FFPROBE`/`FILENAME_RE`/`parse_video_start` from `timelapse.py`, so they must stay in the repo root.

## Commands

```bash
python -m venv venv && venv\Scripts\activate
pip install -r requirements.txt
python reolink-downloader.py --setup-password     # stores password in Windows Credential Manager (keyring service "reolink-downloader")
python reolink-downloader.py -l -d 2026-03-07 --camera cam1   # list only, no download
python reolink-downloader.py -d 2026-03-07                    # download (default with no date: yesterday)
python timelapse.py --input-dir c:\video.in --from 2026-02-12 --to 2026-02-28
```

Running the downloader requires reachable cameras listed in `config.json` and a stored password; there is no offline/mock mode. Prefer `-l` (list) for smoke-testing changes against real cameras.

## Architecture

### reolink-downloader.py
- `config.json` (next to the script) defines `cameras` (name/host/channel), a shared `username`, `output_dir`, and `stream` (`main`/`sub`). The password is never in config — only in keyring.
- Date range resolution in `main_async`: `-d` → one day; `--from/--to` → range; `-l` without date → last 90 days; otherwise yesterday.
- Queries are split per day via `_day_ranges` because the Reolink API caps results at ~200 per `request_vod_files` call.
- Downloads run **in parallel across cameras** (`asyncio.gather`), sequentially within a camera. Listing runs cameras sequentially.
- Ctrl+C sets the module-level `_shutdown` flag; loops check it so the current file finishes cleanly.
- Output layout: `<output>/<cam_name>_<host_ip>/<YYYY-MM-DD>/<YYYY-MM-DD>_<HH-MM-SS>.mp4`. Existing files are skipped unless `--force`.

### timelapse.py
- **Coupled to the downloader's output layout**: relies on `YYYY-MM-DD` folders and the `YYYY-MM-DD_HH-MM-SS.mp4` filename (`FILENAME_RE`) to derive each clip's start timestamp without probing. The camera label (`camN`) for the output filename is regex-extracted from the input path.
- `find_date_root` walks up to 5 levels below `--input-dir` to find the single directory containing date folders; errors if ambiguous (multiple cameras).
- Pipeline: collect first N frames per clip (timestamp = filename time + i/fps via ffprobe) → filter to `--day-start/--day-end` window (default 16:00–21:00) → `select_evenly` picks the nearest frame to evenly spaced target times across the whole span → extract JPGs to a temp dir → ffmpeg encodes.
- Module-level constants at the top act as defaults and are **overwritten as globals in `main()`** from CLI args; helper functions read those globals rather than taking parameters.
- ffmpeg/ffprobe paths are hardcoded to `c:\localapps\ffmpeg\bin\` (`FFMPEG`, `FFPROBE`).
- Output duration defaults to 10 s per processed date unless `--duration` or `--total-frames` is given.

### viewer.py
- Flask app (`requirements-viewer.txt`), templates in `templates/`, vanilla JS/CSS in `static/` (no build step). German UI.
- Reads detection output from `<output_dir>/detections/<camera>/events.jsonl` + `plates/tracks.jsonl`, rebuilt on mtime change; one event per clip, filtering is client-side.
- Plate tracks (main stream) are matched to sub-stream clips by time (±10 s, filenames differ by ~2 s); unmatched ones become their own events.
- Videos served via `send_from_directory` (Range/206 + traversal protection); camera folders derived from `config.json`.
- Manual corrections (class, false positive, vehicle, plate) and static false-positive rules live in `<det-dir>/labels.db` (SQLite, stdlib); they are overlaid on the JSONL data in `apply_labels` (keyed by `track_key` = `<camera|plates>/<clip>/<cls>/<track>`). Detection outputs stay append-only JSONL. Writes are JSON-only POSTs (blocks form CSRF); `/api/events` is gzip-compressed.
- UI drilldown position is the hash key `at` (`all` | `YYYY` | `YYYY-MM` | `YYYY-MM-DD` | `YYYY-MM-DDTHH`; empty = today).
- `watchlist.json` (gitignored) maps plates to labels. Non-localhost `--host` enables an access token, persisted in keyring (`reolink-downloader` / `viewer-token`, renew with `--new-token`).

### Detection pipeline
- Folder layout: sub stream in `<output_dir>/<name>_sub_<host>/`, main in `<name>_<host>/` (same as the downloader); results in `<output_dir>/detections/<camera>/` (stage 1) and `detections/plates/` (stage 2). All resumable via `done.txt`.
- Cameras serve only **one download at a time** (HTTP 503 otherwise) at ~0.8 MB/s; different cameras can run in parallel. `fetch_clips.py` writes `.part` files and skips clips still being recorded.
- `fetch_clips.py` lists all days first, then downloads; progress goes to `<output_dir>/status/<camera>_<stream>.json` (atomic replace, heartbeat every ~2 s). `viewer.py` `/api/status` adds per-day detection counts from `done.txt` and marks jobs "steht" when the heartbeat is older than 90 s.
- Main and sub filenames for the same clip differ by ~2 s → always match by time with ±10 s tolerance.
- `detect.py --classes` takes groups `person`, `bike` (bicycle+motorcycle), `vehicle` (car/bus/truck); the viewer maps classes to a `kind` via `REVIEW_CLASSES` (bike is its own kind, no plates). Output format v2 (`"v": 2`): tracks keyed by (ByteTrack id, class) because ByteTrack associates class-agnostically (a v1 track's `cls` was just its first frame's class); per-track `traj`, fragments merged (`merged`), and a verification stage (`verify`) re-checks all two-wheelers and persons with conf < 0.6 using `yolo11m.pt` on an enlarged crop (thresholds calibrated by visual review, see constants in `detect.py`). Clips processed in v2 are listed in `done_v2.txt`; the viewer ignores v1 lines of those clips and maps label keys via `key_alias`. `--redo-persons` re-runs done clips with persons in v2. The viewer marks tracks `weak` (failed verification, low/short confidence, unverified v1 two-wheelers) and hides them except in the "Fehlalarme" review filter.
- `detect.py --watch SEK` keeps processing newly downloaded clips. Pass folders, not globbed file lists (Windows command-line limit).
- Night: IR overexposes plates, so plate reads are mostly daylight/colour-mode only.
