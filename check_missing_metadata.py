#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.9"
# dependencies = [
#     "mutagen>=1.47.0",
# ]
# ///
"""Recursively check .mp3 and .m4a files (read-only) and print in red any missing artist, album, or index tags."""

from __future__ import annotations

import argparse
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


def _has_nonempty_text(values: object) -> bool:
    """Return True if a mutagen text tag list/value contains non-whitespace text."""
    if not values:
        return False
    if isinstance(values, (list, tuple)):
        return any(isinstance(v, str) and v.strip() for v in values)
    return isinstance(values, str) and bool(values.strip())


def find_missing_tags(audio_path: Path) -> list[str]:
    """Return a list of missing tag names among ['artist', 'album', 'index']."""
    ext = audio_path.suffix.lower()
    has_artist = False
    has_album = False
    has_index = False

    try:
        if ext == ".m4a":
            tags = MP4(audio_path).tags
            if tags:
                has_artist = _has_nonempty_text(tags.get("\xa9ART"))
                has_album = _has_nonempty_text(tags.get("\xa9alb"))
                track_tag = tags.get("trkn")
                track_num = track_tag[0][0] if track_tag and track_tag[0] else 0
                has_index = isinstance(track_num, int) and track_num > 0
        elif ext == ".mp3":
            tags = ID3(audio_path)
            tpe1 = tags.get("TPE1")
            talb = tags.get("TALB")
            trck = tags.get("TRCK")
            has_artist = bool(tpe1 and _has_nonempty_text(tpe1.text))
            has_album = bool(talb and _has_nonempty_text(talb.text))
            has_index = bool(trck and trck.text and _parse_positive_int(trck.text[0]) is not None)
    except Exception:
        return ["artist", "album", "index"]

    missing: list[str] = []
    if not has_artist:
        missing.append("artist")
    if not has_album:
        missing.append("album")
    if not has_index:
        missing.append("index")
    return missing


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Recursively check .mp3/.m4a files for missing artist, album, or track index tags (read-only)."
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

        for file_name in sorted(files):
            if file_name.startswith("."):
                continue
            audio_path = dir_path / file_name
            if audio_path.suffix.lower() not in AUDIO_EXTENSIONS:
                continue

            missing = find_missing_tags(audio_path)
            if missing:
                rel_path = audio_path.relative_to(root_dir)
                missing_str = ", ".join(missing)
                print(f"{ANSI_RED}Missing [{missing_str}] - {rel_path}{ANSI_RESET}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
