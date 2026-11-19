#!/usr/bin/env python3
"""Recursively read kMDItemAlbum via mdls for .m4a/.mp3 files, check ~/Music, and tag missing albums' parent directories green in Finder."""

from __future__ import annotations

import argparse
import os
import plistlib
import subprocess
import sys
import unicodedata
from pathlib import Path

AUDIO_EXTENSIONS = {".m4a", ".mp3"}
MUSIC_DIR_ENV = "MUSIC_DIR"
ANSI_RED = "\033[31m"
ANSI_GREEN = "\033[32m"
ANSI_RESET = "\033[0m"
FINDER_TAGS_XATTR = "com.apple.metadata:_kMDItemUserTags"
GREEN_FINDER_TAG = "Green\n2"


def normalize_text(text: str) -> str:
    """Normalize Unicode (NFC) and casefold for reliable macOS filename matching."""
    return unicodedata.normalize("NFC", text).casefold()


def get_mdls_album_and_authors(audio_path: Path) -> tuple[str | None, list[str]]:
    """Read kMDItemAlbum and kMDItemAuthors for an audio file using mdls -plist."""
    result = subprocess.run(
        ["mdls", "-plist", "-", str(audio_path)],
        capture_output=True,
        check=False,
    )
    if result.returncode != 0 or not result.stdout:
        return None, []

    try:
        metadata = plistlib.loads(result.stdout)
    except Exception:
        return None, []

    raw_album = metadata.get("kMDItemAlbum")
    album = raw_album.strip() if isinstance(raw_album, str) and raw_album.strip() else None

    raw_authors = metadata.get("kMDItemAuthors")
    authors: list[str] = []
    if isinstance(raw_authors, list):
        authors = [a.strip() for a in raw_authors if isinstance(a, str) and a.strip()]
    elif isinstance(raw_authors, str) and raw_authors.strip():
        authors = [raw_authors.strip()]

    return album, authors


def index_music_directory(music_dir: Path) -> list[tuple[str, str]]:
    """
    Walk ~/Music returning (relative_path_str, normalized_relative_path)
    for every directory and file.
    """
    entries: list[tuple[str, str]] = []
    if not music_dir.exists():
        return entries

    for current_dir, dirs, files in os.walk(music_dir):
        current_path = Path(current_dir)
        for name in sorted(dirs) + sorted(files):
            if name.startswith("."):
                continue
            entry_path = current_path / name
            rel_path = str(entry_path.relative_to(music_dir))
            entries.append((rel_path, normalize_text(rel_path)))

    return entries


def find_album_in_music(
    album: str,
    authors: list[str],
    music_entries: list[tuple[str, str]],
) -> str | None:
    """Return the relative path of the first entry in ~/Music whose path contains both album and an author."""
    if not authors:
        return None

    album_needle = normalize_text(album)
    author_needles = [normalize_text(author) for author in authors]

    for rel_path, normalized_rel_path in music_entries:
        if album_needle in normalized_rel_path and any(
            author_needle in normalized_rel_path for author_needle in author_needles
        ):
            return rel_path
    return None


def tag_directory_green(dir_path: Path) -> None:
    """Set the Finder Green tag on dir_path via xattr, preserving any existing tags."""
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

    if GREEN_FINDER_TAG in tags:
        return

    tags.append(GREEN_FINDER_TAG)
    hex_plist = plistlib.dumps(tags, fmt=plistlib.FMT_BINARY).hex()
    subprocess.run(
        ["xattr", "-wx", FINDER_TAGS_XATTR, hex_plist, str(dir_path)],
        check=True,
    )


def resolve_music_dir() -> Path:
    """Return the Music directory from MUSIC_DIR env var if set, otherwise ~/Music."""
    env_path = os.environ.get(MUSIC_DIR_ENV, "").strip()
    if env_path:
        return Path(env_path).expanduser().resolve()
    return Path.home() / "Music"


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Check if albums (via mdls kMDItemAlbum) from .m4a/.mp3 files already exist "
            f"in ${MUSIC_DIR_ENV} (default: ~/Music)."
        )
    )
    parser.add_argument(
        "directory",
        nargs="?",
        default=".",
        help="Root directory to recursively scan for .m4a/.mp3 files (default: current directory)",
    )
    args = parser.parse_args()

    scan_dir = Path(args.directory).resolve()
    cwd = Path.cwd().resolve()
    if not scan_dir.exists():
        print(f"Error: Directory does not exist: {scan_dir}", file=sys.stderr)
        return 1

    music_dir = resolve_music_dir()
    music_entries = index_music_directory(music_dir)

    seen_albums: set[tuple[str, tuple[str, ...]]] = set()

    for current_dir, dirs, files in os.walk(scan_dir):
        dirs[:] = sorted(d for d in dirs if not d.startswith("."))
        for file_name in sorted(files):
            if file_name.startswith("."):
                continue
            file_path = Path(current_dir) / file_name
            if file_path.suffix.lower() not in AUDIO_EXTENSIONS:
                continue

            album, authors = get_mdls_album_and_authors(file_path)
            if not album:
                continue

            album_key = (
                normalize_text(album),
                tuple(sorted(normalize_text(author) for author in authors)),
            )
            if album_key in seen_albums:
                continue
            seen_albums.add(album_key)

            label = f"{album} ({', '.join(authors)})" if authors else album
            match_rel_path = find_album_in_music(album, authors, music_entries)
            if match_rel_path is not None:
                print(f"{ANSI_RED}{label} - Album Exists at {match_rel_path}{ANSI_RESET}")
            else:
                print(f"{ANSI_GREEN}{label} - Album does not exist yet{ANSI_RESET}")
                if file_path.parent.resolve() != cwd:
                    tag_directory_green(file_path.parent)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
