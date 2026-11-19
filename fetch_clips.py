#!/usr/bin/env python3
"""
Lädt Aufnahmen einer Kamera für einen Zeitraum in einem bestimmten Stream (main/sub),
älteste zuerst, fortsetzbar. Ergänzt reolink-downloader.py für die Erkennungs-Pipeline:

- Sub-Stream landet in einem eigenen Ordner: <output_dir>/<name>_sub_<host>/<datum>/...
- temporäre .part-Datei + Wiederholungen (die Kamera erlaubt nur einen Download gleichzeitig)
- Clips, die gerade noch aufgenommen werden, werden übersprungen
- Auswahl: nur bestimmte Clips (--only) oder nur Clips mit nahen, fahrenden Fahrzeugen
  aus der Stufe-1-Erkennung (--from-events), z. B. um dafür den Main-Stream nachzuladen
- Fortschritt (je Tag, aktueller Clip, Rate, Restzeit) in <output_dir>/status/<kamera>_<stream>.json,
  angezeigt von viewer.py

Läuft im Repo-venv (reolink-aio, keyring). Passwort wie beim Downloader im Credential Manager.
"""

import argparse
import asyncio
import json
import os
import signal
import sys
import time
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path

import keyring
from reolink_aio.api import Host

SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_FILE = SCRIPT_DIR / "config.json"
KEYRING_SERVICE = "reolink-downloader"
MATCH_TOLERANCE_S = 10      # Main- und Sub-Dateinamen weichen ca. 2 s ab
RETRIES = 5
STATUS_INTERVAL_S = 2       # Statusdatei höchstens so oft schreiben
RATE_WINDOW_S = 120         # Rate über die letzten 2 Minuten

_shutdown = False


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


class Status:
    """Fortschritt als JSON-Datei für viewer.py (atomar geschrieben)."""

    def __init__(self, path: Path, info: dict):
        self.path = path
        self.data = {**info, "state": "plan", "message": "", "pid": os.getpid(),
                     "started": datetime.now().isoformat(timespec="seconds"),
                     "days": {}, "current": None, "rate_bps": 0, "session_bytes": 0}
        self._last = 0.0
        self._samples: deque = deque()
        path.parent.mkdir(parents=True, exist_ok=True)

    def day(self, date: str) -> dict:
        return self.data["days"].setdefault(date, {"clips": 0, "bytes": 0, "done": 0, "done_bytes": 0,
                                                   "recording": 0, "failed": 0})

    def add_bytes(self, n: int) -> None:
        self.data["session_bytes"] += n
        now = time.time()
        self._samples.append((now, self.data["session_bytes"]))
        while self._samples and now - self._samples[0][0] > RATE_WINDOW_S:
            self._samples.popleft()
        if len(self._samples) > 1:
            (t0, b0), (t1, b1) = self._samples[0], self._samples[-1]
            self.data["rate_bps"] = round((b1 - b0) / max(t1 - t0, 1e-3))

    def save(self, force: bool = False) -> None:
        now = time.time()
        if not force and now - self._last < STATUS_INTERVAL_S:
            return
        self._last = now
        self.data["updated"] = datetime.now().isoformat(timespec="seconds")
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False), encoding="utf-8")
        for _ in range(5):   # Leser kann die Datei kurz geöffnet haben (Windows)
            try:
                os.replace(tmp, self.path)
                return
            except PermissionError:
                time.sleep(0.2)


