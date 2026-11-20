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

Nachklassifikation: Korrekturen aus der Oberfläche (Klasse, Fehlalarm, Fahrzeug, Kennzeichen)
und Fehlalarm-Regeln für stehende Objekte landen in <det-dir>/labels.db (SQLite). Die
Erkennungsdateien bleiben unverändert; die Korrekturen werden beim Ausliefern darübergelegt.
"""

import argparse
import gzip
import json
import re
import secrets
import socket
import sqlite3
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
KEYRING_SERVICE = "reolink-downloader"
KEYRING_TOKEN_USER = "viewer-token"

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
MOVING_MIN_PATH = 0.05      # normierte Verschiebung des Box-Mittelpunkts
NEAR_MIN_AREA = 0.02        # Box-Anteil am Bild für "nahe" Fahrzeuge
MATCH_TOLERANCE_S = 10      # Main- und Sub-Dateinamen weichen ca. 2 s ab
RULE_IOU = 0.5              # Fehlalarm-Regel: Mindestüberlappung der Box mit der Regel-Box
WEAK_CONF = 0.4             # Spur-Konfidenz darunter = unsicher (wenn nicht nachgeprüft)
WEAK_SHORT = (3, 0.6)       # weniger Bilder und Spur-Konfidenz darunter = unsicher

# Erkannte bzw. korrigierte Klasse -> Art (person/bike/vehicle/other/fp)
REVIEW_CLASSES = {
    "person": "person", "bicycle": "bike", "motorcycle": "bike",
    "car": "vehicle", "truck": "vehicle", "bus": "vehicle",
    "animal": "other", "fp": "fp",
}


def kind_of(cls: str) -> str:
    return REVIEW_CLASSES.get(cls, "other")


def is_near(kind: str, area: float) -> bool:
    """Nur Autos/LKW brauchen eine Mindestgröße; Personen und Zweiräder zählen immer als nah."""
    return kind != "vehicle" or area >= NEAR_MIN_AREA


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


def read_lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").split() if path.exists() else []


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


def track_key(prefix: str, tr: dict) -> str:
    """Stabiler Schlüssel je Track (für Korrekturen): <kamera|plates>/<clip>/<klasse>/<track-id>."""
    return f"{prefix}/{Path(tr.get('video', '')).stem}/{tr.get('cls', '?')}/{tr.get('track', 0)}"


def weak_reason(tr: dict) -> str | None:
    """Warum eine Spur unsicher ist (wird im Viewer ausgeblendet), sonst None."""
    v = tr.get("verify")
    if v:
        return None if v.get("ok") else f"Prüfung ({v.get('model')}) nicht bestätigt, Konf. {v.get('conf')}"
    cls = tr.get("cls")
    if cls in ("bicycle", "motorcycle"):
        return "Zweirad nicht nachgeprüft (alte Erkennung)"
    if "reads" in tr:          # Kennzeichen-Spur aus dem Main-Stream
        return None
    conf = tr.get("conf_track", tr.get("max_conf") or 0)
    if conf < WEAK_CONF:
        return f"schwache Erkennung, Konf. {conf}"
    if tr.get("n", 99) < WEAK_SHORT[0] and conf < WEAK_SHORT[1]:
        return f"nur {tr.get('n')} Bilder, Konf. {conf}"
    return None


def track_entry(camera: str, tr: dict, key: str) -> dict:
    cls = tr.get("cls", "?")
    kind = kind_of(cls)
    area = tr.get("max_area", 0)
    path = tr.get("path", 0)
    return {
        "key": key,
        "kind": kind,
        "cls": cls,
        "orig_cls": cls,
        "t_first": tr.get("t_first", 0),
        "t_last": tr.get("t_last", tr.get("t_first", 0)),
        "best_t": tr.get("best_t", tr.get("t_first", 0)),
        "area": round(area, 4),
        "path": round(path, 3),
        "moving": path >= MOVING_MIN_PATH,
        "near": is_near(kind, area),
        "conf": tr.get("max_conf"),
        "night": bool(tr.get("ir")),
        "box": tr.get("box"),
        "crop": f"/crops/{camera}/{tr['crop']}" if tr.get("crop") else None,
        "plate": None,
        "weak": weak_reason(tr),
        "verify": tr.get("verify"),
        "merged": tr.get("merged"),
    }


def build_events() -> dict:
    det_dir: Path = CFG["det_dir"]
    watchlist = load_watchlist()
    events: dict[str, dict] = {}

    # Stufe 1: Sub-Stream-Erkennung je Kamera. Clips in done_v2.txt wurden neu ausgewertet:
    # ältere Zeilen dieser Clips ignorieren, ihre Schlüssel für Korrekturen umbiegen (key_alias).
    superseded: dict[str, list] = defaultdict(list)
    for cam_dir in sorted(p for p in det_dir.iterdir() if p.is_dir() and p.name != "plates"):
        camera = cam_dir.name
        v2 = set(read_lines(cam_dir / "done_v2.txt"))
        for tr in iter_jsonl(cam_dir / "events.jsonl"):
            rel = tr.get("video", "")
            if rel in v2 and tr.get("v", 1) < 2:
                superseded[f"{camera}/{Path(rel).stem}"].append(tr)
                continue
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
            ev["tracks"].append(track_entry(camera, tr, track_key(camera, tr)))

    key_alias = {}
    for eid, old in superseded.items():
        new = events.get(eid, {}).get("tracks", [])
        camera = eid.split("/", 1)[0]
        for tr in old:
            same = [t for t in new if t["orig_cls"] == tr.get("cls")]
            if same:
                a0, a1 = tr.get("t_first", 0), tr.get("t_last", 0)
                best = max(same, key=lambda t: (min(a1, t["t_last"]) - max(a0, t["t_first"]),
                                                -abs(t["t_first"] - a0)))
                key_alias[track_key(camera, tr)] = best["key"]

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
        entry = track_entry(plates_cam, tr, track_key("plates", tr))
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
        "rule_iou": RULE_IOU,
        "events": out,
        "key_alias": key_alias,
    }


# ---------------------------------------------------------------------------
# Nachklassifikation (SQLite)
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS labels (
    key      TEXT PRIMARY KEY,      -- track_key()
    camera   TEXT NOT NULL,
    video    TEXT NOT NULL,         -- Clip-Datei, zur Orientierung
    orig_cls TEXT,                  -- Klasse laut Erkennung
    cls      TEXT,                  -- korrigierte Klasse (REVIEW_CLASSES) oder NULL = bestätigt/unverändert
    vehicle  TEXT,                  -- zugeordnetes Watchlist-Kennzeichen
    plate    TEXT,                  -- korrigiertes Kennzeichen (normiert)
    note     TEXT,
    updated  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS rules (  -- Fehlalarm-Regel für stehende Objekte an einer Stelle
    id       INTEGER PRIMARY KEY,
    camera   TEXT NOT NULL,
    cls      TEXT NOT NULL,         -- erkannte Klasse, für die die Regel gilt
    x0 REAL NOT NULL, y0 REAL NOT NULL, x1 REAL NOT NULL, y1 REAL NOT NULL,
    source   TEXT,                  -- Track, aus dem die Regel entstand
    created  TEXT NOT NULL
);
"""

