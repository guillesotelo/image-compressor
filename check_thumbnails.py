#!/usr/bin/env python3
"""
salvage_thumbnails.py — pull the embedded preview out of photos whose main
image data is destroyed.

When a JPEG's entropy-coded data is gone but its header survived, the EXIF
block at the front of the file usually still holds an intact thumbnail (and on
Samsung phones often a larger preview too). That is a real, viewable photo —
small, but infinitely better than the grey rectangle a truncation-tolerant
decoder produces.

The script scans the head of each broken file for embedded JPEGs (any SOI/EOI
pair after the outer SOI), decodes each candidate strictly, rejects any that are
themselves blank, and keeps the largest one. EXIF from the original header —
date taken, GPS, camera — is carried over, and the file timestamps are
preserved, so the salvaged image still sorts correctly in a photo library.

Usage
  # join against the recovery log so it knows source <-> output pairs
  python3 salvage_thumbnails.py damage_report.csv --recovery-csv recovery_*.csv

  # or translate paths by root
  python3 salvage_thumbnails.py damage_report.csv \
      --source ~/Desktop/to-nas --output ~/Desktop/to-nas_compressed

  --min-damage 50     only touch files at least this %% dead (default 50)
  --dry-run           report what it would salvage, write nothing
  --delete-hopeless   remove the grey placeholder when no preview exists
  --suffix _thumb     mark salvaged files in the filename

Requires: pillow, numpy, rich
"""

from __future__ import annotations

import argparse
import csv
import glob
import os
import sys
from datetime import datetime
from io import BytesIO
from pathlib import Path

import numpy as np
from PIL import Image, ImageFile

try:
    from rich.console import Console
    from rich.progress import BarColumn, Progress, SpinnerColumn, TaskProgressColumn, TextColumn
    from rich.table import Table
except ImportError:
    sys.exit("Needs rich:  python3 -m pip install rich")

console = Console()

HEAD_BYTES = 512 * 1024      # embedded previews live near the front of the file
MIN_PIXELS = 64 * 64         # ignore icon-sized junk
FLAT_STD = 3.0               # a "preview" this uniform is just more dead data


def find_embedded_jpegs(data: bytes) -> list[bytes]:
    """Every SOI..EOI run after the outer SOI, longest first."""
    out, pos = [], 2
    while True:
        start = data.find(b"\xff\xd8\xff", pos)
        if start == -1:
            break
        end = data.find(b"\xff\xd9", start + 2)
        if end != -1:
            out.append(data[start:end + 2])
        pos = start + 2
    out.sort(key=len, reverse=True)
    return out


def decode_strict(blob: bytes) -> Image.Image | None:
    """Decode with truncation tolerance OFF, so garbage is rejected."""
    prev = ImageFile.LOAD_TRUNCATED_IMAGES
    ImageFile.LOAD_TRUNCATED_IMAGES = False
    try:
        im = Image.open(BytesIO(blob))
        im.load()
        return im
    except Exception:
        return None
    finally:
        ImageFile.LOAD_TRUNCATED_IMAGES = prev


def is_blank(im: Image.Image) -> bool:
    """Dead fill has essentially zero variance. Judge at a decent resolution:
    downscaling to a tiny thumbnail averages real detail away and would throw
    out legitimately low-contrast photos (night shots, snow, white walls)."""
    small = im.convert("L")
    small.thumbnail((128, 128), Image.BILINEAR)
    return float(np.asarray(small, dtype=np.float32).std()) < FLAT_STD


def best_preview(src: Path) -> tuple[Image.Image | None, int]:
    try:
        with src.open("rb") as fh:
            head = fh.read(HEAD_BYTES)
    except OSError:
        return None, 0
    for blob in find_embedded_jpegs(head):
        im = decode_strict(blob)
        if im is None:
            continue
        if im.width * im.height < MIN_PIXELS or is_blank(im):
            continue
        return im, len(blob)
    return None, 0


def parent_exif(src: Path) -> bytes:
    """EXIF from the intact header of the broken original."""
    prev = ImageFile.LOAD_TRUNCATED_IMAGES
    ImageFile.LOAD_TRUNCATED_IMAGES = True
    try:
        with Image.open(src) as im:
            exif = im.getexif()
            if 0x0112 in exif:
                exif[0x0112] = 1
            return exif.tobytes()
    except Exception:
        return b""
    finally:
        ImageFile.LOAD_TRUNCATED_IMAGES = prev


def build_map(recovery_csv: str | None, damage_rows: list[dict],
              src_root: Path | None, dst_root: Path | None) -> dict[str, Path]:
    """output path -> source path"""
    mapping: dict[str, Path] = {}
    if recovery_csv:
        for pattern in [recovery_csv]:
            for path in glob.glob(os.path.expanduser(pattern)):
                with open(path, encoding="utf-8") as fh:
                    for r in csv.DictReader(fh):
                        if r.get("output_path"):
                            mapping[r["output_path"]] = Path(r["source_path"])
    if mapping:
        return mapping
    if not (src_root and dst_root):
        sys.exit("Give --recovery-csv, or both --source and --output, so I can "
                 "find the original of each damaged file.")
    for row in damage_rows:
        out = Path(row["path"])
        try:
            rel = out.relative_to(dst_root)
        except ValueError:
            continue
        # the compressed copy is always .jpg; the original may not be
        cands = list((src_root / rel).parent.glob(out.stem + ".*"))
        if cands:
            mapping[str(out)] = cands[0]
    return mapping


