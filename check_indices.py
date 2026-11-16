#!/usr/bin/env python3
"""Recursively check kMDItemAudioTrackNumber via mdls per folder and report missing or duplicate track indices in red."""

from __future__ import annotations

import argparse
from collections import Counter
import os
import subprocess
import sys
from pathlib import Path

AUDIO_EXTENSIONS = {".mp3", ".m4a"}
ANSI_RED = "\033[31m"
ANSI_RESET = "\033[0m"


def get_track_number(audio_path: Path) -> int | None:
    """Read kMDItemAudioTrackNumber for an audio file using mdls. Returns None if missing."""
    result = subprocess.run(
        ["mdls", "-name", "kMDItemAudioTrackNumber", "-raw", str(audio_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return None

    value = result.stdout.strip()
    if not value or value == "(null)":
        return None

    try:
        track_num = int(value)
    except ValueError:
        return None

    return track_num if track_num > 0 else None


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Check for missing or duplicate kMDItemAudioTrackNumber indices in each folder of .mp3/.m4a files."
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

        track_counts: Counter[int] = Counter()
        for audio_path in audio_files:
            track_num = get_track_number(audio_path)
            if track_num is not None:
                track_counts[track_num] += 1

        if not track_counts:
            continue

        rel_dir = "." if dir_path == root_dir else str(dir_path.relative_to(root_dir))
        max_index = max(track_counts)
        missing = [i for i in range(1, max_index + 1) if i not in track_counts]
        duplicates = sorted(num for num, count in track_counts.items() if count > 1)

        if missing:
            missing_str = ", ".join(str(i) for i in missing)
            print(f"{ANSI_RED}Missing indices [{missing_str}] - {rel_dir}{ANSI_RESET}")
        if duplicates:
            dup_str = ", ".join(str(i) for i in duplicates)
            print(f"{ANSI_RED}Duplicate indices [{dup_str}] - {rel_dir}{ANSI_RESET}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
