#!/usr/bin/env python3
"""
compress_media.py — Recursively clone a folder of photos/videos into a
compressed copy, preserving folder structure, orientation, and metadata.

Designed with Samsung Galaxy S10 media in mind:
  * Videos: .mp4 (H.264/HEVC, incl. 60fps & 4K), rotation stored either
    physically or as a display-matrix "rotate" flag -> both handled.
  * Photos: .jpg with EXIF Orientation tag, optional .heic (HEIF).

Orientation strategy
  - Videos: ffmpeg auto-applies the rotation display matrix when
    re-encoding, so the output is always displayed correctly.
  - Photos: EXIF Orientation is baked into the pixels via
    ImageOps.exif_transpose, then the tag is reset so no viewer
    double-rotates. All other EXIF (date taken, GPS, ...) is kept.

Requires: ffmpeg + ffprobe on PATH, and:
    python3 -m pip install pillow rich
Optional: python3 -m pip install pillow-heif   (for .heic input)

Usage:
  python3 compress_media.py /path/to/DCIM            # interactive quality menu
  python3 compress_media.py /path/to/DCIM --video-quality 26 --photo-quality 85
  python3 compress_media.py /path/to/DCIM --codec h264 --dry-run

Output: a sibling folder next to the input, e.g.
  /Documents/DCIM  ->  /Documents/DCIM_compressed
"""

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageOps

try:
    from rich.console import Console
    from rich.panel import Panel
    from rich.progress import (BarColumn, Progress, SpinnerColumn, TaskProgressColumn,
                               TextColumn, TimeElapsedColumn, TimeRemainingColumn)
    from rich.prompt import IntPrompt, Prompt
    from rich.table import Table
except ImportError:
    sys.exit("This script needs the 'rich' package for its UI.\n"
             "Install it with:  python3 -m pip install rich")

try:  # optional HEIC support (Samsung "High efficiency pictures" mode)
    from pillow_heif import register_heif_opener
    register_heif_opener()
    HEIC_OK = True
except ImportError:
    HEIC_OK = False

console = Console()

# ----------------------------------------------------------------------------
# COMPRESSION SETTINGS / PRESETS
# ----------------------------------------------------------------------------
# Video CRF (Constant Rate Factor): lower = better quality / bigger file.
#   libx265 sweet spots: 24 near-transparent .. 30 visibly compressed.
#   libx264 equivalent quality ~= x265 CRF minus 5.
# Photo: JPEG quality 1-95.
QUALITY_PRESETS = {
    "1": {"name": "High quality",  "desc": "Barely distinguishable from original",
          "hevc_crf": 24, "h264_crf": 19, "photo_q": 88, "savings": "~50-65% smaller"},
    "2": {"name": "Balanced",      "desc": "Recommended sweet spot for archives",
          "hevc_crf": 28, "h264_crf": 23, "photo_q": 80, "savings": "~70-85% smaller"},
    "3": {"name": "Space saver",   "desc": "Noticeable on close inspection",
          "hevc_crf": 31, "h264_crf": 26, "photo_q": 70, "savings": "~85-92% smaller"},
}
DEFAULT_PRESET = "2"

VIDEO_CODEC = "hevc"          # "hevc" (best compression) or "h264" (max compatibility)
VIDEO_PRESET = "medium"       # x264/x265 speed preset; slower = smaller at same quality
AUDIO_BITRATE = "128k"
MAX_VIDEO_HEIGHT = 0          # 0 = keep resolution; e.g. 1080 to downscale 4K
MAX_PHOTO_DIMENSION = 0       # 0 = keep resolution; e.g. 3000 to cap long edge
# ----------------------------------------------------------------------------

VIDEO_EXTS = {".mp4", ".mov", ".3gp", ".m4v", ".avi", ".mkv", ".webm", ".mts"}
PHOTO_EXTS = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".webp", ".bmp", ".tif", ".tiff"}

