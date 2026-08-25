#!/usr/bin/env python3
"""
analyze_corruption.py — work out what actually happened to the dead photos.

The files are full-size and almost entirely non-zero, yet their image data is
unreadable. This samples them and answers three questions:

  1. Where does the good data stop?  (offset of the last valid JPEG structure)
  2. What is the bad region made of?  The decisive test is FF-stuffing: inside
     real JPEG scan data every 0xFF byte must be followed by 0x00 or a restart
     marker. Valid data scores ~100%. Random garbage scores ~4% by chance.
        high score -> the bytes ARE JPEG data, just the wrong bytes (cross-linked
                      clusters, shifted extents) — a disk-level imaging tool may
                      recover the real ones
        low score  -> the bytes are foreign data (another file, junk, noise) —
                      the photo is not in this file at all
  3. Is the garbage shared between files?  Identical tails across photos point at
     one repeated junk source rather than per-file damage.

It also flags suspiciously uniform file sizes, which is a signature of a carving
tool that allocated a fixed block per file rather than recovering true lengths.

Usage
  python3 analyze_corruption.py salvage_*.csv              # reads source_path column
  python3 analyze_corruption.py failed.txt --sample 60
  python3 analyze_corruption.py salvage_*.csv --sample 0   # analyze all (slow)

Requires: rich
"""

from __future__ import annotations

import argparse
import csv
import glob
import hashlib
import math
import os
import random
import sys
from collections import Counter
from pathlib import Path

try:
    from rich.console import Console
    from rich.table import Table
except ImportError:
    sys.exit("Needs rich:  python3 -m pip install rich")

console = Console()
TAIL_SAMPLE = 256 * 1024      # bytes of the damaged region to characterise


def entropy(data: bytes) -> float:
    if not data:
        return 0.0
    counts = Counter(data)
    n = len(data)
    return -sum((c / n) * math.log2(c / n) for c in counts.values())


def ff_stuffing_score(data: bytes) -> tuple[float, int]:
    """Fraction of 0xFF bytes legally followed (0x00 or RST D0-D7). ~1.0 = real
    JPEG scan data; ~0.04 = random bytes."""
    total = legal = 0
    for i in range(len(data) - 1):
        if data[i] == 0xFF:
            total += 1
            nxt = data[i + 1]
            if nxt == 0x00 or 0xD0 <= nxt <= 0xD7:
                legal += 1
    return (legal / total if total else 0.0), total


def last_valid_offset(data: bytes) -> int:
    """Walk the JPEG marker segments; return where the structure stops making sense."""
    if data[:2] != b"\xff\xd8":
        return 0
    i = 2
    while i < len(data) - 3:
        if data[i] != 0xFF:
            return i
        marker = data[i + 1]
        if marker == 0xDA:                       # SOS: entropy data follows
            return i + 2 + int.from_bytes(data[i + 2:i + 4], "big")
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        seg_len = int.from_bytes(data[i + 2:i + 4], "big")
        if seg_len < 2:
            return i
        i += 2 + seg_len
    return i


def foreign_jpegs(data: bytes) -> int:
    n, pos = 0, 0
    while True:
        pos = data.find(b"\xff\xd8\xff", pos)
        if pos == -1:
            return n
        n += 1
        pos += 3


def load_paths(spec: str) -> list[Path]:
    matches = glob.glob(os.path.expanduser(spec))
    if not matches:
        sys.exit(f"No file matches {spec}")
    path = Path(matches[0])
    out: list[Path] = []
    if path.suffix.lower() == ".csv":
        with path.open(encoding="utf-8") as fh:
            for r in csv.DictReader(fh):
                p = r.get("source_path") or r.get("path") or ""
                if p:
                    out.append(Path(p))
    else:
        out = [Path(l.strip()) for l in path.read_text(encoding="utf-8").splitlines()
               if l.strip()]
    return [p for p in out if p.exists()]


