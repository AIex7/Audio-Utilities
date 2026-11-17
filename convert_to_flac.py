#!/usr/bin/env python3
"""
Recursively convert APE, WAV, and AIFF files to lossless FLAC using ffmpeg,
preserving metadata tags (and embedded cover art if present).

Verifies bit-for-bit audio identity via decoded PCM MD5 before moving the
original file to macOS Trash.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

SUPPORTED_EXTENSIONS = {".ape", ".wav", ".aiff", ".aif"}


def find_sources(root: Path) -> list[Path]:
    """Recursively find supported audio files under root, ignoring hidden entries."""
    sources: list[Path] = []
    for current_dir, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if not d.startswith("."))
        dir_path = Path(current_dir)
        for filename in sorted(files):
            if filename.startswith("."):
                continue
            file_path = dir_path / filename
            if file_path.suffix.lower() in SUPPORTED_EXTENSIONS:
                sources.append(file_path)
    return sources


def get_audio_md5(ffmpeg: str, audio_file: Path) -> str:
    """Decode the primary audio stream to raw PCM and return its MD5 string."""
    result = subprocess.run(
        [ffmpeg, "-v", "error", "-i", str(audio_file), "-map", "0:a:0", "-f", "md5", "-"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"ffmpeg exited with {result.returncode}")
    md5_output = result.stdout.strip()
    if not md5_output.startswith("MD5="):
        raise RuntimeError(f"Unexpected MD5 output from ffmpeg: {md5_output!r}")
    return md5_output


def convert_file(ffmpeg: str, source: Path, root: Path, dry_run: bool = False) -> str:
    """Convert a single audio file to FLAC, verify losslessness, and trash the original."""
    rel_source = source.relative_to(root)
    destination = source.with_suffix(".flac")
    rel_dest = destination.relative_to(root)

    if destination.exists():
        print(f"[SKIP] Output already exists: {rel_dest}")
        return "skipped"

    if dry_run:
        print(f"[DRY RUN] Would convert: {rel_source} -> {rel_dest} (and trash original)")
        return "converted"

    print(f"[CONVERT] {rel_source} -> {rel_dest}")
    with tempfile.TemporaryDirectory(dir=source.parent) as temp_dir:
        temp_flac = Path(temp_dir) / "output.flac"
        encode_result = subprocess.run(
            [
                ffmpeg,
                "-v", "error",
                "-y",
                "-i", str(source),
                "-map", "0:a:0",
                "-map", "0:v?",
                "-map_metadata", "0",
                "-map_chapters", "0",
                "-c:a", "flac",
                "-compression_level", "8",
                "-c:v", "copy",
                "-disposition:v:0", "attached_pic",
                str(temp_flac),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if encode_result.returncode != 0 or not temp_flac.exists() or temp_flac.stat().st_size == 0:
            err = encode_result.stderr.strip() or f"ffmpeg exited with {encode_result.returncode}"
            print(f"[ERROR] Conversion failed for {rel_source}: {err}", file=sys.stderr)
            return "error"

        try:
            source_md5 = get_audio_md5(ffmpeg, source)
            dest_md5 = get_audio_md5(ffmpeg, temp_flac)
        except RuntimeError as exc:
            print(f"[ERROR] Verification failed for {rel_source}: {exc}", file=sys.stderr)
            return "error"

        if source_md5 != dest_md5:
            print(
                f"[ERROR] Audio MD5 mismatch for {rel_source}: "
                f"source={source_md5} != destination={dest_md5}",
                file=sys.stderr,
            )
            return "error"

        temp_flac.replace(destination)

    try:
        subprocess.run(["trash", str(source)], check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as exc:
        err = exc.stderr.strip() or f"trash exited with {exc.returncode}"
        print(f"[ERROR] Converted {rel_dest}, but failed trashing {rel_source}: {err}", file=sys.stderr)
        return "error"

    print(f"[DONE] {rel_dest} ({dest_md5}); trashed {rel_source}")
    return "converted"


def main() -> int:
    ffmpeg = shutil.which("ffmpeg")
    trash = shutil.which("trash")
    missing = [name for name, path in (("ffmpeg", ffmpeg), ("trash", trash)) if not path]
    if missing:
        print(f"Error: Missing required tool(s) in PATH: {', '.join(missing)}", file=sys.stderr)
        return 1

    parser = argparse.ArgumentParser(
        description="Recursively convert APE, WAV, and AIFF files to lossless FLAC with tags and trash originals."
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
        help="Preview conversions without creating FLAC files or trashing originals",
    )
    args = parser.parse_args()

    root = Path(args.directory).resolve()
    if not root.is_dir():
        print(f"Error: Directory does not exist: {args.directory}", file=sys.stderr)
        return 1

    sources = find_sources(root)
    if not sources:
        print("No APE, WAV, or AIFF files found.")
        return 0

    converted = 0
    skipped = 0
    errors = 0

    for source in sources:
        status = convert_file(ffmpeg, source, root, dry_run=args.dry_run)
        if status == "converted":
            converted += 1
        elif status == "skipped":
            skipped += 1
        else:
            errors += 1

    print("=" * 60)
    print(f"Done. Converted: {converted} | Skipped: {skipped} | Errors: {errors}")
    return 1 if errors > 0 else 0


if __name__ == "__main__":
    raise SystemExit(main())
