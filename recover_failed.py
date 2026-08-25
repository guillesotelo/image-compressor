#!/usr/bin/env python3
"""
recover_failed.py — second pass over the files compress_media.py couldn't process.

Reads a run_..._failed.txt (or the _files.csv) from a previous run and tries
progressively more forgiving strategies on each file, writing results into the
same compressed output tree so the copy ends up complete.

Strategy ladder
  PHOTOS
    1. Pillow with LOAD_TRUNCATED_IMAGES — recovers JPEGs whose tail is missing
       ("broken data stream"). Decodes what exists, greys out the lost rows.
    2. Sniff real format from magic bytes — a .jpg that is actually PNG/HEIC/MP4
       gets routed to the right decoder instead of failing.
    3. ffmpeg as decoder — sometimes reads headers Pillow rejects.
    4. Verbatim copy of the original, so the file is never simply absent.
  VIDEOS
    1. Re-encode, but map only real video+audio streams (GoPro/phone clips carry
       gpmd/tmcd data streams that make "-map 0" fail), forcing an MP4 container,
       with error tolerance and a full-file probe.
    2. Same with H.264, which decodes some broken streams x265 chokes on.
    3. Stream-copy remux into a fresh MP4 — no quality loss, no re-encode.
    4. Verbatim copy.

Every file is tagged with the method that worked, so you can review the ones
that came through damaged or uncompressed.

Usage
  python3 recover_failed.py run_..._failed.txt --diagnose          # report only
  python3 recover_failed.py run_..._failed.txt --video-quality 26 --photo-quality 65
  python3 recover_failed.py run_..._files.csv  --photo-quality 65  # reads status=failed rows

Source and output roots are inferred from the listed paths; override with
--source / --output if the folders have moved.
"""

from __future__ import annotations

import argparse
import csv
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageFile, ImageOps

# The whole point of this script: don't abort on a short file.
ImageFile.LOAD_TRUNCATED_IMAGES = True

try:
    from rich.console import Console
    from rich.progress import (BarColumn, Progress, SpinnerColumn, TaskProgressColumn,
                               TextColumn, TimeElapsedColumn, TimeRemainingColumn)
    from rich.table import Table
except ImportError:
    sys.exit("Needs rich:  python3 -m pip install rich")

try:
    from pillow_heif import register_heif_opener
    register_heif_opener()
    HEIC_OK = True
except ImportError:
    HEIC_OK = False

console = Console()

VIDEO_EXTS = {".mp4", ".mov", ".3gp", ".m4v", ".avi", ".mkv", ".webm", ".mts"}
PHOTO_EXTS = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".webp", ".bmp", ".tif", ".tiff"}
ENCODER = {"hevc": "libx265", "h264": "libx264"}


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} B" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


# ---------------------------------------------------------------- diagnosis
def sniff(path: Path) -> str:
    """Real file type from magic bytes, ignoring the extension."""
    try:
        with path.open("rb") as fh:
            head = fh.read(16)
    except OSError:
        return "unreadable"
    if not head:
        return "empty"
    if head[:3] == b"\xff\xd8\xff":
        return "jpeg"
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if head[:4] == b"GIF8":
        return "gif"
    if head[:2] == b"BM":
        return "bmp"
    if head[:4] in (b"II*\x00", b"MM\x00*"):
        return "tiff"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    if head[4:8] == b"ftyp":
        brand = head[8:12]
        if brand in (b"heic", b"heix", b"hevc", b"mif1", b"heim"):
            return "heic"
        return "mp4"          # incl. 3gp / mov / qt brands
    if head[:4] == b"\x1aE\xdf\xa3":
        return "matroska"
    if head[:4] == b"RIFF":
        return "avi"
    return "unknown"


def jpeg_complete(path: Path) -> bool:
    """A whole JPEG ends with the EOI marker. Missing = truncated tail."""
    try:
        with path.open("rb") as fh:
            fh.seek(-2, os.SEEK_END)
            return fh.read(2) == b"\xff\xd9"
    except OSError:
        return False


def ffprobe_ok(path: Path) -> tuple[bool, str]:
    cmd = ["ffprobe", "-v", "error", "-show_entries",
           "stream=codec_type,codec_name", "-of", "csv=p=0", str(path)]
    r = subprocess.run(cmd, capture_output=True, text=True)
    return r.returncode == 0 and bool(r.stdout.strip()), r.stdout.strip().replace("\n", "; ")


