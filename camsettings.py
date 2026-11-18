#!/usr/bin/env python3
"""
Liest Bild- und Nachteinstellungen einer Kamera aus (nur lesend) und gibt sie als JSON aus:
GetIsp, GetIrLights, GetImage, GetWhiteLed, GetEnc, GetDevInfo.

    python camsettings.py --camera garage2
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

import keyring
from reolink_aio.api import Host

CONFIG_FILE = Path(__file__).resolve().parent / "config.json"
KEYRING_SERVICE = "reolink-downloader"
COMMANDS = ["GetIsp", "GetIrLights", "GetImage", "GetWhiteLed", "GetEnc", "GetDevInfo"]


async def dump(cam: dict, username: str) -> None:
    password = keyring.get_password(KEYRING_SERVICE, username)
    if not password:
        sys.exit("Fehler: Kein Passwort gespeichert. Zuerst: python reolink-downloader.py --setup-password")
    host = Host(cam["host"], username, password, use_https=False)
    try:
        await host.login()
        for cmd in COMMANDS:
            try:
                res = await host.send([{"cmd": cmd, "action": 1, "param": {"channel": cam.get("channel", 0)}}],
                                      expected_response_type="json")
                print(f"===== {cmd}\n{json.dumps(res, indent=1, ensure_ascii=False)}")
            except Exception as e:
                print(f"===== {cmd}: {type(e).__name__}: {e}")
    finally:
        await host.logout()


def main():
    p = argparse.ArgumentParser(description="Kameraeinstellungen auslesen (nur lesend)")
    p.add_argument("--camera", required=True, help="Kameraname aus config.json")
    args = p.parse_args()
    with open(CONFIG_FILE, encoding="utf-8") as f:
        config = json.load(f)
    cam = next((c for c in config.get("cameras", []) if c["name"] == args.camera), None)
    if cam is None:
        sys.exit(f"Fehler: Kamera '{args.camera}' nicht in config.json")
    asyncio.run(dump(cam, config["username"]))


if __name__ == "__main__":
    main()
