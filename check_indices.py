#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.9"
# dependencies = [
#     "mutagen>=1.47.0",
# ]
# ///
"""Recursively check track and disc indices per folder (read-only) and report missing or duplicate indices in red."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import os
import sys
from pathlib import Path

from mutagen.id3 import ID3
from mutagen.mp4 import MP4

AUDIO_EXTENSIONS = {".mp3", ".m4a"}
ANSI_RED = "\033[31m"
ANSI_RESET = "\033[0m"


def _parse_positive_int(raw: object) -> int | None:
    """Parse a positive integer from a tag string like '3' or '3/12'."""
    text = str(raw).split("/", 1)[0].strip()
    if not text:
        return None
    try:
        value = int(text)
    except ValueError:
        return None
    return value if value > 0 else None


def get_disc_and_track(audio_path: Path) -> tuple[int, int | None]:
    """Read (disc_number, track_number) from tags in read-only mode. Defaults disc to 1."""
    ext = audio_path.suffix.lower()
    try:
        if ext == ".m4a":
            tags = MP4(audio_path).tags
            if not tags:
                return 1, None
            disc_tag = tags.get("disk")
            track_tag = tags.get("trkn")
            disc_num = disc_tag[0][0] if disc_tag and disc_tag[0] else 0
            track_num = track_tag[0][0] if track_tag and track_tag[0] else 0
            return (disc_num if disc_num > 0 else 1), (track_num if track_num > 0 else None)

        if ext == ".mp3":
            tags = ID3(audio_path)
            tpos = tags.get("TPOS")
            trck = tags.get("TRCK")
            disc_num = _parse_positive_int(tpos.text[0]) if tpos and tpos.text else None
            track_num = _parse_positive_int(trck.text[0]) if trck and trck.text else None
            return (disc_num or 1), track_num
    except Exception:
        return 1, None

    return 1, None


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Check for missing or duplicate track/disc indices in each folder of .mp3/.m4a files (read-only)."
    )
    parser.add_argument(
        "directory",
        nargs="?",
        default=".",
        help="Root directory to recursively scan (default: current directory)",
    )
    args = parser.parse_args()

    root_dir = Path(args.directory).resolve()
    if not root_dir.exists():
        print(f"Error: Directory does not exist: {root_dir}", file=sys.stderr)
        return 1

    for current_dir, dirs, files in os.walk(root_dir):
        dirs[:] = sorted(d for d in dirs if not d.startswith("."))
        dir_path = Path(current_dir)

        audio_files = [
            dir_path / f
            for f in sorted(files)
            if not f.startswith(".") and Path(f).suffix.lower() in AUDIO_EXTENSIONS
        ]
        if not audio_files:
            continue

        discs: dict[int, Counter[int]] = defaultdict(Counter)
        for audio_path in audio_files:
            disc_num, track_num = get_disc_and_track(audio_path)
            if track_num is not None:
                discs[disc_num][track_num] += 1

        if not discs:
            continue

        rel_dir = "." if dir_path == root_dir else str(dir_path.relative_to(root_dir))
        max_disc = max(discs)
        if max_disc > 1:
            missing_discs = [d for d in range(1, max_disc + 1) if d not in discs]
            if missing_discs:
                missing_disc_str = ", ".join(str(d) for d in missing_discs)
                print(f"{ANSI_RED}Missing discs [{missing_disc_str}] - {rel_dir}{ANSI_RESET}")

        for disc_num in sorted(discs):
            track_counts = discs[disc_num]
            max_index = max(track_counts)
            missing = [i for i in range(1, max_index + 1) if i not in track_counts]
            duplicates = sorted(num for num, count in track_counts.items() if count > 1)
            disc_label = f" (Disc {disc_num})" if len(discs) > 1 or disc_num > 1 else ""

            if missing:
                missing_str = ", ".join(str(i) for i in missing)
                print(f"{ANSI_RED}Missing indices [{missing_str}] - {rel_dir}{disc_label}{ANSI_RESET}")
            if duplicates:
                dup_str = ", ".join(str(i) for i in duplicates)
                print(f"{ANSI_RED}Duplicate indices [{dup_str}] - {rel_dir}{disc_label}{ANSI_RESET}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
