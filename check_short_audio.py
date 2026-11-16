#!/usr/bin/env python3
"""Recursively run afinfo on .mp3/.m4a files and print filenames with duration <= 60s in red."""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

AUDIO_EXTENSIONS = {".mp3", ".m4a"}
DURATION_PATTERN = re.compile(r"estimated duration:\s*([0-9]+(?:\.[0-9]+)?)\s*sec")
ANSI_RED = "\033[31m"
ANSI_RESET = "\033[0m"


def get_estimated_duration(audio_path: Path) -> float | None:
    """Run afinfo on audio_path and return estimated duration in seconds, or None."""
    result = subprocess.run(
        ["afinfo", str(audio_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return None

    match = DURATION_PATTERN.search(result.stdout)
    if not match:
        return None
    return float(match.group(1))


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Recursively find .mp3/.m4a files with estimated duration <= 60 seconds using afinfo."
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
        for file_name in sorted(files):
            if file_name.startswith("."):
                continue
            file_path = Path(current_dir) / file_name
            if file_path.suffix.lower() not in AUDIO_EXTENSIONS:
                continue

            duration = get_estimated_duration(file_path)
            if duration is not None and duration <= 60.0:
                print(f"{ANSI_RED}{file_path.relative_to(root_dir)}{ANSI_RESET}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
