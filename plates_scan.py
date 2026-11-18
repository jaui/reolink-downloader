#!/usr/bin/env python3
"""
Stufe 2: Kennzeichen in Main-Stream-Clips lesen.

Je Clip: ffmpeg dekodiert mit --fps Bildern/s, YOLO11n + ByteTrack verfolgt Fahrzeuge. Sobald eine
Fahrzeugbox >= --min-plate-area des Bildes groß ist, sucht fast-alpr im Ausschnitt in voller
Auflösung nach Kennzeichen und liest sie. Tracks, die mindestens einmal --keep-area erreichen,
landen in <out>/tracks.jsonl mit allen Lesungen; Bildausschnitte in <out>/veh/, Kennzeichen in
<out>/plates/. Fortsetzbar über <out>/done.txt. Mehrheitslesung und Watchlist-Abgleich macht viewer.py.

Läuft im ALPR-venv (c:\\localapps\\alpr\\venv: torch CPU, ultralytics, fast-alpr, truststore), z. B.:
    c:\\localapps\\alpr\\venv\\Scripts\\python plates_scan.py --camera garage2 --from 2026-11-17
"""

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

import truststore

truststore.inject_into_ssl()   # Python kann die Zertifikatskette dieses Rechners sonst nicht prüfen

import numpy as np  # noqa: E402

from timelapse import FFMPEG, FFPROBE  # noqa: E402

SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_FILE = SCRIPT_DIR / "config.json"
DEFAULT_MODELS = Path(r"c:\localapps\alpr\models")
CLS = {2: "car", 5: "bus", 7: "truck"}
IR_MAX_SATURATION = 12


def load_models(models: Path):
    # Modell-Caches nach c:\localapps\alpr\models umlenken (vor dem Import von fast_alpr)
    import fast_plate_ocr.inference.hub as foh
    import open_image_models.detection.core.hub as oih
    foh.MODEL_CACHE_DIR = models / "fast-plate-ocr"
    oih.MODEL_CACHE_DIR = models / "open-image-models"
    from fast_alpr import ALPR
    return ALPR(detector_model="yolo-v9-t-640-license-plate-end2end", ocr_model="cct-s-v2-global-model",
                detector_providers=["CPUExecutionProvider"], ocr_providers=["CPUExecutionProvider"])


