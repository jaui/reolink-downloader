#!/usr/bin/env python3
"""
Stufe 1: Personen und Fahrzeuge in (Sub-Stream-)Clips erkennen und verfolgen.

Je Clip: ffmpeg dekodiert mit --fps Bildern/s, YOLO11n + ByteTrack verfolgt Objekte.
Je Track wird eine Zeile in <out>/events.jsonl geschrieben (Klasse, Zeiten, max. Box-Anteil,
Bewegung, Nacht/IR, bester Bildausschnitt in <out>/crops/). Fortsetzbar über <out>/done.txt.
Ausgabe wird von viewer.py und fetch_clips.py --from-events gelesen.

Läuft im ALPR-venv (c:\\localapps\\alpr\\venv: torch CPU, ultralytics, opencv), z. B.:
    c:\\localapps\\alpr\\venv\\Scripts\\python detect.py --camera garage2 --classes all
    c:\\localapps\\alpr\\venv\\Scripts\\python detect.py --camera schuppen2 --classes person --watch 600
"""

import argparse
import json
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

NAMES = {0: "person", 2: "car", 5: "bus", 7: "truck"}
SELECT = {"person": [0], "vehicle": [2, 5, 7], "all": [0, 2, 5, 7]}
KEEP = {"person": 0.004, "car": 0.01, "bus": 0.01, "truck": 0.01}   # min. max. Box-Anteil eines Tracks
IR_MAX_SATURATION = 12      # mittlere Sättigung darunter = IR-/Graubild


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


def process(video: Path, out: Path, classes: list[int], fps: float, model: Path) -> tuple[int, int]:
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
            cx, cy = (x1 + x2) / 2 / W, (y1 + y2) / 2 / H
            tr = tracks.setdefault(tid, {"cls": NAMES[c], "max_area": 0, "t_first": t, "n": 0, "ir": ir,
                                         "c0": (cx, cy), "path": 0.0, "max_conf": 0})
            # Bewegung = größte Entfernung des Box-Mittelpunkts vom Startpunkt (normiert)
            tr["path"] = max(tr["path"], abs(cx - tr["c0"][0]) + abs(cy - tr["c0"][1]))
            tr["t_last"] = t
            tr["n"] += 1
            tr["max_conf"] = max(tr["max_conf"], cf)
            if area > tr["max_area"]:
                tr.update(max_area=area, best_t=t, best=f[max(y1, 0):y2, max(x1, 0):x2].copy(),
                          box=[x1 / W, y1 / H, x2 / W, y2 / H])
    proc.wait()

    key = f"{video.parent.name}/{video.name}"
    kept = 0
    with open(out / "events.jsonl", "a", encoding="utf-8") as fh:
        for tid, tr in tracks.items():
            if tr["max_area"] < KEEP[tr["cls"]] or tr["n"] < 2:
                continue
            kept += 1
            best = tr.pop("best")
            tr.pop("c0")
            crop = f"{video.parent.name}_{video.stem}_{tr['cls']}{tid}.jpg"
            cv2.imwrite(str(out / "crops" / crop), best)
            tr.update(video=key, track=tid, crop=crop, max_area=round(tr["max_area"], 4),
                      path=round(tr["path"], 3), max_conf=round(tr["max_conf"], 2), W=W, H=H)
            fh.write(json.dumps(tr) + "\n")
    return i, kept


def run_pass(args, inputs: list[Path], out: Path) -> int:
    donef = out / "done.txt"
    done = set(donef.read_text().split()) if donef.exists() else set()
    n = 0
    for video in collect(inputs, args.date_from, args.date_to):
        key = f"{video.parent.name}/{video.name}"
        if key in done:
            continue
        t0 = time.time()
        try:
            frames, kept = process(video, out, SELECT[args.classes], args.fps, args.models / "yolo11n.pt")
        except Exception as e:
            print(f"{key}: FEHLER {type(e).__name__}: {e}", flush=True)
            continue
        with open(donef, "a") as fh:
            fh.write(key + "\n")
        n += 1
        print(f"{key}: {frames} Bilder, {kept} Track(s), {time.time() - t0:.1f}s", flush=True)
    return n


def main():
    p = argparse.ArgumentParser(description="Stufe 1: Personen/Fahrzeuge in Clips erkennen")
    p.add_argument("--camera", help="Kameraname aus config.json (Eingabe: Sub-Stream-Ordner, "
                                    "Ausgabe: <output_dir>/detections/<kamera>)")
    p.add_argument("inputs", nargs="*", type=Path, help="Ordner oder Dateien (statt --camera)")
    p.add_argument("--out", type=Path, help="Ausgabeordner (Standard bei --camera: detections/<kamera>)")
    p.add_argument("--classes", choices=list(SELECT), default="all", help="person | vehicle | all")
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
    (out / "crops").mkdir(parents=True, exist_ok=True)

    while True:
        n = run_pass(args, inputs, out)
        if not args.watch:
            break
        print(f"Durchlauf fertig {time.strftime('%H:%M:%S')}: {n} neue Clips", flush=True)
        time.sleep(args.watch)


if __name__ == "__main__":
    main()
