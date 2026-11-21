#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.9"
# dependencies = [
#     "mutagen>=1.47.0",
# ]
# ///
"""
Recursively find directories containing .mp3/.m4a files and exactly one image file
(.jpeg, .png, .jpg, case-insensitive). Embeds the image into each audio file using
mutagen, and trashes the image if all embeddings succeed.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

from mutagen.id3 import APIC
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4, MP4Cover

AUDIO_EXTENSIONS = {".mp3", ".m4a"}
IMAGE_EXTENSIONS = {".jpeg", ".png", ".jpg"}


def embed_image(audio_path: Path, image_bytes: bytes, is_png: bool) -> bool:
    """Embed image_bytes into an .mp3 or .m4a file, replacing any existing cover art."""
    ext = audio_path.suffix.lower()
    try:
        if ext == ".mp3":
            audio = MP3(audio_path)
            if audio.tags is None:
                audio.add_tags()
            audio.tags.delall("APIC")
            mime = "image/png" if is_png else "image/jpeg"
            audio.tags.add(
                APIC(
                    encoding=3,
                    mime=mime,
                    type=3,
                    desc="Cover",
                    data=image_bytes,
                )
            )
            audio.save()
            return True

        if ext == ".m4a":
            audio = MP4(audio_path)
            if audio.tags is None:
                audio.add_tags()
            fmt = MP4Cover.FORMAT_PNG if is_png else MP4Cover.FORMAT_JPEG
            audio.tags["covr"] = [MP4Cover(image_bytes, imageformat=fmt)]
            audio.save()
            return True
    except Exception as exc:
        print(f"[ERROR] Failed embedding into {audio_path}: {exc}", file=sys.stderr)
        return False

    return False


def process_directory(
    dir_path: Path,
    audio_files: list[Path],
    image_path: Path,
    root: Path,
    trash: str,
    dry_run: bool,
) -> tuple[int, int]:
    """Embed image_path into audio_files and trash image_path if all succeed. Returns (embedded, errors)."""
    rel_img = image_path.relative_to(root)
    if dry_run:
        print(f"[DRY RUN] Would embed {rel_img} into {len(audio_files)} file(s) and trash {rel_img}")
        return len(audio_files), 0

    try:
        image_bytes = image_path.read_bytes()
    except OSError as exc:
        print(f"[ERROR] Could not read {rel_img}: {exc}", file=sys.stderr)
        return 0, 1

    is_png = image_path.suffix.lower() == ".png"
    embedded = 0
    errors = 0

    for audio_path in audio_files:
        if embed_image(audio_path, image_bytes, is_png=is_png):
            embedded += 1
        else:
            errors += 1

    if errors == 0:
        res = subprocess.run([trash, str(image_path)], capture_output=True, text=True, check=False)
        if res.returncode != 0:
            err = res.stderr.strip() or f"trash exited with {res.returncode}"
            print(f"[ERROR] Embedded into {embedded} file(s), but failed trashing {rel_img}: {err}", file=sys.stderr)
            errors += 1
        else:
            print(f"[DONE] Embedded {rel_img} into {embedded} file(s) and trashed {rel_img}")
    else:
        print(f"[WARN] Kept {rel_img} because {errors} file(s) failed to embed", file=sys.stderr)

    return embedded, errors


def main() -> int:
    trash = shutil.which("trash")
    if not trash:
        print("Error: Missing required tool 'trash' in PATH (brew install trash).", file=sys.stderr)
        return 1

    parser = argparse.ArgumentParser(
        description=(
            "Recursively find folders with .mp3/.m4a files and exactly one image "
            "(.jpeg/.png/.jpg), embed it with mutagen, and trash the image."
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
        help="Preview embeddings and trashes without modifying any files",
    )
    args = parser.parse_args()

    root = Path(args.directory).resolve()
    if not root.is_dir():
        print(f"Error: Directory does not exist: {args.directory}", file=sys.stderr)
        return 1

    total_embedded = 0
    total_errors = 0

    for current_dir, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if not d.startswith("."))
        dir_path = Path(current_dir)

        audio_files: list[Path] = []
        image_files: list[Path] = []
        for name in sorted(files):
            if name.startswith("."):
                continue
            path = dir_path / name
            ext = path.suffix.lower()
            if ext in AUDIO_EXTENSIONS:
                audio_files.append(path)
            elif ext in IMAGE_EXTENSIONS:
                image_files.append(path)

        if audio_files and len(image_files) == 1:
            embedded, errors = process_directory(
                dir_path, audio_files, image_files[0], root, trash, dry_run=args.dry_run
            )
            total_embedded += embedded
            total_errors += errors

    print("=" * 60)
    print(f"Done. Embedded files: {total_embedded} | Errors: {total_errors}")
    return 1 if total_errors > 0 else 0


if __name__ == "__main__":
    raise SystemExit(main())
