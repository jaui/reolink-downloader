#!/usr/bin/env python3
"""Reolink Kamera Video-Downloader.

Lädt Aufnahmen von Reolink-Kameras über die lokale API herunter.
Passwörter werden sicher im Windows Credential Manager gespeichert.
"""

import argparse
import asyncio
import json
import signal
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import keyring
from reolink_aio.api import Host
from reolink_aio.exceptions import ReolinkError

KEYRING_SERVICE = "reolink-downloader"
SCRIPT_DIR = Path(__file__).parent
CONFIG_PATH = SCRIPT_DIR / "config.json"

# Globales Flag für sauberes Beenden bei Ctrl+C
_shutdown = False


def load_config() -> dict:
    """Konfiguration aus config.json laden."""
    if not CONFIG_PATH.exists():
        print(f"Fehler: {CONFIG_PATH} nicht gefunden.")
        sys.exit(1)
    with open(CONFIG_PATH, "r") as f:
        return json.load(f)


def get_credentials(username: str) -> tuple[str, str]:
    """Benutzername und Passwort aus dem Credential Manager laden."""
    password = keyring.get_password(KEYRING_SERVICE, username)
    if password is None:
        print("Kein Passwort gespeichert. Bitte zuerst einrichten mit: --setup-password")
        sys.exit(1)
    return username, password


def setup_password(username: str) -> None:
    """Passwort im Windows Credential Manager speichern."""
    import getpass

    print(f"Passwort für Benutzer '{username}' einrichten.")
    password = getpass.getpass("Passwort eingeben: ")
    password_confirm = getpass.getpass("Passwort bestätigen: ")
    if password != password_confirm:
        print("Fehler: Passwörter stimmen nicht überein.")
        sys.exit(1)
    keyring.set_password(KEYRING_SERVICE, username, password)
    print("Passwort erfolgreich im Windows Credential Manager gespeichert.")


async def connect_camera(
    cam_config: dict,
    username: str,
    password: str,
    stream: str,
) -> tuple[Host, str, str, int]:
    """Verbindung zur Kamera herstellen. Gibt (host, cam_name, host_ip, channel) zurück."""
    host_ip = cam_config["host"]
    cam_name = cam_config["name"]
    channel = cam_config.get("channel", 0)

    host = Host(host_ip, username, password, use_https=False, stream=stream)
    await host.login()
    await host.get_host_data()
    return host, cam_name, host_ip, channel


def _day_ranges(start: datetime, end: datetime) -> list[tuple[datetime, datetime]]:
    """Zeitraum in Tagesblöcke aufteilen (API-Limit: ~200 Ergebnisse pro Abfrage)."""
    ranges = []
    current = start.replace(hour=0, minute=0, second=0, microsecond=0)
    while current <= end:
        day_start = max(current, start)
        day_end = min(current.replace(hour=23, minute=59, second=59), end)
        ranges.append((day_start, day_end))
        current += timedelta(days=1)
    return ranges


