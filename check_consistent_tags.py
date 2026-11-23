#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.9"
# dependencies = [
#     "mutagen>=1.47.0",
# ]
# ///
"""
Recursively check each directory of .mp3/.m4a files to ensure every file has an
album tag and an album artist (or artist) tag, and that all files in the directory
share the same album, effective artist (album artist if present, else artist),
and compilation flag. Directories failing this check are tagged Orange in Finder via xattr.
"""

from __future__ import annotations

import argparse
import os
import plistlib
import subprocess
import sys
from pathlib import Path

from mutagen.id3 import ID3
from mutagen.mp4 import MP4

AUDIO_EXTENSIONS = {".mp3", ".m4a"}
FINDER_TAGS_XATTR = "com.apple.metadata:_kMDItemUserTags"
ORANGE_FINDER_TAG = "Orange\n7"


def _first_nonempty_text(values: object) -> str | None:
    """Return the first non-whitespace string from a mutagen tag value, or None."""
    if not values:
        return None
    if isinstance(values, (list, tuple)):
        for value in values:
            if isinstance(value, str) and value.strip():
                return value.strip()
        return None
    if isinstance(values, str) and values.strip():
        return values.strip()
    return None


def get_track_tags(audio_path: Path) -> tuple[str | None, str | None, bool]:
    """Read (album, effective_artist, is_compilation) using album artist if present, else artist."""
    ext = audio_path.suffix.lower()
    try:
        if ext == ".m4a":
            tags = MP4(audio_path).tags
            if not tags:
                return None, None, False
            album = _first_nonempty_text(tags.get("\xa9alb"))
            album_artist = _first_nonempty_text(tags.get("aART"))
            artist = _first_nonempty_text(tags.get("\xa9ART"))
            cpil = tags.get("cpil")
            is_compilation = (
                bool(cpil[0]) if isinstance(cpil, (list, tuple)) and cpil else bool(cpil)
            )
            return album, album_artist or artist, is_compilation

        if ext == ".mp3":
            tags = ID3(audio_path)
            talb = tags.get("TALB")
            tpe2 = tags.get("TPE2")
            tpe1 = tags.get("TPE1")
            tcmp = tags.get("TCMP")
            album = _first_nonempty_text(talb.text) if talb else None
            album_artist = _first_nonempty_text(tpe2.text) if tpe2 else None
            artist = _first_nonempty_text(tpe1.text) if tpe1 else None
            is_compilation = bool(tcmp and tcmp.text and str(tcmp.text[0]).strip() == "1")
            return album, album_artist or artist, is_compilation
    except Exception:
        return None, None, False

    return None, None, False


def directory_has_consistent_tags(audio_files: list[Path]) -> bool:
    """Return True iff every file has album + artist/album-artist and all share the same tags."""
    expected_album: str | None = None
    expected_artist: str | None = None
    expected_compilation: bool | None = None

    for audio_path in audio_files:
        album, effective_artist, is_compilation = get_track_tags(audio_path)
        if not album or not effective_artist:
            return False

        if expected_album is None:
            expected_album = album
            expected_artist = effective_artist
            expected_compilation = is_compilation
        elif (
            album != expected_album
            or effective_artist != expected_artist
            or is_compilation != expected_compilation
        ):
            return False

    return True


def tag_directory_orange(dir_path: Path) -> None:
    """Set the Finder Orange tag on dir_path via xattr, preserving any existing tags."""
    tags: list[str] = []
    read_res = subprocess.run(
        ["xattr", "-px", FINDER_TAGS_XATTR, str(dir_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    if read_res.returncode == 0 and read_res.stdout.strip():
        try:
            loaded = plistlib.loads(bytes.fromhex(read_res.stdout))
            if isinstance(loaded, list):
                tags = [t for t in loaded if isinstance(t, str)]
        except Exception:
            tags = []

    if ORANGE_FINDER_TAG in tags:
        return

    tags.append(ORANGE_FINDER_TAG)
    hex_plist = plistlib.dumps(tags, fmt=plistlib.FMT_BINARY).hex()
    subprocess.run(
        ["xattr", "-wx", FINDER_TAGS_XATTR, hex_plist, str(dir_path)],
        check=True,
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Recursively verify each folder of .mp3/.m4a files shares the same album "
            "and effective artist (album artist, falling back to artist), tagging "
            "mismatched folders Orange in Finder."
        )
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

        if not directory_has_consistent_tags(audio_files):
            tag_directory_orange(dir_path)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
