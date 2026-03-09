# Reolink Downloader

Kommandozeilen-Tool zum Herunterladen und Auflisten von Aufnahmen auf Reolink-Kameras über die lokale API.

## Setup

```bash
# Virtuelle Umgebung erstellen und aktivieren
python -m venv venv
venv\Scripts\activate

# Abhängigkeiten installieren
pip install -r requirements.txt

# Passwort einmalig speichern (Windows Credential Manager)
python reolink-downloader.py --setup-password
```

## Konfiguration

Kameras werden in `config.json` definiert:

```json
{
    "cameras": [
        {"name": "cam1", "host": "192.168.1.40", "channel": 0},
        {"name": "cam2", "host": "192.168.1.41", "channel": 0}
    ],
    "username": "reolink",
    "output_dir": "downloads",
    "stream": "main"
}
```

| Feld         | Beschreibung                                      |
|--------------|---------------------------------------------------|
| `cameras`    | Liste der Kameras mit Name, IP und Kanal           |
| `username`   | Benutzername (gemeinsam für alle Kameras)           |
| `output_dir` | Standard-Ausgabeverzeichnis                        |
| `stream`     | Stream-Typ: `main` (hohe Auflösung) oder `sub`    |

## Ablauf (Flow)

```
1. Konfiguration laden (config.json)
2. Passwort aus Windows Credential Manager lesen
3. Pro Kamera:
   a. Verbindung aufbauen (Login + Host-Daten laden)
   b. Zeitraum in einzelne Tage aufteilen
      (API-Limit: max ~200 Ergebnisse pro Abfrage)
   c. Tagesweise VOD-Dateien abfragen
   d. Je nach Modus:
      --list:  Ergebnisse anzeigen (Statistik oder Einzelauflistung)
      default: Dateien herunterladen (1 MB Chunks, Fortschritt + ETA)
   e. Verbindung trennen (Logout)
```

**Download-Ablauf im Detail:**

```
Für jede gefundene Aufnahme:
  ├── Datei existiert bereits? → Überspringen (oder überschreiben mit --force)
  ├── Zielverzeichnis anlegen: <output>/<kamera_ip>/<datum>/
  ├── Datei herunterladen als: <datum>_<uhrzeit>.mp4
  └── Fortschritt anzeigen: [Nr/Gesamt %] [Speed, ETA] Dateiname
```

**Listing-Verhalten:**
- Mehr als 100 Aufnahmen → Tagesstatistik (Anzahl, Dauer, Größe pro Tag)
- 100 oder weniger → Einzelauflistung mit Start-/Endzeit und Dateiname
- Falls im gewählten Stream keine Aufnahmen: automatischer Fallback auf anderen Stream

## Kommandozeilen-Optionen

```
Optionen:
  -l, --list              Aufnahmen auflisten (nicht herunterladen)
  -d, --date YYYY-MM-DD   Bestimmtes Datum
  --from YYYY-MM-DD       Startdatum (zusammen mit --to)
  --to YYYY-MM-DD         Enddatum (zusammen mit --from)
  --camera NAME           Nur bestimmte Kamera (Name oder IP)
  -o, --output PFAD       Ausgabeverzeichnis
  --force                 Existierende Dateien überschreiben
  --setup-password        Passwort im Credential Manager speichern
```

## Beispiele

### Passwort einrichten

```bash
python reolink-downloader.py --setup-password
```

### Aufnahmen auflisten

```bash
# Alle Kameras, letzte 90 Tage
python reolink-downloader.py -l

# Bestimmter Tag, eine Kamera
python reolink-downloader.py -l -d 2026-03-07 --camera cam1

# Zeitraum
python reolink-downloader.py -l --from 2026-02-01 --to 2026-02-28
```

### Aufnahmen herunterladen

```bash
# Gestern (Standard), alle Kameras
python reolink-downloader.py

# Bestimmter Tag
python reolink-downloader.py -d 2026-03-07

# Zeitraum, eine Kamera, eigenes Verzeichnis
python reolink-downloader.py --camera cam2 --from 2026-01-29 --to 2026-02-28 -o D:\backup

# Erneut herunterladen (existierende überschreiben)
python reolink-downloader.py -d 2026-03-07 --force
```

### Abbrechen

`Ctrl+C` beendet den Download nach der aktuellen Datei sauber.

## Dateistruktur der Downloads

```
downloads/
├── cam1_192.168.1.40/
│   ├── 2026-03-07/
│   │   ├── 2026-03-07_08-15-30.mp4
│   │   ├── 2026-03-07_08-20-45.mp4
│   │   └── ...
│   └── 2026-03-08/
│       └── ...
└── cam2_192.168.1.41/
    └── ...
```

## Abhängigkeiten

- `reolink-aio` — Async Reolink API Client
- `keyring` — Sichere Passwortspeicherung (Windows Credential Manager)
- `aiohttp` — HTTP-Client für Downloads
