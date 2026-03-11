#!/usr/bin/env python3
"""
Timelapse generator from Reolink motion-triggered recordings.

Extracts the first N frames from each video, then selects frames that are
as evenly distributed as possible across the day's time span.
"""

import argparse
import re
import signal
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

FFMPEG = r"c:\localapps\ffmpeg\bin\ffmpeg.exe"
FFPROBE = r"c:\localapps\ffmpeg\bin\ffprobe.exe"

INPUT_DIR   = r"c:\video.in"  # parent of camera folders (or camera folder directly)
OUTPUT_DIR  = r"c:\video.out\timelapse"

# Which date(s) to process — "all" or e.g. "2026-02-12" or ["2026-02-12","2026-02-13"]
DATE = "2026-02-12"

# How many frames to grab from the beginning of each video
FRAMES_PER_VIDEO = 1

# Desired output video duration in seconds (TOTAL_OUTPUT_FRAMES = duration * fps)
# Override with --total-frames to set an exact frame count instead.
OUTPUT_DURATION = 30

# Day window to consider (inclusive); set to None for auto (first–last video)
DAY_START = "16:00"   # e.g. "06:00"  or None
DAY_END   = "21:00"   # e.g. "22:00"  or None

# Output video settings
OUTPUT_FPS    = 10
OUTPUT_WIDTH  = None   # set to None to keep original
OUTPUT_HEIGHT = None   # set to None to keep original

# Video filename timestamp pattern: 2026-02-12_17-14-18.mp4
FILENAME_RE = re.compile(r"(\d{4}-\d{2}-\d{2})_(\d{2}-\d{2}-\d{2})\.mp4$", re.IGNORECASE)

# ---------------------------------------------------------------------------

def parse_video_start(path: Path) -> datetime | None:
    m = FILENAME_RE.match(path.name)
    if not m:
        return None
    return datetime.strptime(f"{m.group(1)} {m.group(2)}", "%Y-%m-%d %H-%M-%S")


