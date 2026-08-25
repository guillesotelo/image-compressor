#!/usr/bin/env python3
"""
cleanup_duplicates.py — retire the anonymous copies once their real names exist.

You deliberately included the carved `recovered/` folder in the compression
source, knowing some of those files are the intact twins of broken photos
elsewhere in the library. After match_carved.py restores them under their proper
names, the anonymously-named compressed copies are redundant.

This reads the match report and, for every committed match, finds the compressed
copy of the carved file and removes it — but only after confirming, by comparing
the two images, that it really is the same picture. Anything that fails that
check is left alone and flagged.

Nothing is deleted by default: files are moved to a quarantine folder so you can
eyeball them and restore any mistake. Use --delete only once you're satisfied.

Usage
  # see what would go, verify nothing
  python3 cleanup_duplicates.py --match-csv match_20260820_195431.csv \
      --source ~/Desktop/to-nas --output ~/Desktop/to-nas_compressed

  # commit: move duplicates into <output>/_duplicates_removed/
  python3 cleanup_duplicates.py --match-csv match_...csv \
      --source ~/Desktop/to-nas --output ~/Desktop/to-nas_compressed --commit

  # commit and delete outright instead of quarantining
  python3 cleanup_duplicates.py --match-csv match_...csv \
      --source ~/Desktop/to-nas --output ~/Desktop/to-nas_compressed --commit --delete

  --hash-distance N   how similar the two images must be to count as duplicates
                      (default 12; lower is stricter)
  --skip-verify       trust the match report without comparing images (not advised)

Requires: pillow, numpy, rich
"""

from __future__ import annotations

import argparse
import csv
import glob
import os
import shutil
import sys
from datetime import datetime
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
QUARANTINE = "_duplicates_removed"


def dims(path: Path) -> tuple[int, int] | None:
    """True stored dimensions, read from the header without decoding."""
    try:
        with Image.open(path) as im:
            return im.size
    except Exception:
        return None


def open_fast(path: Path, box: int = 256) -> Image.Image | None:
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


def dhash(im: Image.Image) -> int:
    g = im.convert("L").resize((9, 8), Image.BILINEAR)
    a = np.asarray(g, dtype=np.int16)
    v = 0
    for b in (a[:, 1:] > a[:, :-1]).flatten():
        v = (v << 1) | int(b)
    return v


def hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


def compressed_counterpart(carved: Path, src_root: Path, dst_root: Path) -> Path | None:
    """Where compress_media.py would have written this source file."""
    try:
        rel = carved.relative_to(src_root)
    except ValueError:
        return None
    # photos are always written as .jpg; other types keep their extension
    as_jpg = (dst_root / rel).with_suffix(".jpg")
    if as_jpg.exists():
        return as_jpg
    plain = dst_root / rel
    return plain if plain.exists() else None


def load_matches(spec: str) -> list[dict]:
    rows: list[dict] = []
    files = sorted(glob.glob(os.path.expanduser(spec)))
    if not files:
        sys.exit(f"No file matches {spec}")
    for f in files:
        with open(f, encoding="utf-8") as fh:
            rows += [r for r in csv.DictReader(fh)
                     if r.get("result") == "matched" and r.get("carved_path")]
    return rows