# ---------------------------------------------------------------- photo path
def save_jpeg(im: Image.Image, dst: Path, quality: int, max_dim: int) -> None:
    im = ImageOps.exif_transpose(im)
    exif = im.getexif()
    if 0x0112 in exif:
        exif[0x0112] = 1
    if max_dim:
        im.thumbnail((max_dim, max_dim), Image.LANCZOS)
    if im.mode in ("RGBA", "P", "LA"):
        im = im.convert("RGB")
    im.save(dst, "JPEG", quality=quality, optimize=True, progressive=True,
            exif=exif.tobytes())


def try_photo(src: Path, tmp: Path, quality: int, max_dim: int) -> tuple[str, str]:
    """Return (method, error). method == '' means every attempt failed."""
    # 1. Pillow, now tolerating truncation
    try:
        with Image.open(src) as im:
            im.load()
            save_jpeg(im, tmp, quality, max_dim)
        truncated = sniff(src) == "jpeg" and not jpeg_complete(src)
        return ("pillow-truncated" if truncated else "pillow"), ""
    except Exception as e:
        first = f"{type(e).__name__}: {e}"
        tmp.unlink(missing_ok=True)

    # 2. ffmpeg as a decoder of last resort (keeps what metadata it can)
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
           "-err_detect", "ignore_err", "-i", str(src),
           "-map_metadata", "0", "-frames:v", "1",
           "-q:v", str(max(2, round((100 - quality) / 8))), "-f", "mjpeg", str(tmp)]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode == 0 and tmp.exists() and tmp.stat().st_size > 0:
        return "ffmpeg-decode", ""
    tmp.unlink(missing_ok=True)
    return "", first


# ---------------------------------------------------------------- video path
def run_ffmpeg(cmd: list[str], tmp: Path) -> tuple[bool, str]:
    with tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace") as errf:
        rc = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=errf).returncode
        errf.seek(0)
        err = errf.read().strip().splitlines()
    if rc == 0 and tmp.exists() and tmp.stat().st_size > 1024:
        return True, ""
    tmp.unlink(missing_ok=True)
    return False, (err[-1] if err else f"exit {rc}")


def try_video(src: Path, tmp: Path, crf: int, codec: str, preset: str) -> tuple[str, str]:
    base = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-err_detect", "ignore_err", "-fflags", "+genpts+discardcorrupt",
            "-analyzeduration", "200M", "-probesize", "200M", "-i", str(src)]
    # only real a/v streams: data streams (GoPro gpmd, tmcd) are what killed "-map 0"
    maps = ["-map", "0:v:0", "-map", "0:a:0?", "-map_metadata", "0",
            "-movflags", "use_metadata_tags+faststart", "-f", "mp4"]
    first = ""

    for name, enc, extra in (("reencode-" + codec, ENCODER[codec],
                              ["-tag:v", "hvc1"] if codec == "hevc" else []),
                             ("reencode-h264", "libx264", [])):
        cmd = base + maps + ["-c:v", enc, "-crf", str(crf), "-preset", preset,
                             "-pix_fmt", "yuv420p", *extra,
                             "-c:a", "aac", "-b:a", "128k", str(tmp)]
        ok, err = run_ffmpeg(cmd, tmp)
        if ok:
            return name, ""
        first = first or err
        if enc == ENCODER[codec] and codec == "h264":
            break            # don't run the same encoder twice

    # remux without touching the streams — rescues files that can't be decoded
    ok, err = run_ffmpeg(base + maps + ["-c", "copy", str(tmp)], tmp)
    if ok:
        return "remux-copy", ""
    return "", first or err


# ---------------------------------------------------------------- input list
def read_failed(list_path: Path) -> list[Path]:
    if list_path.suffix.lower() == ".csv":
        with list_path.open(encoding="utf-8") as fh:
            return [Path(r["source_path"]) for r in csv.DictReader(fh)
                    if r.get("status") == "failed" and r.get("source_path")]
    return [Path(l.strip()) for l in list_path.read_text(encoding="utf-8").splitlines()
            if l.strip() and not l.startswith("#")]


