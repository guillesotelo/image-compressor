#!/usr/bin/env python3
"""
match_carved.py — restore identity to a carved dump.

You have two halves of the same library:
  * named-but-dead files   — correct filename, EXIF and preview, destroyed pixels
  * carved-but-anonymous   — real pixels, meaningless names (f0295360.jpg)

This pairs them up and writes the carved photo into the compressed tree under
its proper name, folder and timestamp.

Matching, in order of confidence
  1. EXIF capture time. A Samsung filename IS the capture time
     (20170110_094059.jpg), and the carved file carries the same value in EXIF.
     Exact key match — no guessing.
  2. Perceptual hash. For carved files whose EXIF was lost, the 512x288 preview
     salvaged from the dead file is compared against the carved image with a
     dHash. Robust to resolution and quality differences, since both descend
     from the same original photo.
  3. When several carved copies match one target, the healthiest and largest
     wins; ties broken by closest hash.

Carved files that are themselves broken are detected and never used to replace
a good preview with another grey rectangle.

Usage
  # see what would match, write nothing
  python3 match_carved.py --carved ~/Desktop/sd_dump \
      --salvage-csv 'salvage_*.csv' --output ~/Desktop/to-nas_compressed --dry-run

  # commit, compressing matches to the same quality as the rest of the library
  python3 match_carved.py --carved ~/Desktop/sd_dump \
      --salvage-csv 'salvage_*.csv' --output ~/Desktop/to-nas_compressed \
      --photo-quality 65

  --hash-distance N   max dHash Hamming distance for a visual match (default 12)
  --no-visual         EXIF timestamp matching only
  --report-only       write the CSV, touch no image files

Requires: pillow, numpy, rich
"""

from __future__ import annotations

import argparse
import csv
import glob
import os
import re
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
from PIL import Image, ImageFile, ImageOps

try:
    from rich.console import Console
    from rich.progress import BarColumn, Progress, SpinnerColumn, TaskProgressColumn, TextColumn
    from rich.table import Table
except ImportError:
    sys.exit("Needs rich:  python3 -m pip install rich")

console = Console()

PHOTO_EXTS = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".tif", ".tiff", ".webp"}
FLAT_STD = 3.0
NAME_TS = re.compile(r"(20\d{6})[_-]?(\d{6})")


# ------------------------------------------------------------------ helpers
def norm_ts(value: str | None) -> str | None:
    """'2017:01:10 09:40:59' or '20170110_094059' -> '20170110094059'"""
    if not value:
        return None
    digits = re.sub(r"\D", "", str(value))
    return digits[:14] if len(digits) >= 14 else None


def exif_timestamp(im: Image.Image) -> str | None:
    try:
        exif = im.getexif()
    except Exception:
        return None
    for tag in (0x9003, 0x9004):                    # DateTimeOriginal, DateTimeDigitized
        try:
            v = exif.get_ifd(0x8769).get(tag)
        except Exception:
            v = None
        if v:
            return norm_ts(v)
    return norm_ts(exif.get(0x0132))                # DateTime


def name_timestamp(path: Path) -> str | None:
    m = NAME_TS.search(path.stem)
    return (m.group(1) + m.group(2)) if m else None


def dhash(im: Image.Image) -> int:
    g = im.convert("L").resize((9, 8), Image.BILINEAR)
    a = np.asarray(g, dtype=np.int16)
    bits = (a[:, 1:] > a[:, :-1]).flatten()
    v = 0
    for b in bits:
        v = (v << 1) | int(b)
    return v


def hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


def open_fast(path: Path, box: int = 256) -> Image.Image | None:
    """Decode at reduced scale — much faster over thousands of files."""
    prev = ImageFile.LOAD_TRUNCATED_IMAGES
    ImageFile.LOAD_TRUNCATED_IMAGES = True
    try:
        im = Image.open(path)
        im.draft("L", (box, box))
        im.load()
        return im
    except Exception:
        return None
    finally:
        ImageFile.LOAD_TRUNCATED_IMAGES = prev


def is_healthy(im: Image.Image) -> bool:
    g = im.convert("L")
    g.thumbnail((128, 128), Image.BILINEAR)
    return float(np.asarray(g, dtype=np.float32).std()) >= FLAT_STD