def color_name(bgr) -> str:
    """Grobe Farbe aus dem Median der Bildmitte (unzuverlässig: misst oft Scheibe/Reflexe, nachts nutzlos)."""
    import cv2
    h, w = bgr.shape[:2]
    c = bgr[h // 4: h * 3 // 4, w // 4: w * 3 // 4]
    hsv = cv2.cvtColor(c, cv2.COLOR_BGR2HSV).reshape(-1, 3).astype(float)
    s, v = np.median(hsv[:, 1]), np.median(hsv[:, 2])
    if s < 50:
        return "weiss" if v > 170 else "silber/grau" if v > 90 else "schwarz/dunkel"
    hue = np.median(hsv[hsv[:, 1] >= 50, 0])
    for lim, name in ((8, "rot"), (22, "orange/braun"), (35, "gelb"), (85, "gruen"), (130, "blau"),
                      (160, "lila"), (180, "rot")):
        if hue < lim:
            return name
    return "?"


def camera_paths(name: str) -> tuple[Path, Path]:
    """(Main-Stream-Ordner, Standard-Ausgabeordner) einer Kamera aus config.json."""
    with open(CONFIG_FILE, encoding="utf-8") as f:
        config = json.load(f)
    root = Path(config.get("output_dir", "downloads"))
    root = root if root.is_absolute() else SCRIPT_DIR / root
    for cam in config.get("cameras", []):
        if cam["name"] == name:
            return root / f"{cam['name']}_{cam['host']}", root / "detections" / "plates"
    sys.exit(f"Fehler: Kamera '{name}' nicht in config.json")


def process(video: Path, out: Path, alpr, args) -> tuple[int, list[dict]]:
    import cv2
    from ultralytics import YOLO
    info = json.loads(subprocess.run([FFPROBE, "-v", "error", "-select_streams", "v:0", "-show_entries",
                                      "stream=width,height", "-of", "json", str(video)],
                                     capture_output=True, text=True).stdout)
    W, H = info["streams"][0]["width"], info["streams"][0]["height"]
    proc = subprocess.Popen([FFMPEG, "-v", "error", "-i", str(video), "-vf", f"fps={args.fps}", "-f", "rawvideo",
                             "-pix_fmt", "bgr24", "-"], stdout=subprocess.PIPE)
    yolo = YOLO(str(args.models / "yolo11n.pt"))
    stem = video.stem
    tracks, i = {}, 0
    while True:
        b = proc.stdout.read(W * H * 3)
        if len(b) < W * H * 3:
            break
        f = np.frombuffer(b, np.uint8).reshape(H, W, 3)
        t = i / args.fps
        i += 1
        small = cv2.resize(f, (W // 8, H // 8))
        sat = float(cv2.cvtColor(small, cv2.COLOR_BGR2HSV)[:, :, 1].mean())
        r = yolo.track(f, imgsz=1280, classes=list(CLS), conf=0.3, persist=True, verbose=False,
                       tracker="bytetrack.yaml")[0]
        if r.boxes.id is None:
            continue
        for bx, tid, c in zip(r.boxes.xyxy.tolist(), r.boxes.id.int().tolist(), r.boxes.cls.int().tolist()):
            x1, y1, x2, y2 = map(int, bx)
            area = (x2 - x1) * (y2 - y1) / (W * H)
            tr = tracks.setdefault(tid, {"cls": CLS[c], "max_area": 0, "t_first": t, "reads": [],
                                         "ir": sat < IR_MAX_SATURATION})
            tr["t_last"] = t
            crop = f[max(y1, 0):y2, max(x1, 0):x2]
            if area > tr["max_area"]:
                tr.update(max_area=area, best=crop.copy(), best_t=t)
            if area < args.min_plate_area:
                continue
            for d in alpr.detector.predict(crop):
                bb = d.bounding_box
                pw, ph = bb.x2 - bb.x1, bb.y2 - bb.y1
                px1, py1 = max(bb.x1 - pw // 10, 0), max(bb.y1 - ph // 5, 0)
                px2, py2 = min(bb.x2 + pw // 10, crop.shape[1]), min(bb.y2 + ph // 5, crop.shape[0])
                pr = alpr.ocr.predict(crop[max(bb.y1, 0):bb.y2, max(bb.x1, 0):bb.x2])
                png = f"{stem}_tr{tid}_t{t:05.1f}.png"
                cv2.imwrite(str(out / "plates" / png), crop[py1:py2, px1:px2])
                conf = float(np.mean(pr.confidence)) if pr and pr.confidence else 0.0
                tr["reads"].append({"t": t, "text": pr.text if pr else "", "conf": round(conf, 3),
                                    "det_conf": round(float(d.confidence), 3), "plate_w": int(pw), "png": png})
    proc.wait()

    kept = []
    with open(out / "tracks.jsonl", "a", encoding="utf-8") as fh:
        for tid, tr in tracks.items():
            if tr["max_area"] < args.keep_area:
                continue
            best = tr.pop("best")
            veh = f"{stem}_tr{tid}.jpg"
            s = min(800 / max(best.shape[:2]), 1)
            cv2.imwrite(str(out / "veh" / veh), cv2.resize(best, None, fx=s, fy=s))
            tr.update(video=video.name, track=tid, color=color_name(best), veh_jpg=veh,
                      max_area=round(tr["max_area"], 4))
            fh.write(json.dumps(tr) + "\n")
            kept.append(tr)
    return i, kept


def main():
    p = argparse.ArgumentParser(description="Stufe 2: Kennzeichen in Main-Stream-Clips lesen")
    p.add_argument("--camera", help="Kameraname aus config.json (Eingabe: Main-Stream-Ordner, "
                                    "Ausgabe: <output_dir>/detections/plates)")
    p.add_argument("inputs", nargs="*", type=Path, help="Ordner (mit Datumsordnern) oder Dateien")
    p.add_argument("--out", type=Path, help="Ausgabeordner (Standard bei --camera: detections/plates)")
    p.add_argument("--from", dest="date_from", help="nur Datumsordner ab YYYY-MM-DD")
    p.add_argument("--to", dest="date_to", help="nur Datumsordner bis YYYY-MM-DD")
    p.add_argument("--fps", type=float, default=2, help="ausgewertete Bilder je Sekunde (Standard: 2)")
    p.add_argument("--min-plate-area", type=float, default=0.008,
                   help="Kennzeichen lesen ab diesem Box-Anteil des Fahrzeugs (Standard: 0.008)")
    p.add_argument("--keep-area", type=float, default=0.02,
                   help="Track behalten, wenn er einmal diesen Box-Anteil erreicht (Standard: 0.02)")
    p.add_argument("--models", type=Path, default=DEFAULT_MODELS, help=f"Modellordner (Standard: {DEFAULT_MODELS})")
    args = p.parse_args()

    inputs, out = list(args.inputs), args.out
    if args.camera:
        main_dir, default_out = camera_paths(args.camera)
        inputs = inputs or [main_dir]
        out = out or default_out
    if not inputs or not out:
        p.error("--camera oder Eingaben + --out angeben")
    for d in (out, out / "veh", out / "plates"):
        d.mkdir(parents=True, exist_ok=True)

    videos = []
    for ip in inputs:
        videos += sorted(ip.glob("*/*.mp4")) if ip.is_dir() else [ip]
    videos = [v for v in videos if v.suffix == ".mp4"
              and (not args.date_from or v.parent.name >= args.date_from)
              and (not args.date_to or v.parent.name <= args.date_to)]

    donef = out / "done.txt"
    done = set(donef.read_text().split()) if donef.exists() else set()
    todo = [v for v in videos if v.name not in done]
    print(f"{len(todo)} von {len(videos)} Clips zu bearbeiten")
    if not todo:
        return
    alpr = load_models(args.models)
    for video in todo:
        t0 = time.time()
        try:
            frames, kept = process(video, out, alpr, args)
        except Exception as e:
            print(f"{video.name}: FEHLER {type(e).__name__}: {e}", flush=True)
            continue
        with open(donef, "a") as fh:
            fh.write(video.name + "\n")
        print(f"{video.name}: {frames} Bilder, {len(kept)} Fahrzeug(e), "
              f"{sum(len(t['reads']) for t in kept)} Lesungen, {time.time() - t0:.0f}s", flush=True)


if __name__ == "__main__":
    main()