CODEC_MAP = {
    "hevc": {"encoder": "libx265", "crf_key": "hevc_crf", "extra": ["-tag:v", "hvc1"]},
    "h264": {"encoder": "libx264", "crf_key": "h264_crf", "extra": []},
}


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} B" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def check_tools():
    for tool in ("ffmpeg", "ffprobe"):
        if shutil.which(tool) is None:
            console.print(f"[bold red]ERROR:[/] '{tool}' not found on PATH. "
                          "Install ffmpeg first (macOS: [cyan]brew install ffmpeg[/]).")
            sys.exit(1)


def probe_video(path: Path) -> dict:
    cmd = ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format",
           "-show_streams", "-select_streams", "v:0", str(path)]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, check=True).stdout
        data = json.loads(out)
        stream = data.get("streams", [{}])[0]
        stream["_duration"] = float(data.get("format", {}).get("duration", 0) or 0)
        return stream
    except (subprocess.CalledProcessError, json.JSONDecodeError, ValueError):
        return {}


# ----------------------------------------------------------------------------
# Interactive quality selector
# ----------------------------------------------------------------------------
def pick_quality(args) -> tuple[int, int]:
    """Return (video_crf, photo_quality). Uses CLI flags if given, else asks."""
    codec = CODEC_MAP[args.codec]

    if args.video_quality is not None and args.photo_quality is not None:
        return args.video_quality, args.photo_quality

    table = Table(title="Compression quality", title_style="bold cyan",
                  header_style="bold", border_style="dim")
    table.add_column("#", justify="center", style="bold yellow")
    table.add_column("Preset")
    table.add_column("Video CRF", justify="center")
    table.add_column("Photo q", justify="center")
    table.add_column("Expected size", style="green")
    table.add_column("Notes", style="dim")
    for key, p in QUALITY_PRESETS.items():
        marker = " [dim](default)[/]" if key == DEFAULT_PRESET else ""
        table.add_row(key, p["name"] + marker, str(p[codec["crf_key"]]),
                      str(p["photo_q"]), p["savings"], p["desc"])
    table.add_row("4", "Custom", "-", "-", "-", "Enter your own values")
    console.print(table)

    choice = Prompt.ask("Pick a preset", choices=["1", "2", "3", "4"],
                        default=DEFAULT_PRESET)
    if choice == "4":
        crf_hint = "18-32, lower = better" if args.codec == "hevc" else "16-28, lower = better"
        vq = IntPrompt.ask(f"  Video CRF ({crf_hint})",
                           default=QUALITY_PRESETS[DEFAULT_PRESET][codec["crf_key"]])
        pq = IntPrompt.ask("  Photo JPEG quality (1-95, higher = better)",
                           default=QUALITY_PRESETS[DEFAULT_PRESET]["photo_q"])
    else:
        p = QUALITY_PRESETS[choice]
        vq, pq = p[codec["crf_key"]], p["photo_q"]

    # CLI flags (if one of the two was given) override the menu
    if args.video_quality is not None:
        vq = args.video_quality
    if args.photo_quality is not None:
        pq = args.photo_quality
    return vq, pq


# ----------------------------------------------------------------------------
# Compression workers
# ----------------------------------------------------------------------------
def compress_video(src: Path, dst: Path, args, crf: int, duration: float,
                   progress: Progress, task_id) -> bool:
    """Re-encode with ffmpeg, streaming real progress into the rich bar."""
    codec = CODEC_MAP[args.codec]
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(src),
        # No -noautorotate -> rotation display matrix gets applied on decode,
        # so clips shot with the phone at 90 degrees come out upright.
        "-map", "0",
        "-map_metadata", "0",
        "-movflags", "use_metadata_tags+faststart",
        "-c:v", codec["encoder"],
        "-crf", str(crf),
        "-preset", args.preset,
        "-pix_fmt", "yuv420p",
        *codec["extra"],
        "-c:a", "aac", "-b:a", AUDIO_BITRATE,
        "-progress", "pipe:1", "-nostats",
    ]
    if args.max_video_height:
        cmd += ["-vf",
                f"scale='if(gt(iw,ih),-2,min({args.max_video_height},iw))'"
                f":'if(gt(iw,ih),min({args.max_video_height},ih),-2)'"]
    cmd.append(str(dst))

    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True)
    # ffmpeg emits key=value progress lines on stdout; out_time_us tracks position
    for line in proc.stdout:
        line = line.strip()
        if line.startswith(("out_time_us=", "out_time_ms=")) and duration > 0:
            try:
                done_s = int(line.split("=")[1]) / 1_000_000
                progress.update(task_id, completed=min(done_s / duration, 1.0) * 100)
            except ValueError:
                pass
    proc.wait()
    if proc.returncode != 0:
        err = proc.stderr.read().strip().splitlines()
        progress.console.print(f"  [red]!! ffmpeg failed on {src.name}: "
                               f"{err[-1] if err else 'unknown error'}[/]")
        dst.unlink(missing_ok=True)
        return False
    progress.update(task_id, completed=100)
    return True


