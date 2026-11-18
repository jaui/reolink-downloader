#!/usr/bin/env python3
"""
Ereignis-Viewer: kleine Flask-Web-App zum Filtern und Durchsehen der erkannten
Personen und Fahrzeuge (Ergebnis der Stufe-1-Erkennung) mit Kennzeichen-Lesungen.

Erwartete Daten unter --det-dir (Standard: <output_dir>/detections):
    <kamera>/events.jsonl      eine Zeile je Track (Personen/Fahrzeuge, Sub-Stream)
    <kamera>/crops/*.jpg       bester Bildausschnitt je Track
    plates/tracks.jsonl        Kennzeichen-Lesungen aus dem Main-Stream (optional)
    plates/veh/*.jpg, plates/plates/*.png
Videos kommen aus der Download-Ablage: <output_dir>/<name>_sub_<host>/<datum>/<datei>.mp4
"""

import argparse
import json
import re
import secrets
import socket
import sys
import threading
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

from flask import Flask, abort, jsonify, render_template, request, send_from_directory

from timelapse import FILENAME_RE, parse_video_start

SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_FILE = SCRIPT_DIR / "config.json"
WATCHLIST_FILE = SCRIPT_DIR / "watchlist.json"

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
VEHICLE_CLASSES = {"car", "bus", "truck"}
MOVING_MIN_PATH = 0.05      # normierte Verschiebung des Box-Mittelpunkts
NEAR_MIN_AREA = 0.02        # Box-Anteil am Bild für "nahe" Fahrzeuge
MATCH_TOLERANCE_S = 10      # Main- und Sub-Dateinamen weichen ca. 2 s ab

app = Flask(__name__)
CFG: dict = {}              # wird in main() gesetzt


# ---------------------------------------------------------------------------
# Konfiguration / Watchlist
# ---------------------------------------------------------------------------

def load_config() -> dict:
    with open(CONFIG_FILE, encoding="utf-8") as f:
        return json.load(f)