def main():
    ap = argparse.ArgumentParser(description="Recover embedded previews from dead photos.")
    ap.add_argument("damage_csv", help="damage_report.csv from check_damage.py")
    ap.add_argument("--recovery-csv", default=None,
                    help="recovery_*.csv, used to pair outputs with their originals")
    ap.add_argument("--source", default=None)
    ap.add_argument("--output", default=None)
    ap.add_argument("--min-damage", type=float, default=50.0)
    ap.add_argument("--quality", type=int, default=90,
                    help="JPEG quality for the salvaged preview (it's small; keep it high)")
    ap.add_argument("--suffix", default="",
                    help="Append to filename, e.g. _thumb, to flag salvaged files")
    ap.add_argument("--delete-hopeless", action="store_true",
                    help="Remove the grey placeholder when nothing can be salvaged")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    damage_path = Path(args.damage_csv).expanduser()
    with damage_path.open(encoding="utf-8") as fh:
        rows = [r for r in csv.DictReader(fh)
                if float(r["damage_pct"]) >= args.min_damage]
    if not rows:
        sys.exit(f"No entries at or above {args.min_damage}% damage.")

    src_root = Path(args.source).expanduser().resolve() if args.source else None
    dst_root = Path(args.output).expanduser().resolve() if args.output else None
    mapping = build_map(args.recovery_csv, rows, src_root, dst_root)

    console.print(f"[bold]{len(rows)}[/] files at ≥{args.min_damage:.0f}% damage"
                  + ("  [yellow](dry run)[/]" if args.dry_run else "") + "\n")

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    report = damage_path.parent / f"salvage_{stamp}.csv"
    fh_out = report.open("w", newline="", encoding="utf-8")
    w = csv.writer(fh_out)
    w.writerow(["result", "preview_px", "damage_pct", "source_path", "output_path"])

    sizes: list[tuple[int, int]] = []
    n_saved = n_none = n_deleted = n_unpaired = 0

    with Progress(SpinnerColumn(), TextColumn("Salvaging"), BarColumn(bar_width=30),
                  TaskProgressColumn(), console=console) as pr:
        task = pr.add_task("", total=len(rows))
        for row in rows:
            out_path = Path(row["path"])
            src = mapping.get(str(out_path))
            if src is None or not src.exists():
                n_unpaired += 1
                w.writerow(["no-original-found", "", row["damage_pct"], "", str(out_path)])
                pr.advance(task)
                continue

            im, _ = best_preview(src)
            if im is None:
                n_none += 1
                if args.delete_hopeless and not args.dry_run:
                    out_path.unlink(missing_ok=True)
                    n_deleted += 1
                w.writerow(["nothing-embedded", "", row["damage_pct"], str(src), str(out_path)])
                pr.advance(task)
                continue

            target = out_path if not args.suffix else out_path.with_name(
                out_path.stem + args.suffix + out_path.suffix)
            px = f"{im.width}x{im.height}"
            sizes.append((im.width, im.height))
            if not args.dry_run:
                if im.mode != "RGB":
                    im = im.convert("RGB")
                tmp = target.with_name(f".{target.stem}.part{target.suffix}")
                im.save(tmp, "JPEG", quality=args.quality, optimize=True,
                        exif=parent_exif(src))
                os.replace(tmp, target)
                try:
                    os.utime(target, (src.stat().st_atime, src.stat().st_mtime))
                except OSError:
                    pass
                if args.suffix and out_path != target:
                    out_path.unlink(missing_ok=True)
            n_saved += 1
            w.writerow(["salvaged", px, row["damage_pct"], str(src), str(target)])
            pr.console.print(f"  [green]{px:>10s}[/] [dim]{out_path.name}[/]")
            pr.advance(task)

    fh_out.close()

    tbl = Table(title="Preview salvage", header_style="bold")
    tbl.add_column("Result"); tbl.add_column("Files", justify="right")
    tbl.add_row("preview recovered", str(n_saved))
    tbl.add_row("nothing embedded", str(n_none))
    if n_deleted:
        tbl.add_row("grey placeholder deleted", str(n_deleted))
    if n_unpaired:
        tbl.add_row("[yellow]original not found[/]", str(n_unpaired))
    console.print(tbl)
    if sizes:
        sizes.sort(key=lambda wh: wh[0] * wh[1])
        w_mid, h_mid = sizes[len(sizes) // 2]
        big = sizes[-1]
        console.print(f"median preview {w_mid}×{h_mid} "
                      f"({w_mid*h_mid/1e6:.2f} MP), largest {big[0]}×{big[1]}")
    console.print(f"\n[dim]{report}[/]")
    if n_none and not args.delete_hopeless:
        console.print("[dim]Re-run with --delete-hopeless to clear the grey files "
                      "that have no preview to replace them.[/]")


if __name__ == "__main__":
    main()