def compress_photo(src: Path, dst: Path, args, quality: int) -> bool:
    try:
        with Image.open(src) as im:
            im = ImageOps.exif_transpose(im)   # bake orientation into pixels
            exif = im.getexif()
            if 0x0112 in exif:
                exif[0x0112] = 1               # neutralize Orientation tag
            if args.max_photo_dimension:
                im.thumbnail((args.max_photo_dimension, args.max_photo_dimension),
                             Image.LANCZOS)
            if im.mode in ("RGBA", "P", "LA"):
                im = im.convert("RGB")
            im.save(dst, "JPEG", quality=quality, optimize=True,
                    progressive=True, exif=exif.tobytes())
        return True
    except Exception as e:
        console.print(f"  [red]!! photo failed on {src.name}: {e}[/]")
        dst.unlink(missing_ok=True)
        return False


# ----------------------------------------------------------------------------
def main():
    p = argparse.ArgumentParser(description="Compress a photo/video library into a sibling folder.")
    p.add_argument("input", help="Path to the folder to compress")
    p.add_argument("--video-quality", type=int, default=None,
                   help="Video CRF (skips the interactive menu if both flags given)")
    p.add_argument("--photo-quality", type=int, default=None,
                   help="JPEG quality 1-95")
    p.add_argument("--codec", choices=("hevc", "h264"), default=VIDEO_CODEC,
                   help=f"Video codec (default {VIDEO_CODEC}: best compression; "
                        "h264: plays everywhere)")
    p.add_argument("--preset", default=VIDEO_PRESET,
                   help=f"x264/x265 speed preset (default {VIDEO_PRESET}; 'slow' = smaller files)")
    p.add_argument("--max-video-height", type=int, default=MAX_VIDEO_HEIGHT,
                   help="Downscale videos taller than this (0 = keep resolution)")
    p.add_argument("--max-photo-dimension", type=int, default=MAX_PHOTO_DIMENSION,
                   help="Cap photo long edge in pixels (0 = keep resolution)")
    p.add_argument("--skip-existing", action="store_true",
                   help="Skip files already present in the output folder (resume)")
    p.add_argument("--dry-run", action="store_true", help="List what would be done")
    args = p.parse_args()

    check_tools()

    src_root = Path(args.input).expanduser().resolve()
    if not src_root.is_dir():
        console.print(f"[bold red]ERROR:[/] not a folder: {src_root}")
        sys.exit(1)

    dst_root = src_root.parent / f"{src_root.name}_compressed"

    files = sorted(f for f in src_root.rglob("*") if f.is_file())
    n_vid = sum(1 for f in files if f.suffix.lower() in VIDEO_EXTS)
    n_pho = sum(1 for f in files if f.suffix.lower() in PHOTO_EXTS)
    n_oth = len(files) - n_vid - n_pho

    console.print(Panel.fit(
        f"[bold]Input[/]   {src_root}\n"
        f"[bold]Output[/]  {dst_root}\n"
        f"[bold]Found[/]   [cyan]{n_vid}[/] videos, [cyan]{n_pho}[/] photos, "
        f"[dim]{n_oth} other files (copied as-is)[/]",
        title="[bold]Media compressor[/]", border_style="cyan"))

    if not HEIC_OK and any(f.suffix.lower() in (".heic", ".heif") for f in files):
        console.print("[yellow]Note:[/] pillow-heif not installed — .heic files will be "
                      "copied uncompressed. [dim]python3 -m pip install pillow-heif[/]\n")

    video_crf, photo_q = pick_quality(args)
    console.print(f"\nUsing [bold]{args.codec}[/] CRF [bold]{video_crf}[/] "
                  f"(preset {args.preset}), photos JPEG q[bold]{photo_q}[/]\n")

    if args.dry_run:
        for f in files:
            ext = f.suffix.lower()
            kind = "video" if ext in VIDEO_EXTS else "photo" if ext in PHOTO_EXTS else "copy "
            console.print(f"[dim][{kind}][/] {f.relative_to(src_root)}  ({human(f.stat().st_size)})")
        return

    dst_root.mkdir(exist_ok=True)
    total_in = total_out = done = failed = skipped = 0

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(bar_width=30),
        TaskProgressColumn(),
        TimeElapsedColumn(),
        TimeRemainingColumn(),
        console=console,
    ) as progress:
        overall = progress.add_task(f"[bold cyan]Overall[/] ({len(files)} files)",
                                    total=len(files))
        current = progress.add_task("", total=100, visible=False)

        for f in files:
            rel = f.relative_to(src_root)
            ext = f.suffix.lower()
            is_video = ext in VIDEO_EXTS
            is_photo = ext in PHOTO_EXTS

            out = (dst_root / rel).with_suffix(".jpg") if is_photo else dst_root / rel

            if args.skip_existing and out.exists():
                skipped += 1
                progress.advance(overall)
                continue

            out.parent.mkdir(parents=True, exist_ok=True)
            size_in = f.stat().st_size
            label = str(rel) if len(str(rel)) <= 45 else "…" + str(rel)[-44:]

            ok = None
            if is_video:
                info = probe_video(f)
                progress.update(current, description=f"[magenta]🎬 {label}[/]",
                                completed=0, visible=True)
                ok = compress_video(f, out, args, video_crf,
                                    info.get("_duration", 0), progress, current)
            elif is_photo:
                if ext in (".heic", ".heif") and not HEIC_OK:
                    out = dst_root / rel
                    shutil.copy2(f, out)
                    total_in += size_in; total_out += out.stat().st_size; done += 1
                    progress.advance(overall)
                    continue
                progress.update(current, description=f"[green]🖼  {label}[/]",
                                completed=50, visible=True)
                ok = compress_photo(f, out, args, photo_q)
            else:
                shutil.copy2(f, out)
                total_in += size_in; total_out += out.stat().st_size; done += 1
                progress.advance(overall)
                continue

            if ok:
                shutil.copystat(f, out)        # keep original timestamps
                size_out = out.stat().st_size
                total_in += size_in; total_out += size_out; done += 1
                pct = 100 - size_out * 100 // size_in if size_in else 0
                progress.console.print(
                    f"  [dim]{rel}[/]  {human(size_in)} → [green]{human(size_out)}[/] "
                    f"[bold green](-{pct}%)[/]")
            else:
                failed += 1
            progress.advance(overall)

        progress.update(current, visible=False)

    # Summary
    saved_pct = 100 - total_out * 100 // total_in if total_in else 0
    style = "green" if failed == 0 else "yellow"
    console.print(Panel.fit(
        f"[bold]{done}[/] processed, [red]{failed}[/] failed, [dim]{skipped} skipped[/]\n"
        f"[bold]{human(total_in)}[/] → [bold green]{human(total_out)}[/]   "
        f"[bold green]{saved_pct}% saved[/]\n"
        f"[dim]{dst_root}[/]",
        title="[bold]Done[/]", border_style=style))


if __name__ == "__main__":
    main()