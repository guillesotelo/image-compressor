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

Crash / interrupt safety
  - Every output is written to a hidden temp file first and only renamed
    into place after the encoder exits cleanly. A run killed mid-video
    therefore never leaves a half-finished file that looks "done", so
    --skip can be trusted when resuming.

Logging
  - Each run writes to <output>/_compress_logs/:
      run_<stamp>.log         human-readable log of everything
      run_<stamp>_files.csv   one row per file (status, sizes, error, ...)
      run_<stamp>_failed.txt  source paths of failures only
  - Feed that failed list straight back in with --retry-from to redo them.

Requires: ffmpeg + ffprobe on PATH, and:
    python3 -m pip install pillow rich
Optional: python3 -m pip install pillow-heif   (for .heic input)

Usage:
  python3 compress_media.py /path/to/DCIM             # interactive quality menu
  python3 compress_media.py /path/to/DCIM -s          # resume, skip done files
  python3 compress_media.py /path/to/DCIM --video-quality 26 --photo-quality 85
  python3 compress_media.py /path/to/DCIM --codec h264 --dry-run
  python3 compress_media.py /path/to/DCIM \
      --retry-from /path/to/DCIM_compressed/_compress_logs/run_..._failed.txt

Output: a sibling folder next to the input, e.g.
  /Documents/DCIM  ->  /Documents/DCIM_compressed