def main():
    ap = argparse.ArgumentParser(
        description="Remove anonymous compressed copies whose photos now have real names.")
    ap.add_argument("--match-csv", required=True, help="match_*.csv from match_carved.py")
    ap.add_argument("--source", required=True, help="Compression source root")
    ap.add_argument("--output", required=True, help="Compressed tree root")
    ap.add_argument("--hash-distance", type=int, default=12)
    ap.add_argument("--skip-verify", action="store_true")
    ap.add_argument("--commit", action="store_true", help="Actually move/delete files")
    ap.add_argument("--delete", action="store_true",
                    help="Delete outright instead of moving to quarantine")
    args = ap.parse_args()

    src_root = Path(args.source).expanduser().resolve()
    dst_root = Path(args.output).expanduser().resolve()
    rows = load_matches(args.match_csv)
    if not rows:
        sys.exit("No committed matches in that report — run match_carved.py without "
                 "--dry-run first.")

    console.print(f"[bold]{len(rows)}[/] matches to clean up after"
                  + ("" if args.commit else "   [yellow](preview — nothing will change)[/]"))
    quarantine = dst_root / QUARANTINE
    if args.commit and not args.delete:
        console.print(f"[dim]Quarantine: {quarantine}[/]")
    console.print()

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    report = Path(args.match_csv).expanduser().parent / f"cleanup_{stamp}.csv"
    fh = report.open("w", newline="", encoding="utf-8")
    w = csv.writer(fh)
    w.writerow(["action", "reason", "hash_distance", "duplicate_path",
                "kept_named_copy", "bytes_freed"])

    counts = {"removed": 0, "kept-target-missing": 0, "kept-not-duplicate": 0,
              "kept-no-copy-found": 0, "kept-unreadable": 0,
              "kept-lower-resolution": 0}
    freed = 0

    with Progress(SpinnerColumn(), TextColumn("Checking"), BarColumn(bar_width=30),
                  TaskProgressColumn(), console=console) as pr:
        task = pr.add_task("", total=len(rows))
        for r in rows:
            carved = Path(r["carved_path"])
            named_out = Path(r["output_path"])
            dup = compressed_counterpart(carved, src_root, dst_root)

            if dup is None:
                counts["kept-no-copy-found"] += 1
                w.writerow(["kept", "no compressed copy of the carved file", "", "",
                            str(named_out), ""])
                pr.advance(task)
                continue

            if not named_out.exists():
                counts["kept-target-missing"] += 1
                w.writerow(["kept", "named copy not written yet", "", str(dup),
                            str(named_out), ""])
                pr.advance(task)
                continue

            # If the named slot still holds the salvaged preview rather than the
            # restored full-size photo, removing the anonymous copy would leave
            # only the low-res version. Happens when match_carved.py was run with
            # --dry-run: the report says "matched" but nothing was written.
            d_dup, d_named = dims(dup), dims(named_out)
            if d_dup and d_named and (d_named[0] * d_named[1]) < 0.5 * (d_dup[0] * d_dup[1]):
                counts["kept-lower-resolution"] += 1
                w.writerow(["kept", f"named copy is only {d_named[0]}x{d_named[1]} vs "
                                    f"{d_dup[0]}x{d_dup[1]}", "", str(dup),
                            str(named_out), ""])
                pr.console.print(f"  [yellow]kept[/] {dup.name} [dim]— named copy is "
                                 f"still {d_named[0]}x{d_named[1]} (preview, not restored)[/]")
                pr.advance(task)
                continue

            dist = ""
            if not args.skip_verify:
                a, b = open_fast(dup), open_fast(named_out)
                if a is None or b is None:
                    counts["kept-unreadable"] += 1
                    w.writerow(["kept", "could not read one of the pair", "", str(dup),
                                str(named_out), ""])
                    pr.advance(task)
                    continue
                dist = hamming(dhash(a), dhash(b))
                if dist > args.hash_distance:
                    counts["kept-not-duplicate"] += 1
                    w.writerow(["kept", "images differ", dist, str(dup),
                                str(named_out), ""])
                    pr.console.print(f"  [yellow]kept[/] {dup.name} "
                                     f"[dim]differs from {named_out.name} (d={dist})[/]")
                    pr.advance(task)
                    continue

            size = dup.stat().st_size
            if args.commit:
                try:
                    if args.delete:
                        dup.unlink()
                    else:
                        rel = dup.relative_to(dst_root)
                        target = quarantine / rel
                        target.parent.mkdir(parents=True, exist_ok=True)
                        shutil.move(str(dup), str(target))
                except Exception as e:
                    counts["kept-unreadable"] += 1
                    w.writerow(["kept", f"move/delete failed: {e}", dist, str(dup),
                                str(named_out), ""])
                    pr.advance(task)
                    continue
            counts["removed"] += 1
            freed += size
            w.writerow(["removed" if args.commit else "would-remove", "duplicate confirmed",
                        dist, str(dup), str(named_out), size])
            pr.console.print(f"  [green]{'removed' if args.commit else 'would remove'}[/] "
                             f"[dim]{dup.relative_to(dst_root)}[/] "
                             f"→ kept {named_out.name}"
                             + (f" [dim]d={dist}[/]" if dist != "" else ""))
            pr.advance(task)
    fh.close()

    t = Table(title="Duplicate cleanup", header_style="bold")
    t.add_column("Outcome"); t.add_column("Files", justify="right")
    t.add_row("removed" if args.commit else "would remove", str(counts["removed"]))
    for key, label in (("kept-lower-resolution", "kept — named copy still a preview"),
                       ("kept-not-duplicate", "kept — images differ"),
                       ("kept-target-missing", "kept — named copy missing"),
                       ("kept-no-copy-found", "kept — no compressed copy"),
                       ("kept-unreadable", "kept — unreadable")):
        if counts[key]:
            t.add_row(f"[yellow]{label}[/]", str(counts[key]))
    console.print(t)
    console.print(f"{freed/1024/1024:.1f} MB "
                  + ("freed" if args.commit else "would be freed"))
    if args.commit and not args.delete and counts["removed"]:
        console.print(f"[dim]Moved to {quarantine} — delete that folder once you're happy.[/]")
    if not args.commit:
        console.print("[dim]Re-run with --commit to apply.[/]")
    console.print(f"\n[dim]{report}[/]")


if __name__ == "__main__":
    main()