def main():
    ap = argparse.ArgumentParser(description="Characterise the corruption in dead photos.")
    ap.add_argument("input", help="salvage_*.csv, recovery_*.csv, or a list of paths")
    ap.add_argument("--sample", type=int, default=40, help="0 = every file")
    ap.add_argument("--seed", type=int, default=0)
    args = ap.parse_args()

    paths = load_paths(args.input)
    if not paths:
        sys.exit("No readable source files found.")
    all_sizes = [p.stat().st_size for p in paths]
    if args.sample and len(paths) > args.sample:
        random.seed(args.seed)
        sample = random.sample(paths, args.sample)
    else:
        sample = paths
    console.print(f"Analyzing [bold]{len(sample)}[/] of {len(paths)} files\n")

    rows = []
    tail_hashes: Counter[str] = Counter()
    for p in sample:
        data = p.read_bytes()
        good = last_valid_offset(data)
        tail = data[good:good + TAIL_SAMPLE]
        if not tail:
            continue
        score, ff_count = ff_stuffing_score(tail)
        rows.append({
            "path": p, "size": len(data), "good": good,
            "good_pct": good * 100 / len(data),
            "ent": entropy(tail), "ff": score, "ffn": ff_count,
            "zeros": tail.count(0) * 100 / len(tail),
            "printable": sum(1 for b in tail if 32 <= b < 127) * 100 / len(tail),
            "foreign": foreign_jpegs(tail),
        })
        tail_hashes[hashlib.sha1(tail[:4096]).hexdigest()[:12]] += 1

    def avg(k):
        return sum(r[k] for r in rows) / len(rows)

    t = Table(title="Damaged region — what the bytes are", header_style="bold")
    t.add_column("Measure"); t.add_column("Value", justify="right"); t.add_column("Reading", style="dim")
    ff = avg("ff")
    t.add_row("FF-stuffing legality", f"{ff*100:.1f}%",
              "valid JPEG scan data" if ff > 0.85 else
              "partly JPEG-like" if ff > 0.25 else "not JPEG data at all")
    t.add_row("Shannon entropy", f"{avg('ent'):.2f} / 8.00",
              "compressed or encrypted" if avg("ent") > 7.5 else
              "structured / repetitive" if avg("ent") < 6.5 else "mixed")
    t.add_row("Zero bytes", f"{avg('zeros'):.1f}%", "not a sparse/unwritten region"
              if avg("zeros") < 20 else "partially unwritten")
    t.add_row("ASCII-printable", f"{avg('printable'):.1f}%",
              "looks like text/documents" if avg("printable") > 60 else "binary")
    t.add_row("Valid data before damage", f"{avg('good_pct'):.1f}% of file",
              f"≈ {avg('good')/1024:.0f} KB intact at the front")
    t.add_row("Foreign JPEG headers found", f"{sum(r['foreign'] for r in rows)}",
              "fragments of other images" if sum(r["foreign"] for r in rows) else "none")
    console.print(t)

    # shared junk?
    dupes = [(h, n) for h, n in tail_hashes.most_common(3) if n > 1]
    if dupes:
        console.print(f"\n[yellow]Shared junk:[/] {dupes[0][1]} of {len(rows)} sampled files "
                      "begin their damaged region with identical bytes — one repeated "
                      "source of garbage, not independent damage.")
    else:
        console.print("\nEvery damaged region differs — damage is per-file, "
                      "not one repeated pattern.")

    # size fingerprints
    size_counts = Counter(all_sizes)
    common = size_counts.most_common(3)
    if common[0][1] > max(3, len(all_sizes) * 0.05):
        console.print(f"[yellow]Size fingerprint:[/] {common[0][1]} files are exactly "
                      f"{common[0][0]:,} bytes — a tool allocated a fixed length rather "
                      "than recovering true file sizes.")
    round_sizes = sum(1 for s in all_sizes if s % 1_000_000 < 200 or s % 1_048_576 < 200)
    if round_sizes > len(all_sizes) * 0.1:
        console.print(f"[yellow]{round_sizes}[/] of {len(all_sizes)} files sit on a suspiciously "
                      "round size boundary — consistent with padded/carved output.")

    console.print("\n[bold]What this means[/]")
    if ff > 0.85:
        console.print("  The damaged region is genuine JPEG entropy data — just not this "
                      "photo's. Clusters were cross-linked or extents misassigned, so the "
                      "real bytes may still exist elsewhere on the source disk. Imaging it "
                      "with ddrescue and re-carving is worth trying.")
    elif ff > 0.25:
        console.print("  Mixed: some stretches are JPEG-like, some aren't. Partial overwrite. "
                      "A disk image plus carving may recover some, not all.")
    else:
        console.print("  The damaged region is not image data in any form — it is foreign "
                      "content that replaced the photo. Nothing in these files can be turned "
                      "back into the original picture; only another copy of the photo can.")


if __name__ == "__main__":
    main()