async def list_camera(
    cam_config: dict,
    username: str,
    password: str,
    start: datetime,
    end: datetime,
    stream: str,
) -> None:
    """Aufnahmen einer Kamera auflisten."""
    from collections import defaultdict

    host = None
    try:
        host, cam_name, host_ip, channel = await connect_camera(cam_config, username, password, stream)

        print(f"\n{'='*60}")
        print(f"Kamera: {cam_name} ({host_ip})")
        print(f"Zeitraum: {start.strftime('%Y-%m-%d %H:%M')} bis {end.strftime('%Y-%m-%d %H:%M')}")
        print(f"{'='*60}")

        # Tagesweise abfragen (API-Limit: ~200 Ergebnisse pro Abfrage)
        # Beide Stream-Typen prüfen falls im gewählten nichts gefunden wird
        all_vod_files = []
        day_ranges = _day_ranges(start, end)
        search_stream = None  # None = Host-Default

        for i, (d_start, d_end) in enumerate(day_ranges):
            try:
                _statuses, vod_files = await host.request_vod_files(
                    channel=channel, start=d_start, end=d_end, stream=search_stream,
                )
                if vod_files:
                    all_vod_files.extend(vod_files)
            except ReolinkError as e:
                if i == 0:
                    print(f"  Hinweis: {e}")
            # Fortschritt anzeigen
            if (i + 1) % 30 == 0 or i == len(day_ranges) - 1:
                print(f"  Suche... {i + 1}/{len(day_ranges)} Tage, {len(all_vod_files)} Aufnahme(n) bisher")

        # Falls nichts gefunden, anderen Stream-Typ versuchen
        if not all_vod_files:
            alt_stream = "sub" if stream == "main" else "main"
            print(f"  Keine Aufnahmen im '{stream}'-Stream. Versuche '{alt_stream}'...")
            search_stream = alt_stream
            for d_start, d_end in day_ranges:
                try:
                    _statuses, vod_files = await host.request_vod_files(
                        channel=channel, start=d_start, end=d_end, stream=search_stream,
                    )
                    if vod_files:
                        all_vod_files.extend(vod_files)
                except ReolinkError:
                    pass

        if not all_vod_files:
            print("  Keine Aufnahmen gefunden.")
            return

        total_size = 0
        total_duration = timedelta()

        if len(all_vod_files) > 100:
            # Bei vielen Aufnahmen: Statistik pro Tag
            days: dict[str, dict] = defaultdict(lambda: {"count": 0, "size": 0, "duration": timedelta()})

            for vod_file in all_vod_files:
                day = vod_file.start_time.strftime("%Y-%m-%d")
                days[day]["count"] += 1
                days[day]["size"] += vod_file.size
                days[day]["duration"] += vod_file.duration
                total_size += vod_file.size
                total_duration += vod_file.duration

            print(f"\n  {'Datum':<12} {'Anzahl':>7} {'Dauer':>10} {'Größe':>10}")
            print(f"  {'-'*12} {'-'*7} {'-'*10} {'-'*10}")
            for day in sorted(days):
                d = days[day]
                print(
                    f"  {day:<12} {d['count']:>7} "
                    f"{d['duration'].total_seconds() / 60:>8.1f} m "
                    f"{d['size'] / (1024 * 1024):>8.1f} MB"
                )
        else:
            # Bei wenigen Aufnahmen: Einzelauflistung
            for vod_file in all_vod_files:
                size_mb = vod_file.size / (1024 * 1024)
                duration = vod_file.duration
                total_size += vod_file.size
                total_duration += duration
                print(
                    f"  {vod_file.start_time.strftime('%Y-%m-%d %H:%M:%S')} - "
                    f"{vod_file.end_time.strftime('%H:%M:%S')}  "
                    f"{duration.total_seconds() / 60:5.1f} Min  "
                    f"{size_mb:7.1f} MB  "
                    f"{vod_file.file_name}"
                )

        print(f"\n  Gesamt: {len(all_vod_files)} Aufnahme(n), "
              f"{total_duration.total_seconds() / 60:.1f} Min, "
              f"{total_size / (1024 * 1024):.1f} MB")

    except ReolinkError as e:
        print(f"  Fehler bei Kamera {cam_config['name']}: {e}")
    finally:
        if host:
            await host.logout()


