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

Download: [ffmpeg-master-latest-win64-gpl-shared.zip](https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl-shared.zip)
→ Inhalt nach `c:\localapps\ffmpeg\` entpacken (sodass `c:\localapps\ffmpeg\bin\ffmpeg.exe` existiert).

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

---

# Erkennungs-Pipeline (Personen, Fahrzeuge, Kennzeichen)

Zweistufig, damit nicht alle 12-MP-Clips geladen werden müssen:

1. **Sub-Stream laden** (klein, ~1/5 der Größe) und **Personen/Fahrzeuge erkennen** (`detect.py`).
2. Nur Clips mit **nahen, fahrenden Fahrzeugen** im **Main-Stream** nachladen und **Kennzeichen lesen** (`plates_scan.py`).
3. Ergebnisse im **Ereignis-Viewer** ansehen (`viewer.py`, siehe unten).

## Werkzeuge

Erkennung und Kennzeichen laufen in einem eigenen venv unter `c:\localapps\alpr` (nur CPU):

```bash
python -m venv c:\localapps\alpr\venv
c:\localapps\alpr\venv\Scripts\pip install torch --index-url https://download.pytorch.org/whl/cpu
c:\localapps\alpr\venv\Scripts\pip install ultralytics "fast-alpr[onnx]" opencv-python-headless truststore lap
```

Modelle liegen in `c:\localapps\alpr\models` (`yolo11n.pt`, fast-alpr-Modelle werden beim ersten Lauf dorthin geladen). ffmpeg wie beim Timelapse unter `c:\localapps\ffmpeg\bin`.

## Ablauf

```bash
# 1. Sub-Stream laden (je Kamera nur ein Download gleichzeitig; Kameras parallel möglich)
python fetch_clips.py --camera garage2 --stream sub --from 2026-10-22 --to 2026-11-18

# 2. Stufe 1: Personen + Fahrzeuge (dauerhaft, alle 10 min neue Clips)
c:\localapps\alpr\venv\Scripts\python detect.py --camera garage2 --classes all --watch 600
c:\localapps\alpr\venv\Scripts\python detect.py --camera schuppen2 --classes person

# 3. Main-Stream nur für Clips mit nahen, fahrenden Fahrzeugen nachladen
python fetch_clips.py --camera garage2 --stream main --from 2026-10-22 --to 2026-11-18 ^
    --from-events downloads\detections\garage2\events.jsonl

# 4. Stufe 2: Kennzeichen lesen
c:\localapps\alpr\venv\Scripts\python plates_scan.py --camera garage2