_labels_version = 0                 # wird bei jedem Schreiben erhöht -> Cache neu


def db() -> sqlite3.Connection:
    con = sqlite3.connect(CFG["db_path"], timeout=10)
    con.row_factory = sqlite3.Row
    return con


def init_db() -> None:
    with db() as con:
        con.execute("PRAGMA journal_mode=WAL")
        con.executescript(SCHEMA)


def load_labels() -> tuple[dict, list]:
    with db() as con:
        labels = {r["key"]: dict(r) for r in con.execute("SELECT * FROM labels")}
        rules = [dict(r) for r in con.execute("SELECT * FROM rules ORDER BY id")]
    return labels, rules


def iou(a, b) -> float:
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union > 0 else 0.0


def rule_hit(camera: str, t: dict, rules: list) -> dict | None:
    if t["moving"] or not t.get("box"):
        return None
    for r in rules:
        if r["camera"] == camera and r["cls"] == t["orig_cls"] \
                and iou(t["box"], (r["x0"], r["y0"], r["x1"], r["y1"])) >= RULE_IOU:
            return r
    return None


def labeled_track(camera: str, t: dict, labels: dict, rules: list, watchlist: list) -> dict | None:
    """Korrektur bzw. Regel auf einen Track anwenden; None = unverändert."""
    lab = labels.get(t["key"])
    rule = None if lab and lab["cls"] else rule_hit(camera, t, rules)
    if not lab and not rule:
        return None
    t = dict(t)
    if lab and lab["cls"]:
        t["weak"] = None        # von Hand eingeordnet -> nicht mehr unsicher
    if rule:
        t.update(cls="fp", kind="fp")
        t["review"] = {"by": "rule", "rule": rule["id"]}
    if lab:
        if lab["cls"]:
            t["cls"] = lab["cls"]
            t["kind"] = kind_of(lab["cls"])
            t["near"] = is_near(t["kind"], t["area"])
        t["review"] = {"by": "manual", "cls": lab["cls"], "vehicle": lab["vehicle"],
                       "plate": lab["plate"], "note": lab["note"], "updated": lab["updated"],
                       **({"rule": rule["id"]} if rule else {})}
        base = t["plate"] or {"text": "", "conf": None, "n_reads": 0, "n_total": 0,
                              "alternatives": [], "match": None, "img": None}
        plate = None
        if lab["plate"]:
            plate = dict(base, text=lab["plate"], match=match_watchlist(lab["plate"], watchlist), manual=True)
        if lab["vehicle"]:
            w = next((w for w in watchlist if w["plate"] == lab["vehicle"]), None)
            if w:
                plate = dict(plate or base, match={"plate": w["plate"], "label": w["label"], "type": "manuell"},
                             manual=True)
                plate["text"] = plate["text"] or w["norm"]
        if plate:
            t["plate"] = plate
    return t