async def download_camera(
    cam_config: dict,
    username: str,
    password: str,
    start: datetime,
    end: datetime,
    output_dir: Path,
    stream: str,
    force: bool = False,
) -> int:
    """Aufnahmen einer Kamera herunterladen. Gibt Anzahl heruntergeladener Dateien zurück."""
    host = None
    downloaded = 0

    try:
        host, cam_name, host_ip, channel = await connect_camera(cam_config, username, password, stream)

        print(f"\n{'='*60}")
        print(f"Kamera: {cam_name} ({host_ip})")
        print(f"Zeitraum: {start.strftime('%Y-%m-%d %H:%M')} bis {end.strftime('%Y-%m-%d %H:%M')}")
        print(f"{'='*60}")

        # Tagesweise abfragen (API-Limit: ~200 Ergebnisse pro Abfrage)
        all_vod_files = []
        day_ranges = _day_ranges(start, end)

        for i, (d_start, d_end) in enumerate(day_ranges):
            if _shutdown:
                break
            try:
                _statuses, vod_files = await host.request_vod_files(
                    channel=channel, start=d_start, end=d_end,
                )
                if vod_files:
                    all_vod_files.extend(vod_files)
            except ReolinkError as e:
                if i == 0:
                    print(f"  Hinweis: {e}")
            if len(day_ranges) > 1 and ((i + 1) % 30 == 0 or i == len(day_ranges) - 1):
                print(f"  Suche... {i + 1}/{len(day_ranges)} Tage, {len(all_vod_files)} Aufnahme(n) bisher")

        if not all_vod_files:
            print("  Keine Aufnahmen im angegebenen Zeitraum gefunden.")
            return 0

        total_size = sum(v.size for v in all_vod_files if v.size)
        total_size_mb = total_size / (1024 * 1024)
        print(f"  {len(all_vod_files)} Aufnahme(n) gefunden ({total_size_mb:.1f} MB gesamt).")

        skipped = 0
        skipped_bytes = 0
        actual_dl_bytes = 0
        dl_start_time = None

        for idx, vod_file in enumerate(all_vod_files):
            if _shutdown:
                print("\n  Abbruch durch Benutzer.")
                break

            date_str = vod_file.start_time.strftime("%Y-%m-%d")
            time_str = vod_file.start_time.strftime("%H-%M-%S")
            cam_dir = output_dir / f"{cam_name}_{host_ip}" / date_str
            cam_dir.mkdir(parents=True, exist_ok=True)

            filename = f"{date_str}_{time_str}.mp4"
            filepath = cam_dir / filename

            if filepath.exists() and not force:
                skipped += 1
                skipped_bytes += vod_file.size if vod_file.size else 0
                continue

            size_mb = vod_file.size / (1024 * 1024) if vod_file.size else 0
            done_bytes = skipped_bytes + actual_dl_bytes
            progress_pct = done_bytes / total_size * 100 if total_size else 0

            # ETA nur auf tatsächlich heruntergeladene Bytes basieren
            if dl_start_time is None:
                dl_start_time = time.monotonic()
            elapsed = time.monotonic() - dl_start_time
            if downloaded > 0 and elapsed > 1:
                speed_bps = actual_dl_bytes / elapsed
                remaining_bytes = total_size - done_bytes
                eta_sec = remaining_bytes / speed_bps if speed_bps > 0 else 0
                if eta_sec >= 3600:
                    eta_str = f"{eta_sec / 3600:.1f}h"
                elif eta_sec >= 60:
                    eta_str = f"{eta_sec / 60:.0f}m"
                else:
                    eta_str = f"{eta_sec:.0f}s"
                speed_mb = speed_bps / (1024 * 1024)
                eta_info = f" [{speed_mb:.1f} MB/s, ETA {eta_str}]"
            else:
                eta_info = ""

            print(
                f"  [{idx + 1}/{len(all_vod_files)} {progress_pct:.0f}%]{eta_info} "
                f"{filename} ({size_mb:.1f} MB)...",
                end="",
                flush=True,
            )

            try:
                vod_download = await host.download_vod(filename=vod_file.file_name)

                with open(filepath, "wb") as f:
                    while True:
                        chunk = await vod_download.stream.read(1024 * 1024)
                        if not chunk:
                            break
                        f.write(chunk)

                vod_download.close()
                downloaded += 1
                actual_dl_bytes += vod_file.size if vod_file.size else 0
                print(" OK")
            except ReolinkError as e:
                print(f" FEHLER: {e}")
                if filepath.exists():
                    filepath.unlink()

        if skipped > 0:
            print(f"  {skipped} Datei(en) übersprungen (existieren bereits).")

    except ReolinkError as e:
        print(f"  Fehler bei Kamera {cam_config['name']}: {e}")
    finally:
        if host:
            await host.logout()

    return downloaded


