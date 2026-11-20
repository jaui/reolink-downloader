#!/usr/bin/env python3
"""
Stufe 1: Personen, Fahrräder und Fahrzeuge in (Sub-Stream-)Clips erkennen und verfolgen.

Je Clip: ffmpeg dekodiert mit --fps Bildern/s, YOLO11n + ByteTrack verfolgt Objekte.
Danach (Format v2):
  - Spuren werden je (Tracker-ID, Klasse) geführt, weil ByteTrack klassenunabhängig zuordnet,
  - Bahn je Spur (Box je ausgewertetem Bild) wird gespeichert,
  - zerfallene Spuren derselben Klasse (Lücke ≤ MERGE_GAP_S, Anschluss an derselben Stelle)
    werden zusammengeführt,
  - Prüfstufe: zweifelhafte Spuren (alle Zweiräder, Personen mit Konfidenz < VERIFY_BELOW)
    werden mit einem größeren Modell (YOLO11m) auf einem vergrößerten Bildausschnitt
    nachgeprüft; Ergebnis in "verify" (ok/conf). Nichts wird verworfen, der Viewer blendet
    nicht bestätigte Spuren als "unsicher" aus.
Je Spur eine Zeile in <out>/events.jsonl (Klasse, Zeiten, max. Box-Anteil, Bewegung, Nacht/IR,
bester Bildausschnitt in <out>/crops/). Fortsetzbar über <out>/done.txt; im Format v2
bearbeitete Clips stehen zusätzlich in <out>/done_v2.txt (ältere Zeilen dieser Clips ignoriert
der Viewer).

--classes ist eine Komma-Liste aus person, bike (Fahrrad, Motorrad), vehicle (Auto, Bus, LKW) oder all.
--redo-persons wertet bereits bearbeitete Clips mit Personen im Format v2 neu aus.

Läuft im ALPR-venv (c:\\localapps\\alpr\\venv: torch CPU, ultralytics, opencv), z. B.:
    c:\\localapps\\alpr\\venv\\Scripts\\python detect.py --camera garage2 --classes all --watch 600
    c:\\localapps\\alpr\\venv\\Scripts\\python detect.py --camera schuppen2 --classes person,bike --watch 600
    c:\\localapps\\alpr\\venv\\Scripts\\python detect.py --camera schuppen2 --classes person,bike --redo-persons
"""

import argparse
import json
import math
import subprocess
import sys
import time
from pathlib import Path

import cv2
import numpy as np
from ultralytics import YOLO

from timelapse import FFMPEG, FFPROBE

SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_FILE = SCRIPT_DIR / "config.json"
DEFAULT_MODELS = Path(r"c:\localapps\alpr\models")
FORMAT = 2

NAMES = {0: "person", 1: "bicycle", 2: "car", 3: "motorcycle", 5: "bus", 7: "truck"}
GROUPS = {"person": [0], "bike": [1, 3], "vehicle": [2, 5, 7]}   # bike = Zweiräder
KEEP = {"person": 0.004, "bicycle": 0.003, "motorcycle": 0.005,
        "car": 0.01, "bus": 0.01, "truck": 0.01}   # min. max. Box-Anteil eines Tracks
IR_MAX_SATURATION = 12      # mittlere Sättigung darunter = IR-/Graubild

# Zusammenführen zerfallener Spuren (ByteTrack vergibt nach Verdeckung neue IDs)
MERGE_GAP_S = 4.0           # max. Lücke zwischen Ende der einen und Anfang der nächsten Spur
MERGE_DIST = 1.0            # max. Abstand der Boxmitten, relativ zur Boxdiagonale
MERGE_SIZE_RATIO = 3.0      # max. Flächenverhältnis der Anschluss-Boxen