# ------------------------------------------------------------------ indexing
def index_carved(folder: Path) -> tuple[dict[str, list[dict]], list[dict]]:
    files = [p for p in folder.rglob("*") if p.suffix.lower() in PHOTO_EXTS and p.is_file()]
    by_ts: dict[str, list[dict]] = {}
    no_ts: list[dict] = []
    with Progress(SpinnerColumn(), TextColumn("Indexing carved dump"),
                  BarColumn(bar_width=30), TaskProgressColumn(), console=console) as pr:
        t = pr.add_task("", total=len(files))
        for p in files:
            im = open_fast(p)
            if im is None:
                pr.advance(t)
                continue
            rec = {"path": p, "healthy": is_healthy(im), "hash": dhash(im),
                   "px": 0, "size": p.stat().st_size}
            try:
                with Image.open(p) as full:
                    rec["px"] = full.width * full.height
                    ts = exif_timestamp(full)
            except Exception:
                ts = None
            if ts:
                by_ts.setdefault(ts, []).append(rec)
            else:
                no_ts.append(rec)
            pr.advance(t)
    return by_ts, no_ts


def load_targets(salvage_glob: str) -> list[dict]:
    rows: list[dict] = []
    for path in sorted(glob.glob(os.path.expanduser(salvage_glob))):
        with open(path, encoding="utf-8") as fh:
            for r in csv.DictReader(fh):
                if r.get("result") in ("salvaged", "nothing-embedded") and r.get("source_path"):
                    rows.append(r)
    return rows


def rank(cands: list[dict], ref_hash: int | None) -> dict:
    def key(c):
        d = hamming(c["hash"], ref_hash) if ref_hash is not None else 99
        return (0 if c["healthy"] else 1, d, -c["px"], -c["size"])
    return sorted(cands, key=key)[0]


