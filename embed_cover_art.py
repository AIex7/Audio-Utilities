# /// script
# requires-python = ">=3.9"
# dependencies = [
#     "mutagen",
#     "pillow",
# ]
# ///
"""
Recursively scans for directories containing cover.jpg or cover.png.
For each directory:
- Checks the width of the cover image.
- If width <= 600: embeds the original image directly.
- If width > 600: creates a 600x600 resized file in /tmp and embeds that.
- Embeds the artwork into every .mp3 and .m4a file in that directory.
- Ensures the final audio file contains ONLY that artwork (removes any previous artwork).
- Preserves all other audio metadata tags untouched.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
from pathlib import Path

import mutagen
from mutagen.id3 import APIC, ID3
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4, MP4Cover
from PIL import Image


def get_cover_file(dir_path: Path, filenames: list[str]) -> Path | None:
    """Find cover.jpg or cover.png in the directory (case-insensitive)."""
    for f in filenames:
        if f.lower() in ("cover.jpg", "cover.jpeg", "cover.png"):
            return dir_path / f
    return None


def prepare_cover_data(cover_path: Path) -> tuple[bytes, str, str]:
    """
    Inspect cover image dimensions and format based purely on binary data (Pillow img.format).
    Returns:
        (image_bytes, mime_type, description_string)
    """
    with Image.open(cover_path) as img:
        width, height = img.size
        img_format = (img.format or "").upper()

    is_png = img_format == "PNG"
    is_jpeg = img_format in ("JPEG", "JPG")

    # If width <= 600 and already standard JPEG/PNG, embed directly
    if width <= 600 and (is_png or is_jpeg):
        mime = "image/png" if is_png else "image/jpeg"
        desc = f"Direct original ({width}x{height}, {img_format})"
        return cover_path.read_bytes(), mime, desc

    # Either width > 600 or non-standard format (e.g. WebP) converted to JPEG/PNG in /tmp
    ext = ".png" if is_png else ".jpg"
    file_hash = hashlib.md5(str(cover_path.resolve()).encode()).hexdigest()[:10]
    tmp_path = Path("/tmp") / f"resized_600x600_{file_hash}{ext}"

    with Image.open(cover_path) as img:
        if width > 600:
            # Resize preserving aspect ratio (neither dimension exceeds 600)
            img.thumbnail((600, 600), Image.Resampling.LANCZOS)
        new_w, new_h = img.size

        if is_png:
            img.save(tmp_path, format="PNG", optimize=True)
            mime = "image/png"
        else:
            if img.mode not in ("RGB", "L"):
                img = img.convert("RGB")
            img.save(tmp_path, format="JPEG", quality=92, optimize=True)
            mime = "image/jpeg"

    if width <= 600:
        desc = f"Converted in /tmp ({img_format} -> JPEG {new_w}x{new_h}, {tmp_path.name})"
    else:
        desc = f"Resized in /tmp preserving aspect ratio ({img_format} {width}x{height} -> {new_w}x{new_h}, {tmp_path.name})"

    return tmp_path.read_bytes(), mime, desc


def embed_art_in_mp3(file_path: Path, image_bytes: bytes, mime_type: str) -> bool:
    """
    Embed artwork into an MP3 file.
    Replaces all existing APIC frames so only this artwork exists.
    Preserves all other ID3 tags.
    """
    try:
        try:
            audio = MP3(file_path)
        except Exception:
            audio = mutagen.File(file_path)

        if audio is None:
            return False

        if audio.tags is None:
            try:
                audio.add_tags()
            except Exception:
                pass

        if audio.tags is None:
            return False

        # Remove all existing picture frames (APIC)
        if hasattr(audio.tags, "delall"):
            audio.tags.delall("APIC")

        # In case any other APIC key format exists in the tag mapping
        keys_to_delete = [k for k in audio.tags.keys() if k.startswith("APIC")]
        for k in keys_to_delete:
            del audio.tags[k]

        # Add the single cover image frame
        audio.tags.add(
            APIC(
                encoding=3,  # UTF-8
                mime=mime_type,
                type=3,  # 3 is Front Cover
                desc="Cover",
                data=image_bytes,
            )
        )

        audio.save()
        return True
    except Exception as err:
        print(f"      [ERROR] Could not save MP3 '{file_path.name}': {err}", file=sys.stderr)
        return False


def embed_art_in_m4a(file_path: Path, image_bytes: bytes, mime_type: str) -> bool:
    """
    Embed artwork into an M4A/MP4 file.
    Replaces existing 'covr' tags so only this artwork exists.
    Preserves all other MP4 tags.
    """
    try:
        try:
            audio = MP4(file_path)
        except Exception:
            audio = mutagen.File(file_path)

        if audio is None:
            return False

        if audio.tags is None:
            try:
                audio.add_tags()
            except Exception:
                pass

        if audio.tags is None:
            return False

        img_format = MP4Cover.FORMAT_PNG if mime_type == "image/png" else MP4Cover.FORMAT_JPEG
        # Replace 'covr' with a single MP4Cover item
        audio.tags["covr"] = [MP4Cover(image_bytes, imageformat=img_format)]

        audio.save()
        return True
    except Exception as err:
        print(f"      [ERROR] Could not save M4A '{file_path.name}': {err}", file=sys.stderr)
        return False


def embed_artwork_in_file(file_path: Path, image_bytes: bytes, mime_type: str) -> bool:
    ext = file_path.suffix.lower()
    if ext == ".mp3":
        return embed_art_in_mp3(file_path, image_bytes, mime_type)
    elif ext in (".m4a", ".mp4", ".m4b", ".m4p"):
        return embed_art_in_m4a(file_path, image_bytes, mime_type)
    return False


def process_directories(root_dir: str, dry_run: bool = False) -> None:
    target_path = Path(root_dir).resolve()
    if not target_path.exists():
        print(f"Error: Target path '{root_dir}' does not exist.", file=sys.stderr)
        sys.exit(1)

    print(f"Scanning directory: {target_path}")
    print(f"Mode              : {'DRY RUN (no files modified)' if dry_run else 'LIVE (updating tags)'}")
    print("=" * 80)

    total_directories = 0
    updated_files_count = 0
    failed_files_count = 0
    target_extensions = {".mp3", ".m4a"}

    for current_dir, _, filenames in os.walk(target_path):
        dir_path = Path(current_dir)

        # Look for cover.jpg or cover.png
        cover_path = get_cover_file(dir_path, filenames)
        if not cover_path:
            continue

        # Look for audio files in the same folder
        audio_files = [
            dir_path / f
            for f in sorted(filenames)
            if Path(f).suffix.lower() in target_extensions
        ]

        if not audio_files:
            continue

        total_directories += 1

        try:
            image_bytes, mime_type, desc = prepare_cover_data(cover_path)
        except Exception as e:
            print(f"[ERROR] Could not process cover '{cover_path}': {e}", file=sys.stderr)
            continue

        print(f"[{total_directories}] Directory: {dir_path}")
        print(f"    Cover: {cover_path.name} -> {desc}")
        print(f"    Files to embed: {len(audio_files)}")

        for af in audio_files:
            if dry_run:
                print(f"      [DRY-RUN WOULD EMBED] {af.name}")
                updated_files_count += 1
            else:
                success = embed_artwork_in_file(af, image_bytes, mime_type)
                if success:
                    print(f"      [EMBEDDED] {af.name}")
                    updated_files_count += 1
                else:
                    print(f"      [FAILED] {af.name}", file=sys.stderr)
                    failed_files_count += 1
        print()

    print("=" * 80)
    print("SUMMARY")
    print(f"  Directories processed : {total_directories}")
    print(f"  Files embedded        : {updated_files_count}")
    if failed_files_count > 0:
        print(f"  Files failed          : {failed_files_count}")
    print("=" * 80)


def main():
    parser = argparse.ArgumentParser(
        description="Embed cover.jpg or cover.png into mp3/m4a files (directly if width <= 600, or resized to 600x600 in /tmp)."
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
        help="Simulate the run without modifying any audio files",
    )

    args = parser.parse_args()
    process_directories(args.directory, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