def apply_labels(base: dict, labels: dict, rules: list) -> dict:
    watchlist = load_watchlist()
    # Korrekturen an inzwischen neu ausgewerteten Spuren auf die neue Spur übertragen
    for old, new in base.get("key_alias", {}).items():
        if old in labels and new not in labels:
            labels[new] = labels[old]
    events = []
    for ev in base["events"]:
        new = [labeled_track(ev["camera"], t, labels, rules, watchlist) for t in ev["tracks"]]
        if any(new):
            ev = dict(ev, tracks=[n or t for n, t in zip(new, ev["tracks"])])
        events.append(ev)
    out = {k: v for k, v in base.items() if k != "key_alias"}
    return dict(out, events=events, rules=[{**r, "box": [r["x0"], r["y0"], r["x1"], r["y1"]]} for r in rules])


_cache = {"key": None, "base": None, "lv": None, "data": None, "index": {}}
_cache_lock = threading.Lock()


def cache_key() -> tuple:
    det_dir: Path = CFG["det_dir"]
    files = [WATCHLIST_FILE, det_dir / "plates" / "tracks.jsonl"]
    files += [p / name for p in det_dir.iterdir() if p.is_dir() for name in ("events.jsonl", "done_v2.txt")]
    # Videos tauchen nach und nach auf -> grob mit einbeziehen (Minutentakt)
    return tuple((str(f), f.stat().st_mtime_ns if f.exists() else 0) for f in files) + (
        int(datetime.now().timestamp() // 60),)


def get_events() -> dict:
    """Erkennungsdaten (Cache nach mtime) + darübergelegte Korrekturen (Cache nach Version)."""
    with _cache_lock:
        key = cache_key()
        if key != _cache["key"]:
            _cache["base"] = build_events()
            _cache["key"] = key
            _cache["lv"] = None
            _cache["index"] = {t["key"]: (ev["camera"], ev["id"], t)
                               for ev in _cache["base"]["events"] for t in ev["tracks"]}
        if _cache["lv"] != _labels_version:
            _cache["data"] = apply_labels(_cache["base"], *load_labels())
            _cache["lv"] = _labels_version
        return _cache["data"]


def find_track(key: str) -> tuple[str, str, dict]:
    get_events()
    hit = _cache["index"].get(key)
    if hit is None:
        abort(404)
    return hit


# ---------------------------------------------------------------------------
# Zugriffsschutz (nur im LAN-Modus)
# ---------------------------------------------------------------------------

@app.before_request
def check_token():
    token = CFG.get("token")
    if not token:
        return None
    given = request.args.get("token") or request.cookies.get("viewer_token") or ""
    if secrets.compare_digest(given.encode(), token.encode()):
        return None
    abort(403)


@app.after_request
def set_token_cookie(resp):
    token = CFG.get("token")
    if token and request.args.get("token") == token:
        resp.set_cookie("viewer_token", token, httponly=True, samesite="Strict", max_age=30 * 86400)
    # JSON komprimieren (Ereignisliste ~2 MB -> ~0,2 MB, wichtig fürs Handy im LAN)
    if resp.mimetype == "application/json" and not resp.direct_passthrough \
            and "gzip" in request.headers.get("Accept-Encoding", "") \
            and (resp.content_length or 0) > 2048 and "Content-Encoding" not in resp.headers:
        resp.set_data(gzip.compress(resp.get_data(), 5))
        resp.headers["Content-Encoding"] = "gzip"
        resp.headers["Vary"] = "Accept-Encoding"
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


def json_body() -> dict:
    # nur application/json: verhindert, dass fremde Seiten per einfachem Formular-POST schreiben
    if not request.is_json:
        abort(415)
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        abort(400)
    return body


def changed_events(ids: set[str]) -> dict:
    global _labels_version
    _labels_version += 1
    data = get_events()
    return {"events": [e for e in data["events"] if e["id"] in ids], "rules": data["rules"]}


def events_with_rule(rule_id: int) -> set[str]:
    return {ev["id"] for ev in get_events()["events"]
            for t in ev["tracks"] if (t.get("review") or {}).get("rule") == rule_id}


@app.post("/api/label")
def api_label():
    """Korrektur eines Tracks: {key, cls?, vehicle?, plate?, note?} oder {key, reset: true}."""
    body = json_body()
    camera, event_id, t = find_track(str(body.get("key", "")))
    if body.get("reset"):
        with db() as con:
            con.execute("DELETE FROM labels WHERE key = ?", (t["key"],))
        return jsonify(changed_events({event_id}))
    cls = body.get("cls") or None
    if cls is not None and cls not in REVIEW_CLASSES:
        abort(400)
    vehicle = body.get("vehicle") or None
    if vehicle is not None and vehicle not in {w["plate"] for w in load_watchlist()}:
        abort(400)
    plate = norm_plate(str(body.get("plate") or ""))[:12] or None
    note = str(body.get("note") or "")[:500] or None
    with db() as con:
        con.execute(
            """INSERT INTO labels (key, camera, video, orig_cls, cls, vehicle, plate, note, updated)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(key) DO UPDATE SET cls=excluded.cls, vehicle=excluded.vehicle,
                   plate=excluded.plate, note=excluded.note, updated=excluded.updated""",
            (t["key"], camera, event_id.split("/", 1)[1], t["orig_cls"], cls, vehicle, plate, note,
             datetime.now().isoformat(timespec="seconds")))
    return jsonify(changed_events({event_id}))


@app.post("/api/rule")
def api_rule_add():
    """Fehlalarm-Regel aus einem stehenden Track: gilt für alle stehenden Tracks gleicher
    Klasse an derselben Stelle (Box-Überlappung >= RULE_IOU) dieser Kamera."""
    body = json_body()
    camera, event_id, t = find_track(str(body.get("key", "")))
    if t["moving"] or not t.get("box"):
        abort(400)
    x0, y0, x1, y1 = t["box"]
    with db() as con:
        cur = con.execute(
            "INSERT INTO rules (camera, cls, x0, y0, x1, y1, source, created) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (camera, t["orig_cls"], x0, y0, x1, y1, t["key"], datetime.now().isoformat(timespec="seconds")))
        rule_id = cur.lastrowid
    global _labels_version
    _labels_version += 1
    return jsonify(changed_events(events_with_rule(rule_id) | {event_id}))


@app.delete("/api/rule/<int:rule_id>")
def api_rule_delete(rule_id):
    ids = events_with_rule(rule_id)
    with db() as con:
        con.execute("DELETE FROM rules WHERE id = ?", (rule_id,))
    return jsonify(changed_events(ids))


STALE_S = 90   # ohne Lebenszeichen länger als das gilt ein laufender Download als "steht"


def detected_per_day(path: Path, by_name: bool) -> dict[str, int]:
    """done.txt -> Anzahl erledigter Clips je Datum (Stufe 1: 'datum/datei', Stufe 2: 'datei')."""
    out: dict[str, int] = defaultdict(int)
    if path.exists():
        for line in path.read_text(encoding="utf-8").split():
            m = FILENAME_RE.search(line)
            if m:
                out[m.group(1)] += 1
    return dict(out)


def build_status() -> dict:
    status_dir: Path = CFG["output_dir"] / "status"
    det_dir: Path = CFG["det_dir"]
    now = datetime.now()
    jobs = []
    for f in sorted(status_dir.glob("*.json")) if status_dir.is_dir() else []:
        try:
            job = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        updated = datetime.fromisoformat(job.get("updated", job.get("started")))
        age = (now - updated).total_seconds()
        state = job.get("state", "?")
        if state in ("plan", "läuft") and age > STALE_S:
            state = "steht"
        days = job.get("days", {})
        tot = {k: sum(d.get(k, 0) for d in days.values()) for k in ("clips", "bytes", "done", "done_bytes")}
        if job.get("stream") == "sub":
            det = detected_per_day(det_dir / job["camera"] / "done.txt", False)
        else:
            det = detected_per_day(det_dir / "plates" / "done.txt", True) \
                if job["camera"] == CFG["plates_camera"] else {}
        for d, v in days.items():
            v["detected"] = min(det.get(d, 0), v.get("done", 0))
        rate = job.get("rate_bps", 0) if state == "läuft" else 0
        cur = job.get("current")
        remaining = tot["bytes"] - tot["done_bytes"] - (cur or {}).get("received", 0)
        jobs.append({
            **job, "days": days, "state": state, "age": round(age), "totals": tot,
            "detected": sum(v["detected"] for v in days.values()),
            "eta_s": round(remaining / rate) if rate > 0 and remaining > 0 else None,
        })
    detectors = {}
    for cam_dir in sorted(p for p in det_dir.iterdir() if p.is_dir()):
        log = cam_dir / "detect.log"
        if log.exists():
            age = now.timestamp() - log.stat().st_mtime
            with open(log, "rb") as fh:
                fh.seek(max(log.stat().st_size - 400, 0))
                last = fh.read().decode("utf-8", "replace").strip().splitlines()[-1:] or [""]
            detectors[cam_dir.name] = {"age": round(age), "last": last[0]}
    return {"generated": now.isoformat(timespec="seconds"), "jobs": jobs, "detectors": detectors}


@app.get("/api/status")
def api_status():
    resp = jsonify(build_status())
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

def viewer_token(renew: bool) -> str:
    """Zugangs-Token für den LAN-Betrieb; bleibt über Neustarts gleich (Windows-Anmeldeinformationsverwaltung)."""
    import keyring
    token = None if renew else keyring.get_password(KEYRING_SERVICE, KEYRING_TOKEN_USER)
    if not token:
        token = secrets.token_urlsafe(16)
        keyring.set_password(KEYRING_SERVICE, KEYRING_TOKEN_USER, token)
    return token


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
    parser.add_argument("--new-token", action="store_true",
                        help="neues Zugangs-Token erzeugen (alte Links und Cookies werden ungültig)")
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

    CFG.update(config=config, output_dir=output_dir, det_dir=det_dir, plates_camera=args.plates_camera,
               db_path=det_dir / "labels.db")
    local = args.host in ("127.0.0.1", "localhost", "::1")
    if not local:
        CFG["token"] = viewer_token(args.new_token)

    init_db()
    data = get_events()
    print(f"Erkennungsdaten: {det_dir} (Korrekturen: {CFG['db_path'].name})")
    print(f"{len(data['events'])} Ereignisse von {', '.join(data['cameras']) or '-'}"
          f"{'' if WATCHLIST_FILE.exists() else ' (keine watchlist.json)'}")
    if local:
        print(f"Viewer: http://{args.host}:{args.port}/")
    else:
        print("ACHTUNG: Viewer ist im Netzwerk erreichbar, Zugriff nur mit Token.")
        for host in (socket.gethostname().lower(), lan_ip()):
            print(f"Viewer: http://{host}:{args.port}/?token={CFG['token']}")
    print("Beenden mit Ctrl+C.")
    app.run(host=args.host, port=args.port, threaded=True, debug=False)


if __name__ == "__main__":
    main()