async def main_async(args: argparse.Namespace) -> None:
    """Hauptlogik (async)."""
    config = load_config()
    username = config.get("username", "admin")

    if args.setup_password:
        setup_password(username)
        return

    username, password = get_credentials(username)

    # Zeitraum bestimmen
    if args.date:
        start_date = datetime.strptime(args.date, "%Y-%m-%d")
        start = start_date.replace(hour=0, minute=0, second=0)
        end = start_date.replace(hour=23, minute=59, second=59)
    elif args.start and args.end:
        start = datetime.strptime(args.start, "%Y-%m-%d").replace(hour=0, minute=0, second=0)
        end = datetime.strptime(args.end, "%Y-%m-%d").replace(hour=23, minute=59, second=59)
    elif args.list:
        # --list ohne Datum: letzte 90 Tage
        end = datetime.now().replace(hour=23, minute=59, second=59, microsecond=0)
        start = (end - timedelta(days=90)).replace(hour=0, minute=0, second=0, microsecond=0)
    else:
        # Standard: gestern
        yesterday = datetime.now() - timedelta(days=1)
        start = yesterday.replace(hour=0, minute=0, second=0, microsecond=0)
        end = yesterday.replace(hour=23, minute=59, second=59, microsecond=0)

    output_dir = Path(args.output) if args.output else SCRIPT_DIR / config.get("output_dir", "downloads")
    output_dir.mkdir(parents=True, exist_ok=True)

    stream = config.get("stream", "main")

    # Kameras filtern
    cameras = config["cameras"]
    if args.camera:
        cameras = [c for c in cameras if c["name"] == args.camera or c["host"] == args.camera]
        if not cameras:
            print(f"Fehler: Kamera '{args.camera}' nicht in config.json gefunden.")
            sys.exit(1)

    if args.list:
        for cam in cameras:
            await list_camera(cam, username, password, start, end, stream)
        return

    total_downloaded = 0
    for cam in cameras:
        if _shutdown:
            break
        count = await download_camera(cam, username, password, start, end, output_dir, stream, args.force)
        total_downloaded += count

    if _shutdown:
        print(f"\nAbgebrochen. {total_downloaded} Datei(en) heruntergeladen nach: {output_dir}")
    else:
        print(f"\nFertig! {total_downloaded} Datei(en) heruntergeladen nach: {output_dir}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Reolink Kamera Video-Downloader",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Beispiele:
  %(prog)s --setup-password          Passwort einrichten
  %(prog)s                           Aufnahmen von gestern herunterladen
  %(prog)s -d 2026-03-07             Aufnahmen eines bestimmten Tages
  %(prog)s --from 2026-03-01 --to 2026-03-07   Zeitraum
  %(prog)s -l -d 2026-03-08            Aufnahmen auflisten
  %(prog)s --camera cam1             Nur eine Kamera
        """,
    )
    parser.add_argument(
        "-l", "--list",
        action="store_true",
        help="Aufnahmen nur auflisten, nicht herunterladen",
    )
    parser.add_argument(
        "--setup-password",
        action="store_true",
        help="Passwort im Windows Credential Manager speichern",
    )
    parser.add_argument(
        "-d", "--date",
        help="Datum der Aufnahmen (Format: YYYY-MM-DD)",
    )
    parser.add_argument(
        "--from", dest="start",
        help="Startdatum (Format: YYYY-MM-DD)",
    )
    parser.add_argument(
        "--to", dest="end",
        help="Enddatum (Format: YYYY-MM-DD)",
    )
    parser.add_argument(
        "--camera",
        help="Nur bestimmte Kamera (Name oder IP)",
    )
    parser.add_argument(
        "-o", "--output",
        help="Ausgabeverzeichnis (Standard: downloads/)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Existierende Dateien überschreiben",
    )

    args = parser.parse_args()

    if args.start and not args.end or args.end and not args.start:
        parser.error("--from und --to müssen zusammen angegeben werden.")

    def handle_sigint(_sig, _frame):
        global _shutdown
        _shutdown = True
        print("\n\nCtrl+C erkannt - beende nach aktuellem Download...")

    signal.signal(signal.SIGINT, handle_sigint)
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