def infer_roots(paths: list[Path]) -> tuple[Path, Path]:
    common = Path(os.path.commonpath([str(p.parent) for p in paths]))
    # walk up until a parent looks like the root the compressed sibling was made from
    for cand in [common, *common.parents]:
        sib = cand.parent / f"{cand.name}_compressed"
        if sib.is_dir():
            return cand, sib
    return common, common.parent / f"{common.name}_compressed"


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description="Recover files a compression run failed on.")
    ap.add_argument("failed_list", help="run_..._failed.txt or run_..._files.csv")
    ap.add_argument("--source", default=None, help="Source root (inferred if omitted)")
    ap.add_argument("--output", default=None, help="Compressed root (inferred if omitted)")
    ap.add_argument("--photo-quality", type=int, default=65)
    ap.add_argument("--video-quality", type=int, default=26)
    ap.add_argument("--codec", choices=("hevc", "h264"), default="hevc")
    ap.add_argument("--preset", default="medium")
    ap.add_argument("--max-photo-dimension", type=int, default=0)
    ap.add_argument("--diagnose", action="store_true",
                    help="Only inspect and report — write nothing")
    ap.add_argument("--no-copy-fallback", action="store_true",
                    help="Leave unrecoverable files out instead of copying the original")
    args = ap.parse_args()

    for tool in ("ffmpeg", "ffprobe"):
        if shutil.which(tool) is None:
            sys.exit(f"'{tool}' not found on PATH")

    paths = read_failed(Path(args.failed_list).expanduser())
    if not paths:
        sys.exit("No failed entries found in that file.")
    missing = [p for p in paths if not p.exists()]
    paths = [p for p in paths if p.exists()]
    if missing:
        console.print(f"[yellow]{len(missing)} listed path(s) no longer exist — skipping.[/]")

    src_root, dst_root = infer_roots(paths)
    if args.source:
        src_root = Path(args.source).expanduser().resolve()
    if args.output:
        dst_root = Path(args.output).expanduser().resolve()
    console.print(f"[bold]Source[/] {src_root}\n[bold]Output[/] {dst_root}\n"
                  f"[bold]Files[/]  {len(paths)}\n")

    # ---------------------------------------------------------- diagnose mode
    if args.diagnose:
        rows = []
        with Progress(SpinnerColumn(), TextColumn("Inspecting"), BarColumn(),
                      TaskProgressColumn(), console=console) as pr:
            t = pr.add_task("", total=len(paths))
            for p in paths:
                kind = sniff(p)
                note = ""
                if kind == "jpeg":
                    note = "complete" if jpeg_complete(p) else "truncated tail"
                elif kind in ("mp4", "matroska", "avi"):
                    ok, streams = ffprobe_ok(p)
                    note = streams if ok else "ffprobe cannot read it"
                rows.append((p, kind, note))
                pr.advance(t)

        summary = {}
        for _, kind, note in rows:
            key = f"{kind} / {note}" if note else kind
            summary[key] = summary.get(key, 0) + 1
        tbl = Table(title="What these files actually are", header_style="bold")
        tbl.add_column("Detected"); tbl.add_column("Count", justify="right")
        for k, v in sorted(summary.items(), key=lambda x: -x[1]):
            tbl.add_row(k, str(v))
        console.print(tbl)
        odd = [r for r in rows if r[1] in ("empty", "unreadable", "unknown")
               or (r[1] != "jpeg" and r[0].suffix.lower() in (".jpg", ".jpeg"))]
        if odd:
            console.print("\n[bold]Files whose extension lies (or that are empty):[/]")
            for p, kind, _ in odd[:40]:
                console.print(f"  [dim]{kind:10s}[/] {p}")
            if len(odd) > 40:
                console.print(f"  [dim]... and {len(odd)-40} more[/]")
        return

    # ---------------------------------------------------------- recovery run
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_dir = dst_root / "_compress_logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    csv_path = log_dir / f"recovery_{stamp}.csv"
    lost_path = log_dir / f"recovery_{stamp}_unrecoverable.txt"
    fh = csv_path.open("w", newline="", encoding="utf-8")
    w = csv.writer(fh)
    w.writerow(["timestamp", "method", "detected", "integrity", "relative_path",
                "source_path", "output_path", "bytes_in", "bytes_out", "error"])

    counts: dict[str, int] = {}
    lost: list[tuple[Path, str]] = []
    total_in = total_out = 0

    with Progress(SpinnerColumn(), TextColumn("[progress.description]{task.description}"),
                  BarColumn(bar_width=30), TaskProgressColumn(), TimeElapsedColumn(),
                  TimeRemainingColumn(), console=console) as pr:
        task = pr.add_task(f"[bold cyan]Recovering[/] ({len(paths)} files)", total=len(paths))
        for src in paths:
            try:
                rel = src.relative_to(src_root)
            except ValueError:
                rel = Path(src.name)
            detected = sniff(src)
            integrity = ("complete" if jpeg_complete(src) else "truncated") \
                if detected == "jpeg" else ""
            size_in = src.stat().st_size
            ext = src.suffix.lower()
            treat_as_video = detected in ("mp4", "matroska", "avi") or (
                ext in VIDEO_EXTS and detected not in
                ("jpeg", "png", "heic", "webp", "bmp", "tiff", "gif"))

            if detected in ("empty", "unreadable"):
                method, err = "", f"file is {detected}"
                out = tmp = None
            elif treat_as_video:
                out = (dst_root / rel).with_suffix(".mp4")
                out.parent.mkdir(parents=True, exist_ok=True)
                tmp = out.with_name(f".{out.stem}.part{out.suffix}")
                method, err = try_video(src, tmp, args.video_quality,
                                        args.codec, args.preset)
            else:
                if detected == "heic" and not HEIC_OK:
                    method, err, out, tmp = "", "heic without pillow-heif", None, None
                else:
                    out = (dst_root / rel).with_suffix(".jpg")
                    out.parent.mkdir(parents=True, exist_ok=True)
                    tmp = out.with_name(f".{out.stem}.part{out.suffix}")
                    method, err = try_photo(src, tmp, args.photo_quality,
                                            args.max_photo_dimension)

            if not method and not args.no_copy_fallback and detected != "empty":
                out = dst_root / rel                      # keep original name+ext
                out.parent.mkdir(parents=True, exist_ok=True)
                tmp = out.with_name(f".{out.stem}.part{out.suffix}")
                try:
                    shutil.copy2(src, tmp)
                    method = "copied-original"
                except Exception as e:
                    tmp.unlink(missing_ok=True)
                    err = f"{err} | copy failed: {e}"

            if method:
                shutil.copystat(src, tmp)
                os.replace(tmp, out)
                size_out = out.stat().st_size
                total_in += size_in
                total_out += size_out
                counts[method] = counts.get(method, 0) + 1
                w.writerow([f"{datetime.now():%Y-%m-%d %H:%M:%S}", method, detected,
                            integrity, str(rel), str(src), str(out), size_in, size_out, ""])
                tag = "yellow" if method.startswith(("copied", "pillow-truncated")) else "green"
                pr.console.print(f"  [{tag}]{method:18s}[/] [dim]{rel}[/]  "
                                 f"{human(size_in)} → {human(size_out)}")
            else:
                counts["unrecoverable"] = counts.get("unrecoverable", 0) + 1
                lost.append((src, err))
                w.writerow([f"{datetime.now():%Y-%m-%d %H:%M:%S}", "unrecoverable", detected,
                            integrity, str(rel), str(src), "", size_in, "", err])
                pr.console.print(f"  [red]{'unrecoverable':18s}[/] [dim]{rel}[/]  {err}")
            fh.flush()
            pr.advance(task)

    fh.close()
    if lost:
        lost_path.write_text("\n".join(str(p) for p, _ in lost) + "\n", encoding="utf-8")

    tbl = Table(title="Recovery results", header_style="bold")
    tbl.add_column("Method"); tbl.add_column("Files", justify="right")
    order = ["pillow", "pillow-truncated", "ffmpeg-decode", "reencode-hevc",
             "reencode-h264", "remux-copy", "copied-original", "unrecoverable"]
    for k in order + [k for k in counts if k not in order]:
        if counts.get(k):
            tbl.add_row(k, str(counts[k]))
    console.print(tbl)
    saved = 100 - total_out * 100 // total_in if total_in else 0
    console.print(f"{human(total_in)} → {human(total_out)}  ({saved}% saved)\n"
                  f"[dim]{csv_path}[/]")
    if lost:
        console.print(f"[red]{len(lost)} unrecoverable[/] → [dim]{lost_path}[/]")
    console.print("\n[dim]Review anything tagged pillow-truncated (image tail is grey) "
                  "or copied-original (stored uncompressed).[/]")


if __name__ == "__main__":
    main()