"""

import argparse
import csv
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime
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
LOG_DIRNAME = "_compress_logs"
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
# Atomic-write helpers
# ----------------------------------------------------------------------------
def temp_target(out: Path) -> Path:
    """Hidden sibling temp path that keeps the real extension (ffmpeg needs it
    to pick a muxer): foo/VID.mp4 -> foo/.VID.part.mp4"""
    return out.with_name(f".{out.stem}.part{out.suffix}")


def sweep_partials(root: Path) -> int:
    """Delete leftovers from a previous interrupted run."""
    n = 0
    if not root.exists():
        return 0
    for p in root.rglob(".*.part.*"):
        if p.is_file():
            p.unlink(missing_ok=True)
            n += 1
    return n


# ----------------------------------------------------------------------------
# Run log (text + CSV + failed-list), flushed after every file so a Ctrl-C
# or a crash still leaves a complete record on disk.
# ----------------------------------------------------------------------------
class RunLog:
    CSV_HEADER = ["timestamp", "status", "kind", "relative_path", "source_path",
                  "output_path", "bytes_in", "bytes_out", "saved_pct",
                  "seconds", "error"]

    def __init__(self, log_dir: Path, stamp: str):
        log_dir.mkdir(parents=True, exist_ok=True)
        self.log_path = log_dir / f"run_{stamp}.log"
        self.csv_path = log_dir / f"run_{stamp}_files.csv"
        self.failed_path = log_dir / f"run_{stamp}_failed.txt"
        self.failures: list[tuple[str, str]] = []   # (source path, error)
        self._log = self.log_path.open("w", encoding="utf-8")
        self._csv_fh = self.csv_path.open("w", newline="", encoding="utf-8")
        self._csv = csv.writer(self._csv_fh)
        self._csv.writerow(self.CSV_HEADER)
        self._csv_fh.flush()

    def line(self, msg: str, level: str = "INFO"):
        self._log.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} [{level}] {msg}\n")
        self._log.flush()

    def block(self, text: str):
        self._log.write(text.rstrip() + "\n")
        self._log.flush()

    def file(self, status: str, kind: str, rel: str, src: Path, out: Path | None,
             bytes_in: int, bytes_out: int, seconds: float, error: str = ""):
        saved = (100 - bytes_out * 100 // bytes_in) if (bytes_in and bytes_out) else ""
        self._csv.writerow([f"{datetime.now():%Y-%m-%d %H:%M:%S}", status, kind, rel,
                            str(src), str(out) if out else "", bytes_in or "",
                            bytes_out or "", saved, f"{seconds:.1f}", error])
        self._csv_fh.flush()

        detail = f"{human(bytes_in)} -> {human(bytes_out)} ({saved}% saved)" \
            if (bytes_in and bytes_out) else human(bytes_in) if bytes_in else ""
        msg = f"{status.upper():9s} {kind:5s} {rel}" + (f"  {detail}" if detail else "")
        if error:
            msg += f"  | ERROR: {error}"
        self.line(msg, "ERROR" if status == "failed" else "INFO")

        if status == "failed":
            self.failures.append((str(src), error))
            with self.failed_path.open("a", encoding="utf-8") as fh:
                fh.write(f"{src}\n")

    def close(self):
        self._log.close()
        self._csv_fh.close()


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
# Compression workers   (each returns (ok, error_message))
# ----------------------------------------------------------------------------
def compress_video(src: Path, dst: Path, args, crf: int, duration: float,
                   progress: Progress, task_id) -> tuple[bool, str]:
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

    # stderr goes to a temp file rather than a pipe: ffmpeg can't deadlock
    # filling a pipe nobody is draining while we read progress from stdout.
    with tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace") as errf:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=errf, text=True)
        try:
            # ffmpeg emits key=value progress lines on stdout; out_time_us tracks position
            for line in proc.stdout:
                line = line.strip()
                if line.startswith(("out_time_us=", "out_time_ms=")) and duration > 0:
                    try:
                        done_s = int(line.split("=")[1]) / 1_000_000
                        progress.update(task_id,
                                        completed=min(done_s / duration, 1.0) * 100)
                    except ValueError:
                        pass
            proc.wait()
        except BaseException:                  # includes KeyboardInterrupt
            proc.kill()
            proc.wait()
            dst.unlink(missing_ok=True)
            raise
        errf.seek(0)
        err_text = errf.read().strip()

    if proc.returncode != 0:
        lines = err_text.splitlines()
        msg = lines[-1] if lines else f"ffmpeg exited with code {proc.returncode}"
        progress.console.print(f"  [red]!! ffmpeg failed on {src.name}: {msg}[/]")
        dst.unlink(missing_ok=True)
        return False, msg
    progress.update(task_id, completed=100)
    return True, ""


def compress_photo(src: Path, dst: Path, args, quality: int) -> tuple[bool, str]:
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
        return True, ""
    except KeyboardInterrupt:
        dst.unlink(missing_ok=True)
        raise
    except Exception as e:
        console.print(f"  [red]!! photo failed on {src.name}: {e}[/]")
        dst.unlink(missing_ok=True)
        return False, f"{type(e).__name__}: {e}"


def load_retry_list(path: Path, src_root: Path) -> set[Path]:
    wanted = set()
    for raw in Path(path).read_text(encoding="utf-8").splitlines():
        raw = raw.strip()
        if not raw or raw.startswith("#"):
            continue
        p = Path(raw)
        wanted.add((p if p.is_absolute() else src_root / p).resolve())
    return wanted


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
    p.add_argument("-s", "--skip", "--skip-existing", dest="skip_existing",
                   action="store_true",
                   help="Resume: skip files already present in the output folder")
    p.add_argument("--retry-from", metavar="FILE", default=None,
                   help="Only process the source paths listed in FILE "
                        "(e.g. a run_..._failed.txt from a previous run)")
    p.add_argument("--log-dir", default=None,
                   help=f"Where to write logs (default: <output>/{LOG_DIRNAME})")
    p.add_argument("--no-log", action="store_true", help="Disable log files")
    p.add_argument("--dry-run", action="store_true", help="List what would be done")
    args = p.parse_args()

    check_tools()

    src_root = Path(args.input).expanduser().resolve()
    if not src_root.is_dir():
        console.print(f"[bold red]ERROR:[/] not a folder: {src_root}")
        sys.exit(1)

    dst_root = src_root.parent / f"{src_root.name}_compressed"

    files = sorted(f for f in src_root.rglob("*") if f.is_file())
    if args.retry_from:
        wanted = load_retry_list(Path(args.retry_from).expanduser(), src_root)
        files = [f for f in files if f.resolve() in wanted]
        console.print(f"[cyan]Retry mode:[/] {len(files)} of {len(wanted)} listed "
                      f"path(s) found under {src_root}")

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
            out = (dst_root / f.relative_to(src_root))
            if ext in PHOTO_EXTS and not (ext in (".heic", ".heif") and not HEIC_OK):
                out = out.with_suffix(".jpg")
            mark = " [yellow](would skip, exists)[/]" \
                if args.skip_existing and out.exists() and out.stat().st_size > 0 else ""
            console.print(f"[dim][{kind}][/] {f.relative_to(src_root)}  "
                          f"({human(f.stat().st_size)}){mark}")
        return

    dst_root.mkdir(exist_ok=True)

    # Clear leftovers from a run that was killed mid-encode.
    stale = sweep_partials(dst_root)
    if stale:
        console.print(f"[dim]Removed {stale} unfinished file(s) from a previous run.[/]")

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log = None
    if not args.no_log:
        log_dir = Path(args.log_dir).expanduser() if args.log_dir else dst_root / LOG_DIRNAME
        log = RunLog(log_dir, stamp)
        log.block(
            f"# compress_media run {stamp}\n"
            f"# input      : {src_root}\n"
            f"# output     : {dst_root}\n"
            f"# codec      : {args.codec} (encoder {CODEC_MAP[args.codec]['encoder']}), "
            f"CRF {video_crf}, preset {args.preset}\n"
            f"# photo qual : {photo_q}"
            + (f", max long edge {args.max_photo_dimension}px" if args.max_photo_dimension else "")
            + "\n"
            f"# max height : {args.max_video_height or 'original'}\n"
            f"# skip mode  : {args.skip_existing}\n"
            f"# retry list : {args.retry_from or '-'}\n"
            f"# queued     : {len(files)} files ({n_vid} video, {n_pho} photo, {n_oth} other)\n"
            f"# stale parts removed: {stale}\n")
        console.print(f"[dim]Logging to {log.log_path}[/]\n")

    total_in = total_out = done = failed = skipped = copied = 0
    run_start = time.time()
    interrupted = False

    try:
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
                heic_passthrough = ext in (".heic", ".heif") and not HEIC_OK
                kind = "video" if is_video else "photo" if is_photo else "other"

                out = (dst_root / rel).with_suffix(".jpg") \
                    if (is_photo and not heic_passthrough) else dst_root / rel

                # Resume: a file only exists here if a previous run finished it,
                # because unfinished work lives in .*.part.* temp files.
                if args.skip_existing and out.exists() and out.stat().st_size > 0:
                    skipped += 1
                    if log:
                        log.file("skipped", kind, str(rel), f, out,
                                 f.stat().st_size, out.stat().st_size, 0.0)
                    progress.advance(overall)
                    continue

                out.parent.mkdir(parents=True, exist_ok=True)
                size_in = f.stat().st_size
                label = str(rel) if len(str(rel)) <= 45 else "…" + str(rel)[-44:]
                tmp = temp_target(out)
                t0 = time.time()

                if is_video:
                    info = probe_video(f)
                    progress.update(current, description=f"[magenta]🎬 {label}[/]",
                                    completed=0, visible=True)
                    ok, err = compress_video(f, tmp, args, video_crf,
                                             info.get("_duration", 0), progress, current)
                elif is_photo and not heic_passthrough:
                    progress.update(current, description=f"[green]🖼  {label}[/]",
                                    completed=50, visible=True)
                    ok, err = compress_photo(f, tmp, args, photo_q)
                else:
                    # HEIC without pillow-heif, and every non-media file: copy as-is
                    progress.update(current, description=f"[blue]📄 {label}[/]",
                                    completed=50, visible=True)
                    try:
                        shutil.copy2(f, tmp)
                        ok, err = True, ""
                    except Exception as e:
                        tmp.unlink(missing_ok=True)
                        ok, err = False, f"copy failed: {type(e).__name__}: {e}"

                elapsed = time.time() - t0

                if ok:
                    shutil.copystat(f, tmp)    # keep original timestamps
                    os.replace(tmp, out)       # atomic: now it counts as "done"
                    size_out = out.stat().st_size
                    total_in += size_in
                    total_out += size_out
                    pct = 100 - size_out * 100 // size_in if size_in else 0
                    if kind == "other" or heic_passthrough:
                        copied += 1
                        status = "copied"
                    else:
                        done += 1
                        status = "ok"
                        progress.console.print(
                            f"  [dim]{rel}[/]  {human(size_in)} → [green]{human(size_out)}[/] "
                            f"[bold green](-{pct}%)[/]")
                    if log:
                        log.file(status, kind, str(rel), f, out, size_in, size_out, elapsed)
                else:
                    failed += 1
                    if log:
                        log.file("failed", kind, str(rel), f, None, size_in, 0, elapsed, err)
                progress.advance(overall)

            progress.update(current, visible=False)

    except KeyboardInterrupt:
        interrupted = True
        console.print("\n[bold yellow]Interrupted.[/] Partial output removed; "
                      "finished files are intact.")
        if log:
            log.line("Run interrupted by user (Ctrl-C).", "WARN")
        sweep_partials(dst_root)

    # ------------------------------------------------------------------ Summary
    elapsed_total = time.time() - run_start
    saved_pct = 100 - total_out * 100 // total_in if total_in else 0
    style = "yellow" if (failed or interrupted) else "green"

    console.print(Panel.fit(
        f"[bold]{done}[/] compressed, [dim]{copied} copied[/], [red]{failed}[/] failed, "
        f"[dim]{skipped} skipped[/]\n"
        f"[bold]{human(total_in)}[/] → [bold green]{human(total_out)}[/]   "
        f"[bold green]{saved_pct}% saved[/]   [dim]in {elapsed_total/60:.1f} min[/]\n"
        f"[dim]{dst_root}[/]",
        title="[bold]Interrupted[/]" if interrupted else "[bold]Done[/]",
        border_style=style))

    if log:
        log.block(
            "\n" + "=" * 70 + "\n"
            f"SUMMARY ({'INTERRUPTED' if interrupted else 'COMPLETE'})\n"
            + "=" * 70 + "\n"
            f"compressed : {done}\n"
            f"copied     : {copied}\n"
            f"failed     : {failed}\n"
            f"skipped    : {skipped}\n"
            f"bytes in   : {total_in} ({human(total_in)})\n"
            f"bytes out  : {total_out} ({human(total_out)})\n"
            f"saved      : {saved_pct}%\n"
            f"elapsed    : {elapsed_total/60:.1f} min\n")
        if log.failures:
            log.block("FAILED FILES (source path | reason)\n" + "-" * 70)
            for src, err in log.failures:
                log.block(f"{src} | {err}")
            log.block(
                "\nRe-run just these with:\n"
                f"  python3 {Path(sys.argv[0]).name} \"{src_root}\" "
                f"--retry-from \"{log.failed_path}\" "
                f"--video-quality {video_crf} --photo-quality {photo_q} "
                f"--codec {args.codec}")
        else:
            log.block("No failures.")
        log.close()

        console.print(f"[dim]Log:  {log.log_path}\n"
                      f"CSV:  {log.csv_path}[/]")
        if failed:
            console.print(f"[yellow]Failed list:[/] {log.failed_path}\n"
                          f"[dim]Retry with:[/] python3 {Path(sys.argv[0]).name} "
                          f"\"{src_root}\" --retry-from \"{log.failed_path}\" "
                          f"--video-quality {video_crf} --photo-quality {photo_q} "
                          f"--codec {args.codec}")
        if interrupted or args.skip_existing or failed:
            console.print(f"[dim]Resume later with:[/] python3 "
                          f"{Path(sys.argv[0]).name} \"{src_root}\" -s")


if __name__ == "__main__":
    main()