# Kameraeinstellungen (Bild/Nacht/IR) auslesen
python camsettings.py --camera garage2
```

| Skript | wichtige Optionen |
|---|---|
| `fetch_clips.py` | `--camera`, `--stream main\|sub`, `--from`, `--to`, `--only <liste.txt>`, `--from-events <events.jsonl>`, `--min-area` (0.02), `--min-path` (0.05) |
| `detect.py` | `--camera` oder Ordner/Dateien + `--out`, `--classes person\|vehicle\|all`, `--fps` (2), `--from`, `--to`, `--watch SEK` |
| `plates_scan.py` | `--camera` oder Ordner/Dateien + `--out`, `--from`, `--to`, `--fps` (2), `--min-plate-area` (0.008), `--keep-area` (0.02) |

Alle Schritte sind fortsetzbar (`done.txt` bzw. vorhandene Dateien werden übersprungen).

**Nachts** überstrahlt die IR-Beleuchtung die reflektierenden Kennzeichen; Lesungen gelingen praktisch nur bei Tageslicht oder im Farbmodus.

---

# Ereignis-Viewer (`viewer.py`)

Kleine Web-App (Flask) zum Durchsehen der erkannten **Personen** und **Fahrzeuge** inkl. Kennzeichen-Lesungen. Läuft lokal; Bilder und Videos verlassen den Rechner nicht.

- Filter: Kamera, Personen/Fahrzeuge, Tag/Nacht, Zeitraum, „Fahrzeuge nur nah & bewegt" (blendet geparkte Autos und entfernten Verkehr aus), Suche nach Kennzeichen oder Fahrzeugname
- Drilldown: Jahre → Monate → Tage → Stunden → Detail mit Video, Bildausschnitten, Kennzeichen; Start ist der aktuelle Tag. Jede Zeile hat einen Zeitstreifen (Monate/Tage/Stunden), ein Klick auf ein Fach springt direkt dorthin
- Personenzahl auf den Karten: `2` = zwei Spuren, gleichzeitig im Bild; `1–3` = höchstens eine gleichzeitig, aber drei Spuren (eine Person bekommt nach Verdeckung oft eine neue Spur)
- Nachklassifikation im Detail: Klasse korrigieren (Person, Fahrrad, Auto, LKW/Transporter, Bus, Motorrad, Tier, Fehlalarm), bestätigen, Fahrzeug aus der Watchlist zuordnen oder Kennzeichen von Hand eintragen (danach per Suche auffindbar). Tasten: `B` bestätigen, `F` Fehlalarm, `P` Person, `A` Auto, `L` LKW, `T` Tier. Für stehende Fehlalarme (z. B. Gartenstuhl als „Person“) gibt es eine Regel „alle stehenden Erkennungen dieser Klasse an dieser Stelle“. Filter „Prüfung“: Alle / Offen / Geprüft / Fehlalarme
- Korrekturen liegen in `downloads/detections/labels.db` (SQLite); die Erkennungsdateien bleiben unverändert
- Responsiv (Handy: Detail als Vollbild), Zustand im URL-Hash (Zurück-Taste, Lesezeichen), Aktualisierung alle 60 s
- Download-Fortschritt: Karte je laufendem `fetch_clips.py` (geladen/erkannt, MB/s, Restzeit, aktueller Clip, Erkennung aktiv?) und je Tag in der Tagesübersicht; Tage ohne Ereignisse erscheinen ebenfalls (Zeitverlauf lückenlos)
- Darstellung: System / Hell (Sonne) / Dunkel (Mond), wird im Browser gemerkt

## Installation & Start

```bash
venv\Scripts\activate
pip install -r requirements-viewer.txt
python viewer.py                      # http://127.0.0.1:8765/
python viewer.py --host 0.0.0.0       # im LAN erreichbar (z. B. Handy), Zugriff nur mit ausgegebenem Token-Link
```

| Option | Bedeutung |
|---|---|
| `--port` | Port (Standard 8765) |
| `--host` | Adresse (Standard 127.0.0.1; bei anderer Adresse wird ein Zufallstoken erzeugt) |
| `--det-dir` | Ordner mit den Erkennungsdaten (Standard `<output_dir>/detections`) |
| `--plates-camera` | Kamera, zu der `plates/tracks.jsonl` gehört (Standard `garage2`) |

## Watchlist

`watchlist.json` neben dem Skript (nicht im Git, Vorlage `watchlist.json.example`):

```json
{ "AB CD 123": "Beispielauto blau" }
```

Treffer werden grün markiert: `exakt`, `teil` (Teil hinter dem Ortskürzel), `fuzzy` (1 Zeichen Abweichung) oder `neben` (Nebenlesung).

## Erwartete Daten

```
downloads/detections/
├── <kamera>/events.jsonl     # eine Zeile je Track: cls, video ("<datum>/<datei>.mp4"), t_first, t_last,
│                             # best_t, max_area, path, max_conf, ir, box, crop
├── <kamera>/crops/*.jpg
└── plates/tracks.jsonl       # Kennzeichen-Lesungen (Main-Stream): video, reads[{t,text,conf,png}], veh_jpg, color
    plates/veh/*.jpg, plates/plates/*.png
```

Videos werden aus der Download-Ablage abgespielt, bevorzugt der Sub-Stream (`<name>_sub_<host>`, H.264). Main-Stream-Clips (H.265, 12 MP) kann der Browser meist nicht abspielen; dann wird ein Download-Link angezeigt.