def norm_plate(text: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", text.upper())


def load_watchlist() -> list[dict]:
    """watchlist.json: {"AB CD 123": "Fahrzeugname", ...}"""
    if not WATCHLIST_FILE.exists():
        return []
    with open(WATCHLIST_FILE, encoding="utf-8") as f:
        data = json.load(f)
    out = []
    for plate, label in data.items():
        parts = plate.upper().split()
        tail = norm_plate("".join(parts[1:])) if len(parts) > 1 else ""
        out.append({"plate": plate, "norm": norm_plate(plate), "tail": tail, "label": label})
    return out


def levenshtein(a: str, b: str) -> int:
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def match_watchlist(text: str, watchlist: list[dict]) -> dict | None:
    """exakt > teil (Teil hinter dem Ortskürzel) > fuzzy (Levenshtein 1)."""
    for kind in ("exakt", "teil", "fuzzy"):
        for w in watchlist:
            if kind == "exakt" and text == w["norm"]:
                hit = True
            elif kind == "teil":
                hit = len(w["tail"]) >= 4 and w["tail"] in text
            elif kind == "fuzzy":
                hit = len(text) >= 6 and levenshtein(text, w["norm"]) <= 1
            else:
                hit = False
            if hit:
                return {"plate": w["plate"], "label": w["label"], "type": kind}
    return None


# ---------------------------------------------------------------------------
# Daten laden
# ---------------------------------------------------------------------------

def video_dir(camera: str, stream: str) -> Path | None:
    for cam in CFG["config"].get("cameras", []):
        if cam["name"] == camera:
            sub = "_sub" if stream == "sub" else ""
            return CFG["output_dir"] / f"{cam['name']}{sub}_{cam['host']}"
    return None


def video_url(camera: str, date: str, name: str) -> str | None:
    """Sub-Clip bevorzugt (H.264, läuft im Browser); sonst Main-Clip."""
    for stream in ("sub", "main"):
        d = video_dir(camera, stream)
        if d and (d / date / name).exists():
            return f"/video/{camera}/{stream}/{date}/{name}"
    return None


def iter_jsonl(path: Path):
    if not path.exists():
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue  # Zeile wird evtl. gerade geschrieben


def plate_summary(reads: list[dict], watchlist: list[dict]) -> dict | None:
    """Mehrheitslesung gewichtet nach Konfidenz."""
    if not reads:
        return None
    score: dict[str, float] = defaultdict(float)
    count: dict[str, int] = defaultdict(int)
    best: dict[str, dict] = {}
    for r in reads:
        t = norm_plate(r.get("text", ""))
        if len(t) < 4:
            continue
        score[t] += r.get("conf", 0)
        count[t] += 1
        if t not in best or r.get("conf", 0) > best[t].get("conf", 0):
            best[t] = r
    if not score:
        return None
    ranked = sorted(score, key=score.get, reverse=True)
    text = ranked[0]
    match = match_watchlist(text, watchlist)
    if match is None:  # Nebenlesung mit Watchlist-Treffer?
        for alt in ranked[1:]:
            m = match_watchlist(alt, watchlist)
            if m and m["type"] != "fuzzy":
                match = dict(m, type="neben")
                break
    return {
        "text": text,
        "conf": round(best[text].get("conf", 0), 3),
        "n_reads": count[text],
        "n_total": sum(count.values()),
        "alternatives": [{"text": t, "n": count[t]} for t in ranked[1:4]],
        "match": match,
        "img": f"/plates/plates/{best[text]['png']}" if best[text].get("png") else None,
    }


def track_entry(camera: str, tr: dict) -> dict:
    cls = tr.get("cls", "?")
    kind = "person" if cls == "person" else "vehicle" if cls in VEHICLE_CLASSES else "other"
    area = tr.get("max_area", 0)
    path = tr.get("path", 0)
    return {
        "kind": kind,
        "cls": cls,
        "t_first": tr.get("t_first", 0),
        "t_last": tr.get("t_last", tr.get("t_first", 0)),
        "best_t": tr.get("best_t", tr.get("t_first", 0)),
        "area": round(area, 4),
        "path": round(path, 3),
        "moving": path >= MOVING_MIN_PATH,
        "near": kind == "person" or area >= NEAR_MIN_AREA,
        "conf": tr.get("max_conf"),
        "night": bool(tr.get("ir")),
        "box": tr.get("box"),
        "crop": f"/crops/{camera}/{tr['crop']}" if tr.get("crop") else None,
        "plate": None,
    }


def build_events() -> dict:
    det_dir: Path = CFG["det_dir"]
    watchlist = load_watchlist()
    events: dict[str, dict] = {}

    # Stufe 1: Sub-Stream-Erkennung je Kamera
    for cam_dir in sorted(p for p in det_dir.iterdir() if p.is_dir() and p.name != "plates"):
        camera = cam_dir.name
        for tr in iter_jsonl(cam_dir / "events.jsonl"):
            rel = tr.get("video", "")
            start = parse_video_start(Path(rel))
            if start is None:
                continue
            name = Path(rel).name
            eid = f"{camera}/{Path(rel).stem}"
            ev = events.get(eid)
            if ev is None:
                ev = events[eid] = {
                    "id": eid, "camera": camera, "clip_start": start, "source": "sub",
                    "date": start.strftime("%Y-%m-%d"), "file": name,
                    "video": video_url(camera, start.strftime("%Y-%m-%d"), name),
                    "tracks": [],
                }
            ev["tracks"].append(track_entry(camera, tr))

    # Kennzeichen (Main-Stream) zuordnen; ohne passenden Sub-Clip eigenes Ereignis
    plates_cam = CFG["plates_camera"]
    by_cam_time = defaultdict(list)
    for ev in events.values():
        by_cam_time[ev["camera"]].append(ev)
    for tr in iter_jsonl(det_dir / "plates" / "tracks.jsonl"):
        start = parse_video_start(Path(tr.get("video", "")))
        if start is None:
            continue
        plate = plate_summary(tr.get("reads", []), watchlist)
        candidates = [e for e in by_cam_time[plates_cam]
                      if abs((e["clip_start"] - start).total_seconds()) <= MATCH_TOLERANCE_S]
        entry = track_entry(plates_cam, tr)
        entry["crop"] = f"/plates/veh/{tr['veh_jpg']}" if tr.get("veh_jpg") else None
        entry["color"] = tr.get("color")
        entry["plate"] = plate
        if candidates:
            ev = min(candidates, key=lambda e: abs((e["clip_start"] - start).total_seconds()))
            offset = (start - ev["clip_start"]).total_seconds()
            vehicles = [t for t in ev["tracks"] if t["kind"] == "vehicle"]
            if vehicles:
                # Sub-Track mit größter zeitlicher Überlappung bekommt das Kennzeichen
                def overlap(t):
                    a0, a1 = tr.get("t_first", 0) + offset, tr.get("t_last", 0) + offset
                    return min(a1, t["t_last"]) - max(a0, t["t_first"]), t["area"]
                target = max(vehicles, key=overlap)
                if plate and (target["plate"] is None or (plate["match"] and not target["plate"]["match"])):
                    target["plate"] = plate
                    target["plate_crop"] = entry["crop"]
                continue
            # Sub-Clip ohne Fahrzeug: Track übernehmen (Zeit auf den Sub-Clip verschieben)
            for k in ("t_first", "t_last", "best_t"):
                entry[k] = round(entry[k] + offset, 1)
            ev["tracks"].append(entry)
            continue
        name = Path(tr["video"]).name
        eid = f"{plates_cam}/{Path(name).stem}"
        ev = events.get(eid)
        if ev is None:
            ev = events[eid] = {
                "id": eid, "camera": plates_cam, "clip_start": start, "source": "main",
                "date": start.strftime("%Y-%m-%d"), "file": name,
                "video": video_url(plates_cam, start.strftime("%Y-%m-%d"), name),
                "tracks": [],
            }
            by_cam_time[plates_cam].append(ev)
        ev["tracks"].append(entry)

    out = []
    for ev in events.values():
        tracks = sorted(ev["tracks"], key=lambda t: t["t_first"])
        t0 = min(t["t_first"] for t in tracks)
        t1 = max(t["t_last"] for t in tracks)
        start = ev["clip_start"] + timedelta(seconds=t0)
        out.append({
            **ev,
            "tracks": tracks,
            "clip_start": ev["clip_start"].isoformat(),
            "start": start.isoformat(),
            "time": start.strftime("%H:%M:%S"),
            "duration": round(t1 - t0, 1),
            "night": sum(t["night"] for t in tracks) * 2 >= len(tracks),
        })
    out.sort(key=lambda e: e["start"])
    return {
        "generated": datetime.now().isoformat(timespec="seconds"),
        "cameras": sorted({e["camera"] for e in out}),
        "watchlist": [{"plate": w["plate"], "label": w["label"]} for w in watchlist],
        "events": out,
    }


_cache = {"key": None, "data": None}
_cache_lock = threading.Lock()


def cache_key() -> tuple:
    det_dir: Path = CFG["det_dir"]
    files = [WATCHLIST_FILE, det_dir / "plates" / "tracks.jsonl"]
    files += [p / "events.jsonl" for p in det_dir.iterdir() if p.is_dir()]
    # Videos tauchen nach und nach auf -> grob mit einbeziehen (Minutentakt)
    return tuple((str(f), f.stat().st_mtime_ns if f.exists() else 0) for f in files) + (
        int(datetime.now().timestamp() // 60),)


def get_events() -> dict:
    with _cache_lock:
        key = cache_key()
        if key != _cache["key"]:
            _cache["data"] = build_events()
            _cache["key"] = key
        return _cache["data"]


# ---------------------------------------------------------------------------
# Zugriffsschutz (nur im LAN-Modus)
# ---------------------------------------------------------------------------

@app.before_request
def check_token():
    token = CFG.get("token")
    if not token:
        return None
    if request.args.get("token") == token or request.cookies.get("viewer_token") == token:
        return None
    abort(403)


@app.after_request
def set_token_cookie(resp):
    token = CFG.get("token")
    if token and request.args.get("token") == token:
        resp.set_cookie("viewer_token", token, httponly=True, samesite="Strict", max_age=30 * 86400)
    return resp


# ---------------------------------------------------------------------------
# Routen
# ---------------------------------------------------------------------------

@app.get("/")
def index():
    return render_template("index.html")


@app.get("/api/events")
def api_events():
    resp = jsonify(get_events())
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.get("/crops/<camera>/<path:filename>")
def crops(camera, filename):
    cam_dir = CFG["det_dir"] / camera
    if camera == "plates" or not (cam_dir / "events.jsonl").exists():
        abort(404)
    return send_from_directory(cam_dir / "crops", filename, max_age=86400)


@app.get("/plates/<kind>/<path:filename>")
def plates(kind, filename):
    if kind not in ("veh", "plates"):
        abort(404)
    return send_from_directory(CFG["det_dir"] / "plates" / kind, filename, max_age=86400)


@app.get("/video/<camera>/<stream>/<date>/<filename>")
def video(camera, stream, date, filename):
    if stream not in ("sub", "main") or not DATE_RE.match(date) or not FILENAME_RE.fullmatch(filename):
        abort(404)
    d = video_dir(camera, stream)
    if d is None:
        abort(404)
    # send_from_directory: Schutz gegen Pfad-Traversal, Range-Anfragen (206) für das Springen
    return send_from_directory(d / date, filename, mimetype="video/mp4", conditional=True, max_age=3600)


# ---------------------------------------------------------------------------

def lan_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.168.1.1", 9))
            return s.getsockname()[0]
    except OSError:
        return "localhost"


def main():
    parser = argparse.ArgumentParser(description="Ereignis-Viewer für erkannte Personen und Fahrzeuge")
    parser.add_argument("--port", type=int, default=8765, help="Port (Standard: 8765)")
    parser.add_argument("--host", default="127.0.0.1",
                        help="Adresse (Standard: 127.0.0.1; 0.0.0.0 für Zugriff im LAN, dann mit Token)")
    parser.add_argument("--det-dir", type=Path, default=None,
                        help="Ordner mit den Erkennungsdaten (Standard: <output_dir>/detections)")
    parser.add_argument("--plates-camera", default="garage2",
                        help="Kamera, zu der plates/tracks.jsonl gehört (Standard: garage2)")
    args = parser.parse_args()

    config = load_config()
    output_dir = Path(config.get("output_dir", "downloads"))
    if not output_dir.is_absolute():
        output_dir = SCRIPT_DIR / output_dir
    det_dir = (args.det_dir or output_dir / "detections").resolve()
    if not det_dir.is_dir():
        print(f"Fehler: Erkennungsordner {det_dir} nicht gefunden.", file=sys.stderr)
        sys.exit(1)

    CFG.update(config=config, output_dir=output_dir, det_dir=det_dir, plates_camera=args.plates_camera)
    local = args.host in ("127.0.0.1", "localhost", "::1")
    if not local:
        CFG["token"] = secrets.token_urlsafe(16)

    data = get_events()
    print(f"Erkennungsdaten: {det_dir}")
    print(f"{len(data['events'])} Ereignisse von {', '.join(data['cameras']) or '-'}"
          f"{'' if WATCHLIST_FILE.exists() else ' (keine watchlist.json)'}")
    if local:
        print(f"Viewer: http://{args.host}:{args.port}/")
    else:
        print("ACHTUNG: Viewer ist im Netzwerk erreichbar, Zugriff nur mit Token.")
        print(f"Viewer: http://{lan_ip()}:{args.port}/?token={CFG['token']}")
    print("Beenden mit Ctrl+C.")
    app.run(host=args.host, port=args.port, threaded=True, debug=False)


if __name__ == "__main__":
    main()