# Prüfstufe (kalibriert 2026-11-20 an 142 Rad- und 92 Personen-Kandidaten, Sichtprüfung):
#   Räder: YOLO11m-Konfidenz >= 0.75 -> 18/20 echte bestätigt, 0 von 122 Fehlalarmen
#          (Rasenmäher, Reifen, Kabel, Staubsauger, Einblendung "Garage2")
#   Personen < 0.6: YOLO11m >= 0.3 trennt Gartenstuhl/Leeres von echten Personen
VERIFY_MODEL = "yolo11m.pt"
VERIFY_BELOW = {"person": 0.6}                       # nur schwache Personen prüfen
VERIFY_ALWAYS = {"bicycle", "motorcycle"}            # Zweiräder immer prüfen
VERIFY_MIN = {"person": 0.3, "bicycle": 0.75, "motorcycle": 0.75}
VERIFY_SAME = {"person": {"person"}, "bicycle": {"bicycle", "motorcycle"}, "motorcycle": {"bicycle", "motorcycle"}}
VERIFY_IOU = 0.3
CTX_SCALE, CTX_MIN_PX = 2.0, 192                     # Ausschnitt: doppelte Boxgröße, mind. 192 px


def parse_classes(spec: str) -> list[int]:
    """'person,bike' -> COCO-IDs; 'all' = alle Gruppen."""
    names = list(GROUPS) if spec == "all" else [x.strip() for x in spec.split(",") if x.strip()]
    bad = [n for n in names if n not in GROUPS]
    if bad or not names:
        raise argparse.ArgumentTypeError(f"unbekannte Klasse(n): {', '.join(bad) or spec} "
                                         f"(erlaubt: {', '.join(GROUPS)}, all)")
    return sorted({c for n in names for c in GROUPS[n]})


def load_config() -> dict:
    with open(CONFIG_FILE, encoding="utf-8") as f:
        return json.load(f)


def camera_paths(name: str) -> tuple[Path, Path]:
    """(Sub-Stream-Ordner, Standard-Ausgabeordner) einer Kamera aus config.json."""
    config = load_config()
    root = Path(config.get("output_dir", "downloads"))
    root = root if root.is_absolute() else SCRIPT_DIR / root
    for cam in config.get("cameras", []):
        if cam["name"] == name:
            return root / f"{cam['name']}_sub_{cam['host']}", root / "detections" / cam["name"]
    sys.exit(f"Fehler: Kamera '{name}' nicht in config.json")


def collect(inputs: list[Path], date_from: str | None, date_to: str | None) -> list[Path]:
    """Ordner (mit Datumsordnern darunter) oder einzelne Dateien; .part-Dateien fallen weg."""
    videos = []
    for p in inputs:
        videos += sorted(p.glob("*/*.mp4")) if p.is_dir() else [p]
    return [v for v in videos if v.suffix == ".mp4"
            and (not date_from or v.parent.name >= date_from) and (not date_to or v.parent.name <= date_to)]


def context_crop(f: np.ndarray, x1: int, y1: int, x2: int, y2: int) -> tuple[np.ndarray, list[float]]:
    """Vergrößerter Ausschnitt um die Box (für die Prüfstufe) + Box in Ausschnitt-Koordinaten."""
    H, W = f.shape[:2]
    cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
    s = max((x2 - x1) * CTX_SCALE, (y2 - y1) * CTX_SCALE, CTX_MIN_PX)
    X0, Y0 = int(max(0, cx - s / 2)), int(max(0, cy - s / 2))
    X1, Y1 = int(min(W, cx + s / 2)), int(min(H, cy + s / 2))
    return f[Y0:Y1, X0:X1].copy(), [x1 - X0, y1 - Y0, x2 - X0, y2 - Y0]


