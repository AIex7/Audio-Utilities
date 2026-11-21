#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.9"
# dependencies = [
#     "mutagen>=1.47.0",
# ]
# ///
"""
Recursively scan directories. If a directory's visible files are all .flac
(with no .png/.jpg/.jpeg files) and every .flac file contains the same embedded
cover image, extract that image to cover.jpg or cover.png.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from mutagen.flac import FLAC, Picture

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg"}


def get_primary_flac_image(flac_path: Path) -> tuple[bytes, str] | None:
    """Return (image_bytes, 'png' | 'jpg') from a FLAC file, or None if missing/unsupported."""
    try:
        audio = FLAC(flac_path)
    except Exception as exc:
        print(f"[WARN] Could not read {flac_path}: {exc}", file=sys.stderr)
        return None

    if not audio.pictures:
        return None

    pic: Picture = next((p for p in audio.pictures if p.type == 3), audio.pictures[0])
    data = pic.data
    if not data:
        return None

    mime = pic.mime.lower()
    if data.startswith(b"\x89PNG\r\n\x1a\n") or "png" in mime:
        return data, "png"
    if data.startswith(b"\xff\xd8\xff") or "jpeg" in mime or "jpg" in mime:
        return data, "jpg"
    return None


def process_directory(dir_path: Path, visible_files: list[str], root: Path, dry_run: bool) -> str:
    """Process a single directory. Returns 'extracted', 'skipped', or 'error'."""
    if not visible_files:
        return "skipped"

    if any(Path(f).suffix.lower() in IMAGE_EXTENSIONS for f in visible_files):
        return "skipped"

    if not all(Path(f).suffix.lower() == ".flac" for f in visible_files):
        return "skipped"

    expected: tuple[bytes, str] | None = None
    for filename in sorted(visible_files):
        extracted = get_primary_flac_image(dir_path / filename)
        if extracted is None:
            return "skipped"
        if expected is None:
            expected = extracted
        elif extracted[0] != expected[0]:
            return "skipped"

    if expected is None:
        return "skipped"

    img_data, ext = expected
    dest = dir_path / f"cover.{ext}"
    if dest.exists():
        return "skipped"

    rel_dest = dest.relative_to(root)
    if dry_run:
        print(f"[DRY RUN] Would extract {rel_dest} ({len(img_data)} bytes)")
        return "extracted"

    try:
        dest.write_bytes(img_data)
    except OSError as exc:
        print(f"[ERROR] Failed writing {rel_dest}: {exc}", file=sys.stderr)
        return "error"

    print(f"[EXTRACT] {rel_dest} ({len(img_data)} bytes)")
    return "extracted"


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Recursively extract cover.jpg or cover.png in directories that contain "
            "only .flac files (no .png/.jpg/.jpeg) sharing the same embedded image."
        )
    )
    parser.add_argument(
        "directory",
        nargs="?",
        default=".",
        help="Root directory to recursively scan (default: current directory)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview extractions without writing any files",
    )
    args = parser.parse_args()

    root = Path(args.directory).resolve()
    if not root.is_dir():
        print(f"Error: Directory does not exist: {args.directory}", file=sys.stderr)
        return 1

    extracted = 0
    errors = 0

    for current_dir, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if not d.startswith("."))
        visible_files = [f for f in files if not f.startswith(".")]
        status = process_directory(Path(current_dir), visible_files, root, dry_run=args.dry_run)
        if status == "extracted":
            extracted += 1
        elif status == "error":
            errors += 1

    print("=" * 60)
    print(f"Done. Extracted: {extracted} | Errors: {errors}")
    return 1 if errors > 0 else 0


if __name__ == "__main__":
    raise SystemExit(main())
