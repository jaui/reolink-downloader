#!/usr/bin/env python3
"""
Lädt Aufnahmen einer Kamera für einen Zeitraum in einem bestimmten Stream (main/sub),
älteste zuerst, fortsetzbar. Ergänzt reolink-downloader.py für die Erkennungs-Pipeline:

- Sub-Stream landet in einem eigenen Ordner: <output_dir>/<name>_sub_<host>/<datum>/...
- temporäre .part-Datei + Wiederholungen (die Kamera erlaubt nur einen Download gleichzeitig)
- Clips, die gerade noch aufgenommen werden, werden übersprungen
- Auswahl: nur bestimmte Clips (--only) oder nur Clips mit nahen, fahrenden Fahrzeugen
  aus der Stufe-1-Erkennung (--from-events), z. B. um dafür den Main-Stream nachzuladen

Läuft im Repo-venv (reolink-aio, keyring). Passwort wie beim Downloader im Credential Manager.
"""

import argparse
import asyncio
import json
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import keyring
from reolink_aio.api import Host

SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_FILE = SCRIPT_DIR / "config.json"
KEYRING_SERVICE = "reolink-downloader"
MATCH_TOLERANCE_S = 10      # Main- und Sub-Dateinamen weichen ca. 2 s ab
RETRIES = 5


def load_config() -> dict:
    with open(CONFIG_FILE, encoding="utf-8") as f:
        return json.load(f)


def output_dir(config: dict) -> Path:
    out = Path(config.get("output_dir", "downloads"))
    return out if out.is_absolute() else SCRIPT_DIR / out


def find_camera(config: dict, name: str) -> dict:
    for cam in config.get("cameras", []):
        if cam["name"] == name:
            return cam
    sys.exit(f"Fehler: Kamera '{name}' nicht in config.json")


def times_from_events(path: Path, min_area: float, min_path: float) -> list[datetime]:
    """Clip-Startzeiten mit nahen, fahrenden Fahrzeugen aus events.jsonl (Stufe 1)."""
    out = set()
    with open(path, encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            tr = json.loads(line)
            if tr.get("cls") in ("car", "bus", "truck") and tr.get("max_area", 0) >= min_area \
                    and tr.get("path", 0) >= min_path:
                out.add(datetime.strptime(Path(tr["video"]).stem, "%Y-%m-%d_%H-%M-%S"))
    return sorted(out)


async def fetch(cam: dict, username: str, root: Path, stream: str, d0: datetime, d1: datetime,
                only: list[datetime] | None) -> None:
    password = keyring.get_password(KEYRING_SERVICE, username)
    if not password:
        sys.exit("Fehler: Kein Passwort gespeichert. Zuerst: python reolink-downloader.py --setup-password")
    sub = "_sub" if stream == "sub" else ""
    target = root / f"{cam['name']}{sub}_{cam['host']}"
    host = Host(cam["host"], username, password, use_https=False, stream=stream)
    await host.login()
    await host.get_host_data()
    n = nbytes = 0
    t0 = time.time()
    day = d0
    try:
        while day <= d1:
            _, files = await host.request_vod_files(channel=cam.get("channel", 0), start=day,
                                                    end=day.replace(hour=23, minute=59, second=59), stream=stream)
            files = sorted(files, key=lambda v: v.start_time)
            for v in files:
                st, et = v.start_time.replace(tzinfo=None), v.end_time.replace(tzinfo=None)
                base = v.start_time.strftime("%Y-%m-%d_%H-%M-%S")
                if only is not None and not any(abs((st - o).total_seconds()) <= MATCH_TOLERANCE_S for o in only):
                    continue
                fp = target / base[:10] / f"{base}.mp4"
                if fp.exists() and fp.stat().st_size == v.size:
                    continue
                if et > datetime.now() - timedelta(minutes=2):
                    continue  # wird noch aufgenommen
                fp.parent.mkdir(parents=True, exist_ok=True)
                tmp = fp.with_suffix(".part")
                for attempt in range(RETRIES):
                    try:
                        dl = await host.download_vod(filename=v.file_name)
                        with open(tmp, "wb") as f:
                            while chunk := await dl.stream.read(1 << 20):
                                f.write(chunk)
                        dl.close()
                        tmp.replace(fp)
                        n += 1
                        nbytes += v.size
                        break
                    except Exception as e:  # 503 bei parallelem Zugriff, Verbindungsabbrüche
                        print(f"  Wiederholung {base}: {type(e).__name__} {e}", flush=True)
                        await asyncio.sleep(5 * (attempt + 1))
                else:
                    print(f"  FEHLER {base}: aufgegeben", flush=True)
            print(f"{day:%Y-%m-%d}: {len(files)} Clips, gesamt {n} geladen, {nbytes / 1e9:.1f} GB, "
                  f"{nbytes / 1e6 / max(time.time() - t0, 1):.1f} MB/s", flush=True)
            day += timedelta(days=1)
    finally:
        await host.logout()


def main():
    p = argparse.ArgumentParser(description="Aufnahmen einer Kamera für einen Zeitraum laden (main/sub)")
    p.add_argument("--camera", required=True, help="Kameraname aus config.json")
    p.add_argument("--stream", choices=["main", "sub"], default="sub", help="Stream (Standard: sub)")
    p.add_argument("--from", dest="date_from", required=True, help="Startdatum YYYY-MM-DD")
    p.add_argument("--to", dest="date_to", help="Enddatum YYYY-MM-DD (Standard: = Startdatum)")
    sel = p.add_mutually_exclusive_group()
    sel.add_argument("--only", type=Path, help="Textdatei mit Clip-Namen YYYY-MM-DD_HH-MM-SS (±10 s)")
    sel.add_argument("--from-events", type=Path,
                     help="events.jsonl der Stufe-1-Erkennung: nur Clips mit nahen, fahrenden Fahrzeugen")
    p.add_argument("--min-area", type=float, default=0.02, help="mit --from-events: min. Box-Anteil (0.02)")
    p.add_argument("--min-path", type=float, default=0.05, help="mit --from-events: min. Bewegung (0.05)")
    args = p.parse_args()

    config = load_config()
    cam = find_camera(config, args.camera)
    d0 = datetime.strptime(args.date_from, "%Y-%m-%d")
    d1 = datetime.strptime(args.date_to or args.date_from, "%Y-%m-%d")
    only = None
    if args.only:
        only = [datetime.strptime(x, "%Y-%m-%d_%H-%M-%S") for x in args.only.read_text().split()]
    elif args.from_events:
        only = times_from_events(args.from_events, args.min_area, args.min_path)
        print(f"{len(only)} Clips mit nahen, fahrenden Fahrzeugen in {args.from_events}")
    asyncio.run(fetch(cam, config["username"], output_dir(config), args.stream, d0, d1, only))


if __name__ == "__main__":
    main()