# ------------------------------------------------------------------ main
def main():
    ap = argparse.ArgumentParser(description="Match a carved dump back to real filenames.")
    ap.add_argument("--carved", required=True, help="Folder of recovered/carved files")
    ap.add_argument("--salvage-csv", required=True, help="salvage_*.csv (glob ok)")
    ap.add_argument("--output", required=True, help="Compressed tree root")
    ap.add_argument("--photo-quality", type=int, default=65)
    ap.add_argument("--max-dimension", type=int, default=0)
    ap.add_argument("--hash-distance", type=int, default=12)
    ap.add_argument("--no-visual", action="store_true")
    ap.add_argument("--visual-all", action="store_true",
                    help="Compare previews against every carved file, not just the "
                         "ones missing an EXIF timestamp")
    ap.add_argument("--report-only", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    carved_root = Path(args.carved).expanduser().resolve()
    if not carved_root.is_dir():
        sys.exit(f"Not a folder: {carved_root}")
    out_root = Path(args.output).expanduser().resolve()

    targets = load_targets(args.salvage_csv)
    if not targets:
        sys.exit("No target rows found in the salvage CSV.")
    console.print(f"[bold]{len(targets)}[/] damaged photos to find originals for")

    by_ts, no_ts = index_carved(carved_root)
    n_carved = sum(len(v) for v in by_ts.values()) + len(no_ts)
    healthy = sum(1 for v in by_ts.values() for c in v if c["healthy"]) \
        + sum(1 for c in no_ts if c["healthy"])
    all_recs = [c for v in by_ts.values() for c in v] + no_ts
    console.print(f"[bold]{n_carved}[/] carved files indexed "
                  f"([green]{healthy}[/] healthy, {n_carved-healthy} broken, "
                  f"{len(no_ts)} without EXIF time)\n")

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    report = Path(args.salvage_csv).parent if os.path.dirname(args.salvage_csv) \
        else Path.cwd()
    report = report / f"match_{stamp}.csv"
    fh = report.open("w", newline="", encoding="utf-8")
    w = csv.writer(fh)
    w.writerow(["result", "method", "hash_distance", "target_name", "carved_path",
                "carved_px", "output_path"])

    counts = {"exif": 0, "visual": 0, "matched-but-broken": 0, "no-match": 0}
    used: set[Path] = set()

    with Progress(SpinnerColumn(), TextColumn("Matching"), BarColumn(bar_width=30),
                  TaskProgressColumn(), console=console) as pr:
        task = pr.add_task("", total=len(targets))
        for row in targets:
            src = Path(row["source_path"])
            out_path = Path(row["output_path"])
            ts = name_timestamp(src)
            if not ts:
                with Image.open(src) as im0:
                    ts = exif_timestamp(im0)

            # reference hash from the salvaged preview, if one exists
            ref_hash = None
            if not args.no_visual and out_path.exists():
                prev_im = open_fast(out_path)
                if prev_im is not None and is_healthy(prev_im):
                    ref_hash = dhash(prev_im)

            method, chosen, dist = "", None, ""
            cands = [c for c in by_ts.get(ts, []) if c["path"] not in used] if ts else []
            if cands:
                chosen = rank(cands, ref_hash)
                method = "exif"
                if ref_hash is not None:
                    dist = hamming(chosen["hash"], ref_hash)
            elif ref_hash is not None and not args.no_visual:
                # by default only EXIF-less carved files are eligible; --visual-all
                # opens it to everything, which catches timestamp drift/rewrites
                pool_src = all_recs if args.visual_all else no_ts
                pool = [c for c in pool_src if c["path"] not in used and c["healthy"]]
                best, best_d = None, 999
                for c in pool:
                    d = hamming(c["hash"], ref_hash)
                    if d < best_d:
                        best, best_d = c, d
                if best is not None and best_d <= args.hash_distance:
                    chosen, method, dist = best, "visual", best_d

            if chosen is None:
                counts["no-match"] += 1
                w.writerow(["no-match", "", "", src.name, "", "", str(out_path)])
                pr.advance(task)
                continue

            if not chosen["healthy"]:
                counts["matched-but-broken"] += 1
                w.writerow(["matched-but-broken", method, dist, src.name,
                            str(chosen["path"]), chosen["px"], str(out_path)])
                pr.advance(task)
                continue

            used.add(chosen["path"])
            counts[method] += 1
            w.writerow(["matched", method, dist, src.name, str(chosen["path"]),
                        chosen["px"], str(out_path)])
            pr.console.print(f"  [green]{method:6s}[/] {src.name} "
                             f"[dim]<- {chosen['path'].name} ({chosen['px']/1e6:.1f} MP)[/]"
                             + (f" [dim]d={dist}[/]" if dist != "" else ""))

            if not (args.dry_run or args.report_only):
                out_path.parent.mkdir(parents=True, exist_ok=True)
                try:
                    with Image.open(chosen["path"]) as im:
                        im = ImageOps.exif_transpose(im)
                        exif = im.getexif()
                        if 0x0112 in exif:
                            exif[0x0112] = 1
                        if args.max_dimension:
                            im.thumbnail((args.max_dimension, args.max_dimension),
                                         Image.LANCZOS)
                        if im.mode != "RGB":
                            im = im.convert("RGB")
                        tmp = out_path.with_name(f".{out_path.stem}.part{out_path.suffix}")
                        im.save(tmp, "JPEG", quality=args.photo_quality, optimize=True,
                                progressive=True, exif=exif.tobytes())
                    os.replace(tmp, out_path)
                    st = src.stat()
                    os.utime(out_path, (st.st_atime, st.st_mtime))
                except Exception as e:
                    pr.console.print(f"    [red]write failed: {e}[/]")
            pr.advance(task)

    fh.close()

    t = Table(title="Carved-dump matching", header_style="bold")
    t.add_column("Result"); t.add_column("Files", justify="right")
    t.add_row("matched by EXIF time", str(counts["exif"]))
    t.add_row("matched visually", str(counts["visual"]))
    t.add_row("[yellow]match found but also broken[/]", str(counts["matched-but-broken"]))
    t.add_row("[dim]no match[/]", str(counts["no-match"]))
    console.print(t)
    recovered = counts["exif"] + counts["visual"]
    if recovered:
        console.print(f"[bold green]{recovered}[/] photos restored at full resolution"
                      + ("  [yellow](dry run — nothing written)[/]"
                         if args.dry_run or args.report_only else ""))
    console.print(f"\n[dim]{report}[/]")


if __name__ == "__main__":
    main()