def get_video_fps(path: Path) -> float:
    """Use ffprobe to get the frame rate of a video."""
    cmd = [
        FFPROBE, "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=r_frame_rate",
        "-of", "csv=p=0",
        str(path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    raw = result.stdout.strip()
    if "/" in raw:
        num, den = raw.split("/")
        return float(num) / float(den)
    try:
        return float(raw)
    except ValueError:
        return 25.0  # fallback


def collect_frames(date_dir: Path, frames_per_video: int) -> list[tuple[datetime, Path, int]]:
    """
    Return list of (timestamp, video_path, frame_index) for each candidate frame.
    frame_index is 0-based (frame number within the video, to be extracted).
    """
    videos = sorted(
        [p for p in date_dir.iterdir() if FILENAME_RE.match(p.name)],
        key=lambda p: p.name,
    )
    frames: list[tuple[datetime, Path, int]] = []
    for video in videos:
        start = parse_video_start(video)
        if start is None:
            continue
        fps = get_video_fps(video)
        for i in range(frames_per_video):
            ts = start + timedelta(seconds=i / fps)
            frames.append((ts, video, i))
    return frames


def select_evenly(
    frames: list[tuple[datetime, Path, int]],
    total: int,
    day_start: datetime | None,
    day_end: datetime | None,
) -> list[tuple[datetime, Path, int]]:
    """
    From all candidate frames, pick `total` that are as evenly distributed
    over [day_start, day_end] as possible.
    """
    if not frames:
        return []

    t0 = day_start or frames[0][0]
    t1 = day_end   or frames[-1][0]

    span = (t1 - t0).total_seconds()
    if span <= 0 or total <= 1:
        return frames[:total]

    # Build target times
    targets = [t0 + timedelta(seconds=span * i / (total - 1)) for i in range(total)]

    # For each target, find the closest frame
    selected_indices: set[int] = set()
    result: list[tuple[datetime, Path, int]] = []

    for target in targets:
        best_idx = min(
            range(len(frames)),
            key=lambda i: abs((frames[i][0] - target).total_seconds()),
        )
        if best_idx not in selected_indices:
            selected_indices.add(best_idx)
            result.append(frames[best_idx])

    result.sort(key=lambda x: x[0])
    return result


def extract_frame(video: Path, frame_index: int, out_path: Path) -> bool:
    """Extract a single frame from a video by frame number."""
    cmd = [
        FFMPEG, "-y",
        "-i", str(video),
        "-vf", f"select=eq(n\\,{frame_index})",
        "-vframes", "1",
        "-q:v", "2",
        str(out_path),
    ]
    result = subprocess.run(cmd, capture_output=True)
    return result.returncode == 0


def build_timelapse(frame_dir: Path, output_path: Path, fps: int, width: int | None, height: int | None) -> None:
    """Assemble numbered frames into a timelapse video."""
    vf_parts = []
    if width and height:
        vf_parts.append(f"scale={width}:{height}")

    cmd = [
        FFMPEG, "-y",
        "-framerate", str(fps),
        "-i", str(frame_dir / "frame_%06d.jpg"),
    ]
    if vf_parts:
        cmd += ["-vf", ",".join(vf_parts)]
    cmd += [
        "-c:v", "libx264",
        "-pix_fmt", "yuv420p",
        "-crf", "18",
        str(output_path),
    ]
    subprocess.run(cmd, check=True)


def collect_frames_for_date(date_str: str) -> list[tuple[datetime, Path, int]]:
    date_dir = Path(INPUT_DIR) / date_str
    if not date_dir.is_dir():
        print(f"  Skipping {date_str}: directory not found")
        return []
    frames = collect_frames(date_dir, FRAMES_PER_VIDEO)
    # Filter to day window
    if DAY_START:
        lo = datetime.strptime(f"{date_str} {DAY_START}", "%Y-%m-%d %H:%M")
        frames = [f for f in frames if f[0] >= lo]
    if DAY_END:
        hi = datetime.strptime(f"{date_str} {DAY_END}", "%Y-%m-%d %H:%M")
        frames = [f for f in frames if f[0] <= hi]
    return frames


def build_output(selected: list[tuple[datetime, Path, int]], output_path: Path) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        print(f"  Extracting {len(selected)} frames...")
        ok = 0
        for i, (ts, video, frame_idx) in enumerate(selected):
            out = tmp_path / f"frame_{i:06d}.jpg"
            if extract_frame(video, frame_idx, out):
                ok += 1
            else:
                print(f"    Warning: failed to extract frame {frame_idx} from {video.name}")
        print(f"  Extracted {ok}/{len(selected)} frames")
        print(f"  Building timelapse -> {output_path}")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        build_timelapse(tmp_path, output_path, OUTPUT_FPS, OUTPUT_WIDTH, OUTPUT_HEIGHT)
        print(f"  Done: {output_path}")


def find_date_roots(path: Path, max_depth: int = 5) -> list[Path]:
    """Find all directories (up to max_depth levels deep) that contain YYYY-MM-DD subdirs."""
    date_re = re.compile(r"^\d{4}-\d{2}-\d{2}$")
    results: list[Path] = []

    def _walk(current: Path, depth: int) -> None:
        if depth > max_depth:
            return
        try:
            children = [d for d in current.iterdir() if d.is_dir()]
        except PermissionError:
            return
        if any(date_re.match(d.name) for d in children):
            results.append(current)
        else:
            for child in children:
                _walk(child, depth + 1)

    _walk(path, 0)
    return results


def find_date_root(path: Path) -> Path:
    """Resolve input path to the directory containing YYYY-MM-DD folders.
    Errors with a helpful message if ambiguous."""
    roots = find_date_roots(path)
    if len(roots) == 1:
        return roots[0]
    if len(roots) == 0:
        print(f"Error: no date folders (YYYY-MM-DD) found under {path}", file=sys.stderr)
        sys.exit(1)
    print(f"Error: multiple camera directories found under {path}.", file=sys.stderr)
    print("Use --input-dir to specify one of:", file=sys.stderr)
    for r in roots:
        print(f"  {r}", file=sys.stderr)
    sys.exit(1)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Timelapse generator from Reolink motion-triggered recordings.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--date", default=DATE,
                   help='Date to process: "all", "YYYY-MM-DD", or comma-separated list')
    p.add_argument("--from", dest="date_from", default=None, metavar="YYYY-MM-DD",
                   help="Process dates from this date (inclusive)")
    p.add_argument("--to", dest="date_to", default=None, metavar="YYYY-MM-DD",
                   help="Process dates up to this date (inclusive)")
    p.add_argument("--frames-per-video", type=int, default=FRAMES_PER_VIDEO, metavar="N",
                   help="Frames to extract from the start of each video")
    p.add_argument("--duration", type=float, default=None, metavar="SEC",
                   help="Desired output video length in seconds (default: 10s per day)")
    p.add_argument("--total-frames", type=int, default=None, metavar="N",
                   help="Override total frames directly (ignores --duration)")
    p.add_argument("--day-start", default=DAY_START, metavar="HH:MM",
                   help="Start of day window (or empty for auto)")
    p.add_argument("--day-end", default=DAY_END, metavar="HH:MM",
                   help="End of day window (or empty for auto)")
    p.add_argument("--fps", type=int, default=OUTPUT_FPS,
                   help="Output video frame rate")
    p.add_argument("--width", type=int, default=OUTPUT_WIDTH,
                   help="Output width in pixels (None = keep original)")
    p.add_argument("--height", type=int, default=OUTPUT_HEIGHT,
                   help="Output height in pixels (None = keep original)")
    p.add_argument("--input-dir", default=INPUT_DIR,
                   help="Root directory containing date folders")
    p.add_argument("--output-dir", default=OUTPUT_DIR,
                   help="Directory for output timelapse videos")
    return p.parse_args()


def main() -> None:
    signal.signal(signal.SIGINT, lambda *_: (print("\nAborted."), sys.exit(1)))

    args = parse_args()

    # Apply CLI overrides to globals used by process_date
    global INPUT_DIR, OUTPUT_DIR, FRAMES_PER_VIDEO, TOTAL_OUTPUT_FRAMES, OUTPUT_DURATION
    global DAY_START, DAY_END, OUTPUT_FPS, OUTPUT_WIDTH, OUTPUT_HEIGHT
    INPUT_DIR           = args.input_dir
    OUTPUT_DIR          = args.output_dir
    FRAMES_PER_VIDEO    = args.frames_per_video
    OUTPUT_FPS          = args.fps
    DAY_START           = args.day_start or None
    DAY_END             = args.day_end   or None
    OUTPUT_WIDTH        = args.width
    OUTPUT_HEIGHT       = args.height

    input_root = find_date_root(Path(INPUT_DIR))
    INPUT_DIR = str(input_root)
    print(f"Input root: {input_root}")

    # Extract camera label (e.g. "cam1") from path components
    cam_re = re.compile(r"(cam\d+)", re.IGNORECASE)
    cam_label = next(
        (cam_re.search(p).group(1) for p in input_root.parts if cam_re.search(p)),
        None,
    )

    all_dates = sorted(
        d.name for d in input_root.iterdir()
        if d.is_dir() and re.match(r"\d{4}-\d{2}-\d{2}$", d.name)
    )

    if args.date_from or args.date_to:
        lo = args.date_from or "0000-00-00"
        hi = args.date_to   or "9999-99-99"
        dates = [d for d in all_dates if lo <= d <= hi]
    else:
        date_arg = args.date
        if date_arg == "all":
            dates = all_dates
        elif "," in date_arg:
            dates = [d.strip() for d in date_arg.split(",")]
        else:
            dates = [date_arg]

    print(f"Processing {len(dates)} date(s): {dates}")

    # Collect all candidate frames across all dates
    all_frames: list[tuple[datetime, Path, int]] = []
    for date_str in dates:
        frames = collect_frames_for_date(date_str)
        print(f"  {date_str}: {len(frames)} candidate frames")
        all_frames.extend(frames)

    # Resolve output duration and frame count (after dates are known)
    if args.total_frames is not None:
        TOTAL_OUTPUT_FRAMES = args.total_frames
    else:
        duration = args.duration if args.duration is not None else len(dates) * 10
        TOTAL_OUTPUT_FRAMES = max(1, round(duration * OUTPUT_FPS))

    print(f"\nTotal candidate frames: {len(all_frames)}")
    print(f"Target: {TOTAL_OUTPUT_FRAMES} frames  =>  {TOTAL_OUTPUT_FRAMES / OUTPUT_FPS:.1f}s @ {OUTPUT_FPS}fps")

    # Select evenly across the full time span
    t0 = all_frames[0][0]  if all_frames else None
    t1 = all_frames[-1][0] if all_frames else None
    selected = select_evenly(all_frames, TOTAL_OUTPUT_FRAMES, t0, t1)
    print(f"Selected {len(selected)} evenly distributed frames")

    if not selected:
        print("Nothing to do.")
        sys.exit(0)

    # Output filename: cam_firstdate[_lastdate].mp4
    prefix = f"timelapse_{cam_label}_" if cam_label else "timelapse_"
    date_suffix = dates[0] if len(dates) == 1 else f"{dates[0]}_{dates[-1]}"
    output_path = Path(OUTPUT_DIR) / f"{prefix}{date_suffix}.mp4"

    build_output(selected, output_path)


if __name__ == "__main__":
    main()