async def fetch(cam: dict, username: str, root: Path, stream: str, d0: datetime, d1: datetime,
                only: list[datetime] | None, selection: str) -> None:
    password = keyring.get_password(KEYRING_SERVICE, username)
    if not password:
        sys.exit("Fehler: Kein Passwort gespeichert. Zuerst: python reolink-downloader.py --setup-password")
    sub = "_sub" if stream == "sub" else ""
    target = root / f"{cam['name']}{sub}_{cam['host']}"
    status = Status(root / "status" / f"{cam['name']}_{stream}.json",
                    {"camera": cam["name"], "stream": stream, "host": cam["host"], "selection": selection,
                     "from": f"{d0:%Y-%m-%d}", "to": f"{d1:%Y-%m-%d}"})
    status.save(force=True)
    host = Host(cam["host"], username, password, use_https=False, stream=stream)
    try:
        await host.login()
        await host.get_host_data()

        # 1. Alle Tage auflisten -> Gesamtumfang für die Fortschrittsanzeige
        todo = []
        day = d0
        while day <= d1 and not _shutdown:
            _, files = await host.request_vod_files(channel=cam.get("channel", 0), start=day,
                                                    end=day.replace(hour=23, minute=59, second=59), stream=stream)
            ds = status.day(f"{day:%Y-%m-%d}")
            for v in sorted(files, key=lambda v: v.start_time):
                st = v.start_time.replace(tzinfo=None)
                if only is not None and not any(abs((st - o).total_seconds()) <= MATCH_TOLERANCE_S for o in only):
                    continue
                base = v.start_time.strftime("%Y-%m-%d_%H-%M-%S")
                fp = target / base[:10] / f"{base}.mp4"
                ds["clips"] += 1
                ds["bytes"] += v.size
                if fp.exists() and fp.stat().st_size == v.size:
                    ds["done"] += 1
                    ds["done_bytes"] += v.size
                else:
                    todo.append((v, base, fp))
            status.data["message"] = f"Liste {day:%Y-%m-%d}"
            status.save()
            day += timedelta(days=1)

        total = sum(d["clips"] for d in status.data["days"].values())
        print(f"{total} Clips im Zeitraum, {len(todo)} zu laden", flush=True)
        status.data.update(state="läuft", message="")
        status.save(force=True)

        # 2. Laden, älteste zuerst
        n = 0
        for v, base, fp in todo:
            if _shutdown:
                break
            ds = status.day(base[:10])
            if v.end_time.replace(tzinfo=None) > datetime.now() - timedelta(minutes=2):
                ds["recording"] += 1
                continue  # wird noch aufgenommen
            fp.parent.mkdir(parents=True, exist_ok=True)
            tmp = fp.with_suffix(".part")
            status.data["current"] = {"file": fp.name, "size": v.size, "received": 0}
            for attempt in range(RETRIES):
                try:
                    dl = await host.download_vod(filename=v.file_name)
                    received = 0
                    with open(tmp, "wb") as f:
                        while chunk := await dl.stream.read(1 << 20):
                            f.write(chunk)
                            received += len(chunk)
                            status.add_bytes(len(chunk))
                            status.data["current"]["received"] = received
                            status.save()
                    dl.close()
                    tmp.replace(fp)
                    ds["done"] += 1
                    ds["done_bytes"] += v.size
                    n += 1
                    break
                except Exception as e:  # 503 bei parallelem Zugriff, Verbindungsabbrüche
                    print(f"  Wiederholung {base}: {type(e).__name__} {e}", flush=True)
                    status.data["message"] = f"Wiederholung {base}: {type(e).__name__}"
                    status.save(force=True)
                    await asyncio.sleep(5 * (attempt + 1))
            else:
                ds["failed"] += 1
                print(f"  FEHLER {base}: aufgegeben", flush=True)
            status.data["current"] = None
            status.data["message"] = ""
            status.save()
            if n and n % 50 == 0:
                print(f"{n} geladen, {status.data['session_bytes'] / 1e9:.1f} GB", flush=True)

        status.data.update(state="abgebrochen" if _shutdown else "fertig", current=None, rate_bps=0)
        print(f"{'Abgebrochen' if _shutdown else 'Fertig'}: {n} Clips geladen, "
              f"{status.data['session_bytes'] / 1e9:.1f} GB", flush=True)
    except BaseException as e:
        status.data.update(state="fehler", message=f"{type(e).__name__}: {e}", current=None, rate_bps=0)
        raise
    finally:
        status.save(force=True)
        try:
            await host.logout()
        except Exception:
            pass


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
    only, selection = None, "alle"
    if args.only:
        only = [datetime.strptime(x, "%Y-%m-%d_%H-%M-%S") for x in args.only.read_text().split()]
        selection = "Liste"
    elif args.from_events:
        only = times_from_events(args.from_events, args.min_area, args.min_path)
        selection = "Fahrzeug-Ereignisse"
        print(f"{len(only)} Clips mit nahen, fahrenden Fahrzeugen in {args.from_events}")

    def handle_sigint(_sig, _frame):
        global _shutdown
        if _shutdown:
            sys.exit(1)
        _shutdown = True
        print("\nAbbruch: aktueller Clip wird noch fertig geladen (nochmal Ctrl+C = sofort).", flush=True)

    signal.signal(signal.SIGINT, handle_sigint)
    asyncio.run(fetch(cam, config["username"], output_dir(config), args.stream, d0, d1, only, selection))


if __name__ == "__main__":
    main()
