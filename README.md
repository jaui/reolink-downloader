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

---

# Timelapse Generator (`timelapse.py`)

Erstellt ein Timelapse-Video aus Reolink motion-triggered Aufnahmen.
Extrahiert die ersten N Frames aus jedem Video und wählt daraus eine
gleichmäßig über den Tag verteilte Auswahl aus.

## Voraussetzungen

FFmpeg muss unter `c:\localapps\ffmpeg\bin\` installiert sein (keine pip-Abhängigkeiten).

## Ablauf

```
1. Alle Videos eines Zeitraums einlesen (sortiert nach Zeitstempel im Dateinamen)
2. Aus jedem Video die ersten N Frames extrahieren (mit absolutem Zeitstempel)
3. Über den gesamten Zeitraum gleichmäßig verteilte Ziel-Zeitpunkte berechnen
4. Pro Ziel-Zeitpunkt den nächstgelegenen verfügbaren Frame wählen
5. Frames per ffmpeg zu einem einzelnen Timelapse-Video zusammensetzen
```

## Kommandozeilen-Optionen

```
  --input-dir PATH       Eingabeverzeichnis (kann cam1/, cam1/cam1_IP/ oder c:\video.in\ sein)
  --output-dir PATH      Ausgabeverzeichnis (Standard: c:\video.out\timelapse)
  --date YYYY-MM-DD      Einzelnes Datum (oder "all")
  --from YYYY-MM-DD      Startdatum (zusammen mit --to)
  --to   YYYY-MM-DD      Enddatum   (zusammen mit --from)
  --frames-per-video N   Frames vom Anfang jedes Videos (Standard: 1)
  --duration SEC         Gewünschte Ausgabelänge in Sekunden (Standard: 10s pro Tag)
  --total-frames N       Feste Frameanzahl (überschreibt --duration)
  --day-start HH:MM      Beginn des Tagesfensters (Standard: 16:00)
  --day-end   HH:MM      Ende des Tagesfensters   (Standard: 21:00)
  --fps N                Framerate des Ausgabevideos (Standard: 10)
  --width / --height     Auflösung skalieren (Standard: original)
```

## Ausgabe-Dateiname

```
timelapse_{cam}_{datum}.mp4              # einzelner Tag
timelapse_{cam}_{startdatum}_{enddatum}.mp4   # Zeitraum
```

Der Kamera-Name (`cam1`, `cam2`, …) wird automatisch aus dem Pfad extrahiert.

## Beispiele

```bash
# Alle Tage, automatische Dauer (10s/Tag), Standardfenster 16-21 Uhr
python timelapse.py --input-dir c:\video.in --from 2026-02-12 --to 2026-02-28

# Mehr Kandidaten durch breiteres Zeitfenster und mehr Frames pro Video
python timelapse.py --input-dir c:\video.in --from 2026-02-12 --to 2026-02-28 \
    --day-start "" --day-end "" --frames-per-video 3

# Feste Ausgabelänge 60 Sekunden
python timelapse.py --input-dir c:\video.in --date all --duration 60

# Nur ein Tag, halbe Auflösung
python timelapse.py --input-dir c:\video.in --date 2026-02-15 --width 960 --height 540
```
