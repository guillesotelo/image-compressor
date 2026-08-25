#!/usr/bin/env python3
"""
check_damage.py — estimate how much of each recovered photo is actually dead.

A JPEG with corrupt entropy data still decodes under LOAD_TRUNCATED_IMAGES, but
everything after the damage point comes out as flat grey (or black) rows. This
scans the recovered files, measures how far up that flat region reaches, and
ranks the worst offenders so you can review a handful instead of a thousand.

It also builds a contact sheet of the worst N, which is the fastest way to see
whether "12% damaged" means a grey strip you'd never notice or a ruined photo.

False positives are expected: a photo of an overcast sky or a white wall has a
genuinely flat bottom. That's why the contact sheet exists — trust your eyes
over the number.

Usage
  python3 check_damage.py recovery_20260820_*.csv
  python3 check_damage.py recovery_*.csv --sheet-count 60 --threshold 3
  python3 check_damage.py --scan /path/to/to-nas_compressed   # any folder of jpgs

Requires: pillow, numpy, rich
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFile

ImageFile.LOAD_TRUNCATED_IMAGES = True

try:
    from rich.console import Console
    from rich.progress import BarColumn, Progress, SpinnerColumn, TaskProgressColumn, TextColumn
    from rich.table import Table
except ImportError:
    sys.exit("Needs rich:  python3 -m pip install rich")

console = Console()

SAMPLE_W = 48          # columns kept when downscaling — enough to judge flatness
SAMPLE_H = 240         # rows scored per image


def damage_score(path: Path, threshold: float) -> tuple[float, str]:
    """Return (fraction of image height that is flat, description of the fill)."""
    with Image.open(path) as im:
        im = im.convert("L").resize((SAMPLE_W, SAMPLE_H), Image.BILINEAR)
        arr = np.asarray(im, dtype=np.float32)
        with Image.open(path) as im2:
            im2 = im2.convert("RGB").resize((SAMPLE_W, SAMPLE_H), Image.BILINEAR)
            rgb = np.asarray(im2, dtype=np.float32)

    row_std = arr.std(axis=1)
    flat = row_std < threshold
    # count consecutive flat rows anchored at the bottom
    n = 0
    for v in flat[::-1]:
        if not v:
            break
        n += 1
    if n == 0:
        return 0.0, ""

    fill = rgb[SAMPLE_H - n:].reshape(-1, 3).mean(axis=0)
    if fill.max() - fill.min() < 12:
        tone = "black" if fill.mean() < 40 else "white" if fill.mean() > 215 else "grey"
    else:
        tone = "colour"
    return n / SAMPLE_H, tone


def contact_sheet(entries: list[tuple[Path, float, str]], out: Path,
                  cols: int = 6, cell: int = 260) -> None:
    rows = (len(entries) + cols - 1) // cols
    label_h = 22
    sheet = Image.new("RGB", (cols * cell, rows * (cell + label_h)), (18, 18, 20))
    draw = ImageDraw.Draw(sheet)
    for i, (path, frac, tone) in enumerate(entries):
        x = (i % cols) * cell
        y = (i // cols) * (cell + label_h)
        try:
            with Image.open(path) as im:
                im = im.convert("RGB")
                im.thumbnail((cell - 8, cell - 8), Image.LANCZOS)
                sheet.paste(im, (x + 4 + (cell - 8 - im.width) // 2,
                                 y + 4 + (cell - 8 - im.height) // 2))
        except Exception:
            draw.rectangle([x + 4, y + 4, x + cell - 4, y + cell - 4], fill=(60, 20, 20))
        name = path.name if len(path.name) <= 30 else path.name[:29] + "…"
        draw.text((x + 6, y + cell), f"{frac*100:.0f}% {tone}  {name}", fill=(200, 200, 205))
    sheet.save(out, "JPEG", quality=88, optimize=True)


def main():
    ap = argparse.ArgumentParser(description="Rank recovered photos by visible damage.")
    ap.add_argument("csv", nargs="?", help="recovery_*.csv from recover_failed.py")
    ap.add_argument("--scan", default=None, help="Scan a folder of .jpg files instead")
    ap.add_argument("--threshold", type=float, default=2.5,
                    help="Row std-dev below this counts as flat (default 2.5)")
    ap.add_argument("--min-damage", type=float, default=1.0,
                    help="Ignore images with less than this %% flat (default 1)")
    ap.add_argument("--sheet-count", type=int, default=48,
                    help="How many of the worst to put on the contact sheet")
    args = ap.parse_args()

    if args.scan:
        files = sorted(p for p in Path(args.scan).expanduser().rglob("*")
                       if p.suffix.lower() in (".jpg", ".jpeg"))
        out_dir = Path(args.scan).expanduser()
    elif args.csv:
        csv_path = Path(args.csv).expanduser()
        with csv_path.open(encoding="utf-8") as fh:
            files = [Path(r["output_path"]) for r in csv.DictReader(fh)
                     if r.get("output_path", "").lower().endswith((".jpg", ".jpeg"))]
        out_dir = csv_path.parent
    else:
        sys.exit("Give a recovery CSV or --scan FOLDER")

    files = [f for f in files if f.exists()]
    if not files:
        sys.exit("No JPEGs found to check.")
    console.print(f"Checking [bold]{len(files)}[/] images\n")

    results: list[tuple[Path, float, str]] = []
    with Progress(SpinnerColumn(), TextColumn("Scanning"), BarColumn(bar_width=30),
                  TaskProgressColumn(), console=console) as pr:
        t = pr.add_task("", total=len(files))
        for f in files:
            try:
                frac, tone = damage_score(f, args.threshold)
                if frac * 100 >= args.min_damage:
                    results.append((f, frac, tone))
            except Exception as e:
                console.print(f"  [red]!! {f.name}: {e}[/]")
            pr.advance(t)

    results.sort(key=lambda r: -r[1])

    buckets = [("over 50% dead", 0.50), ("25-50%", 0.25), ("10-25%", 0.10),
               ("3-10%", 0.03), ("under 3%", 0.0)]
    tbl = Table(title="Estimated flat/dead area", header_style="bold")
    tbl.add_column("Damage"); tbl.add_column("Files", justify="right")
    prev = 1.01
    for label, lo in buckets:
        n = sum(1 for _, f, _ in results if lo <= f < prev)
        if n:
            tbl.add_row(label, str(n))
        prev = lo
    tbl.add_row("[dim]clean[/]", f"[dim]{len(files) - len(results)}[/]")
    console.print(tbl)

    report = out_dir / "damage_report.csv"
    with report.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["damage_pct", "fill", "path"])
        for p, frac, tone in results:
            w.writerow([f"{frac*100:.1f}", tone, str(p)])
    console.print(f"\n[dim]{report}[/]")

    if results:
        sheet = out_dir / "damage_contact_sheet.jpg"
        contact_sheet(results[:args.sheet_count], sheet)
        console.print(f"[dim]{sheet}[/]  — worst {min(args.sheet_count, len(results))}, "
                      "open this before deciding anything")


if __name__ == "__main__":
    main()