def iou(a, b) -> float:
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    i = ix * iy
    return i / ((a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - i + 1e-9)


def center_path(traj: list) -> float:
    """Bewegung = größte Entfernung der Boxmitte vom Startpunkt (normiert, |dx| + |dy|)."""
    x0, y0 = (traj[0][1] + traj[0][3]) / 2, (traj[0][2] + traj[0][4]) / 2
    return max(abs((p[1] + p[3]) / 2 - x0) + abs((p[2] + p[4]) / 2 - y0) for p in traj)


def can_join(a: dict, b: dict, W: int, H: int) -> float | None:
    """Abstandsmaß, wenn Spur b die Fortsetzung von a sein kann, sonst None."""
    if a["cls"] != b["cls"] or b["t_first"] <= a["t_last"] or b["t_first"] - a["t_last"] > MERGE_GAP_S:
        return None
    p, q = a["traj"][-1], b["traj"][0]
    pa = (p[1] * W, p[2] * H, p[3] * W, p[4] * H)
    qa = (q[1] * W, q[2] * H, q[3] * W, q[4] * H)
    area_p, area_q = (pa[2] - pa[0]) * (pa[3] - pa[1]), (qa[2] - qa[0]) * (qa[3] - qa[1])
    if min(area_p, area_q) <= 0 or max(area_p, area_q) / min(area_p, area_q) > MERGE_SIZE_RATIO:
        return None
    d = math.dist(((pa[0] + pa[2]) / 2, (pa[1] + pa[3]) / 2), ((qa[0] + qa[2]) / 2, (qa[1] + qa[3]) / 2))
    diag = max(math.hypot(pa[2] - pa[0], pa[3] - pa[1]), math.hypot(qa[2] - qa[0], qa[3] - qa[1]))
    return d / diag if d <= MERGE_DIST * diag else None


def merge_tracks(tracks: dict, W: int, H: int) -> list[dict]:
    """Zerfallene Spuren gierig in zeitlicher Reihenfolge verketten (jeweils nächster Anschluss)."""
    order = sorted(tracks.values(), key=lambda t: t["t_first"])
    used, out = set(), []
    for a in order:
        if id(a) in used:
            continue
        used.add(id(a))
        while True:
            cands = [(s, b) for b in order if id(b) not in used and (s := can_join(a, b, W, H)) is not None]
            if not cands:
                break
            _, b = min(cands, key=lambda x: x[0])
            used.add(id(b))
            a["merged"] = a.get("merged", [a["track"]]) + b.get("merged", [b["track"]])
            a["traj"] += b["traj"]
            a["t_last"], a["n"] = b["t_last"], a["n"] + b["n"]
            a["confs"] += b["confs"]
            a["max_conf"] = max(a["max_conf"], b["max_conf"])
            if b["max_area"] > a["max_area"]:
                for k in ("max_area", "best_t", "best", "box", "ctx", "ctx_box"):
                    a[k] = b[k]
        out.append(a)
    return out


class Verifier:
    """Größeres Modell für die Prüfstufe, erst bei Bedarf geladen."""

    def __init__(self, models: Path):
        self.path, self.model = models / VERIFY_MODEL, None

    def __call__(self, tr: dict) -> dict:
        if self.model is None:
            self.model = YOLO(str(self.path))
        r = self.model.predict(tr["ctx"], imgsz=640, conf=0.2, verbose=False)[0]
        same = VERIFY_SAME[tr["cls"]]
        conf = max([cf for bx, cl, cf in zip(r.boxes.xyxy.tolist(), r.boxes.cls.int().tolist(), r.boxes.conf.tolist())
                    if self.model.names[cl] in same and iou(bx, tr["ctx_box"]) >= VERIFY_IOU] or [0.0])
        return {"model": VERIFY_MODEL.removesuffix(".pt"), "conf": round(conf, 2), "ok": conf >= VERIFY_MIN[tr["cls"]]}


def needs_verify(tr: dict) -> bool:
    return tr["cls"] in VERIFY_ALWAYS or tr["max_conf"] < VERIFY_BELOW.get(tr["cls"], 0)


def process(video: Path, out: Path, classes: list[int], fps: float, model: Path, verifier: Verifier) -> tuple[int, int]:
    info = json.loads(subprocess.run([FFPROBE, "-v", "error", "-select_streams", "v:0", "-show_entries",
                                      "stream=width,height", "-of", "json", str(video)],
                                     capture_output=True, text=True).stdout)
    W, H = info["streams"][0]["width"], info["streams"][0]["height"]
    proc = subprocess.Popen([FFMPEG, "-v", "error", "-i", str(video), "-vf", f"fps={fps}", "-f", "rawvideo",
                             "-pix_fmt", "bgr24", "-"], stdout=subprocess.PIPE)
    yolo = YOLO(str(model))   # neue Instanz je Clip = Tracker zurückgesetzt
    tracks, i = {}, 0
    while True:
        b = proc.stdout.read(W * H * 3)
        if len(b) < W * H * 3:
            break
        f = np.frombuffer(b, np.uint8).reshape(H, W, 3)
        t = i / fps
        i += 1
        r = yolo.track(f, imgsz=max(W, 640), classes=classes, conf=0.3, persist=True, verbose=False,
                       tracker="bytetrack.yaml")[0]
        if r.boxes.id is None:
            continue
        small = cv2.resize(f, (W // 4, H // 4))
        ir = float(cv2.cvtColor(small, cv2.COLOR_BGR2HSV)[:, :, 1].mean()) < IR_MAX_SATURATION
        for bx, tid, c, cf in zip(r.boxes.xyxy.tolist(), r.boxes.id.int().tolist(), r.boxes.cls.int().tolist(),
                                  r.boxes.conf.tolist()):
            x1, y1, x2, y2 = map(int, bx)
            area = (x2 - x1) * (y2 - y1) / (W * H)
            # ByteTrack ordnet klassenunabhängig zu: eine ID kann erst "Fahrrad", dann "Person" sein.
            # Deshalb je (ID, Klasse) eine eigene Spur; Bruchstücke verbindet merge_tracks wieder.
            tr = tracks.setdefault((tid, c), {"cls": NAMES[c], "track": tid, "max_area": 0, "t_first": t, "n": 0,
                                              "ir": ir, "max_conf": 0, "traj": [], "confs": []})
            tr["t_last"] = t
            tr["n"] += 1
            tr["max_conf"] = max(tr["max_conf"], cf)
            tr["confs"].append(cf)
            tr["traj"].append([round(t, 1), round(x1 / W, 3), round(y1 / H, 3), round(x2 / W, 3), round(y2 / H, 3),
                               round(cf, 2)])
            if area > tr["max_area"]:
                ctx, ctx_box = context_crop(f, x1, y1, x2, y2)
                tr.update(max_area=area, best_t=t, best=f[max(y1, 0):y2, max(x1, 0):x2].copy(),
                          box=[x1 / W, y1 / H, x2 / W, y2 / H], ctx=ctx, ctx_box=ctx_box)
    proc.wait()

    key = f"{video.parent.name}/{video.name}"
    kept = 0
    with open(out / "events.jsonl", "a", encoding="utf-8") as fh:
        for tr in merge_tracks(tracks, W, H):
            if tr["max_area"] < KEEP[tr["cls"]] or tr["n"] < 2:
                continue
            kept += 1
            if needs_verify(tr):
                tr["verify"] = verifier(tr)
            best = tr.pop("best")
            tr.pop("ctx"), tr.pop("ctx_box")
            confs = sorted(tr.pop("confs"), reverse=True)[:3]
            crop = f"{video.parent.name}_{video.stem}_{tr['cls']}{tr['track']}.jpg"
            cv2.imwrite(str(out / "crops" / crop), best)
            tr.update(v=FORMAT, video=key, crop=crop, max_area=round(tr["max_area"], 4),
                      path=round(center_path(tr["traj"]), 3), max_conf=round(tr["max_conf"], 2),
                      conf_track=round(sum(confs) / len(confs), 2), W=W, H=H)
            fh.write(json.dumps(tr) + "\n")
    return i, kept


def read_done(f: Path) -> set[str]:
    return set(f.read_text().split()) if f.exists() else set()


def run_pass(args, inputs: list[Path], out: Path, verifier: Verifier) -> int:
    done = read_done(out / "done.txt")
    done_v2 = read_done(out / "done_v2.txt")
    if args.redo_persons:
        # Neu im Format v2: schon bearbeitete Clips mit Personen (Radfahrer sind auch Personen)
        with_person = set()
        if (out / "events.jsonl").exists():
            with open(out / "events.jsonl", encoding="utf-8") as fh:
                with_person = {json.loads(l)["video"] for l in fh if '"person"' in l}
        todo = lambda key: key in done and key in with_person and key not in done_v2   # noqa: E731
    else:
        todo = lambda key: key not in done   # noqa: E731
    n = 0
    for video in collect(inputs, args.date_from, args.date_to):
        key = f"{video.parent.name}/{video.name}"
        if not todo(key):
            continue
        t0 = time.time()
        try:
            frames, kept = process(video, out, args.classes, args.fps, args.models / "yolo11n.pt", verifier)
        except Exception as e:
            print(f"{key}: FEHLER {type(e).__name__}: {e}", flush=True)
            continue
        for name in ("done_v2.txt",) if key in done else ("done.txt", "done_v2.txt"):
            with open(out / name, "a") as fh:
                fh.write(key + "\n")
        n += 1
        print(f"{key}: {frames} Bilder, {kept} Track(s), {time.time() - t0:.1f}s", flush=True)
    return n


def main():
    p = argparse.ArgumentParser(description="Stufe 1: Personen/Fahrräder/Fahrzeuge in Clips erkennen")
    p.add_argument("--camera", help="Kameraname aus config.json (Eingabe: Sub-Stream-Ordner, "
                                    "Ausgabe: <output_dir>/detections/<kamera>)")
    p.add_argument("inputs", nargs="*", type=Path, help="Ordner oder Dateien (statt --camera)")
    p.add_argument("--out", type=Path, help="Ausgabeordner (Standard bei --camera: detections/<kamera>)")
    p.add_argument("--classes", type=parse_classes, default="all",
                   help="Komma-Liste aus person, bike, vehicle oder all (Standard: all)")
    p.add_argument("--redo-persons", action="store_true",
                   help="bereits bearbeitete Clips mit Personen im aktuellen Format neu auswerten")
    p.add_argument("--fps", type=float, default=2, help="ausgewertete Bilder je Sekunde (Standard: 2)")
    p.add_argument("--from", dest="date_from", help="nur Datumsordner ab YYYY-MM-DD")
    p.add_argument("--to", dest="date_to", help="nur Datumsordner bis YYYY-MM-DD")
    p.add_argument("--models", type=Path, default=DEFAULT_MODELS, help=f"Modellordner (Standard: {DEFAULT_MODELS})")
    p.add_argument("--watch", type=int, metavar="SEK", help="dauerhaft laufen, alle SEK Sekunden neue Clips abarbeiten")
    args = p.parse_args()

    inputs, out = list(args.inputs), args.out
    if args.camera:
        sub_dir, default_out = camera_paths(args.camera)
        inputs = inputs or [sub_dir]
        out = out or default_out
    if not inputs or not out:
        p.error("--camera oder Eingaben + --out angeben")
    if not (args.models / VERIFY_MODEL).exists():
        p.error(f"Prüfmodell fehlt: {args.models / VERIFY_MODEL}")
    (out / "crops").mkdir(parents=True, exist_ok=True)
    verifier = Verifier(args.models)

    while True:
        n = run_pass(args, inputs, out, verifier)
        if not args.watch:
            break
        print(f"Durchlauf fertig {time.strftime('%H:%M:%S')}: {n} neue Clips", flush=True)
        time.sleep(args.watch)


if __name__ == "__main__":
    main()
