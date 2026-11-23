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


def _parse_int_part(raw: object, part_index: int = 0) -> int | None:
    """Parse a positive integer from a tag string like '3' or '3/12'."""
    parts = str(raw).split("/", 1)
    if part_index >= len(parts):
        return None
    text = parts[part_index].strip()
    if not text:
        return None
    try:
        value = int(text)
    except ValueError:
        return None
    return value if value > 0 else None


def get_disc_and_track(audio_path: Path) -> tuple[int, int | None, int | None]:
    """Read (disc_number, track_number, total_tracks) from tags in read-only mode. Defaults disc to 1."""
    ext = audio_path.suffix.lower()
    try:
        if ext == ".m4a":
            tags = MP4(audio_path).tags
            if not tags:
                return 1, None, None
            disc_tag = tags.get("disk")
            track_tag = tags.get("trkn")
            disc_num = disc_tag[0][0] if disc_tag and disc_tag[0] else 0
            track_num = track_tag[0][0] if track_tag and track_tag[0] else 0
            total_tracks = (
                track_tag[0][1]
                if track_tag and track_tag[0] and len(track_tag[0]) > 1
                else 0
            )
            return (
                (disc_num if disc_num > 0 else 1),
                (track_num if track_num > 0 else None),
                (total_tracks if total_tracks > 0 else None),
            )

        if ext == ".mp3":
            tags = ID3(audio_path)
            tpos = tags.get("TPOS")
            trck = tags.get("TRCK")
            disc_num = _parse_int_part(tpos.text[0], 0) if tpos and tpos.text else None
            track_num = _parse_int_part(trck.text[0], 0) if trck and trck.text else None
            total_tracks = _parse_int_part(trck.text[0], 1) if trck and trck.text else None
            return (disc_num or 1), track_num, total_tracks
    except Exception:
        return 1, None, None

    return 1, None, None


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
    parser.add_argument(
        "-e", "--exclude",
        action="extend",
        nargs="+",
        default=[],
        metavar="DIRNAME",
        help="Directory name(s) to exclude from scanning (basename, not full path)",
    )
    args = parser.parse_args()

    excluded_dirs = set(args.exclude)
    root_dir = Path(args.directory).resolve()
    if not root_dir.exists():
        print(f"Error: Directory does not exist: {root_dir}", file=sys.stderr)
        return 1

    for current_dir, dirs, files in os.walk(root_dir):
        dirs[:] = sorted(
            d for d in dirs if not d.startswith(".") and d not in excluded_dirs
        )
        dir_path = Path(current_dir)

        audio_files = [
            dir_path / f
            for f in sorted(files)
            if not f.startswith(".") and Path(f).suffix.lower() in AUDIO_EXTENSIONS
        ]
        if not audio_files:
            continue

        discs: dict[int, Counter[int]] = defaultdict(Counter)
        expected_totals: dict[int, int] = {}
        for audio_path in audio_files:
            disc_num, track_num, total_tracks = get_disc_and_track(audio_path)
            if track_num is not None:
                discs[disc_num][track_num] += 1
            if total_tracks is not None:
                expected_totals[disc_num] = max(expected_totals.get(disc_num, 0), total_tracks)

        all_discs = sorted(set(discs) | set(expected_totals))
        if not all_discs:
            continue

        rel_dir = "." if dir_path == root_dir else str(dir_path.relative_to(root_dir))
        max_disc = max(all_discs)
        if max_disc > 1:
            missing_discs = [d for d in range(1, max_disc + 1) if d not in all_discs]
            if missing_discs:
                missing_disc_str = ", ".join(str(d) for d in missing_discs)
                print(f"{ANSI_RED}Missing discs [{missing_disc_str}] - {rel_dir}{ANSI_RESET}")

        for disc_num in all_discs:
            track_counts = discs[disc_num]
            max_index = max(track_counts, default=0)
            upper_bound = max(max_index, expected_totals.get(disc_num, 0))
            missing = [i for i in range(1, upper_bound + 1) if i not in track_counts]
            duplicates = sorted(num for num, count in track_counts.items() if count > 1)
            disc_label = f" (Disc {disc_num})" if len(all_discs) > 1 or disc_num > 1 else ""

            if missing:
                missing_str = ", ".join(str(i) for i in missing)
                print(f"{ANSI_RED}Missing indices [{missing_str}] - {rel_dir}{disc_label}{ANSI_RESET}")
            if duplicates:
                dup_str = ", ".join(str(i) for i in duplicates)
                print(f"{ANSI_RED}Duplicate indices [{dup_str}] - {rel_dir}{disc_label}{ANSI_RESET}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
