# /// script
# requires-python = ">=3.9"
# dependencies = [
#     "mutagen",
# ]
# ///
"""
Recursively scans for directories containing cover.jpg, cover.png, or cover.jxl.
For each directory:
- Checks the width of the cover image.
- If width <= 600 and JPEG: embeds the original image directly.
- If PNG or JXL with width <= 600: converts to JPEG at original size in /tmp via sips and embeds that.
- If width > 600: resizes to 600x600 max as JPEG in /tmp via sips and embeds that.
- Embeds the artwork into every .mp3 and .m4a file in that directory.
- Ensures the final audio file contains ONLY that artwork (removes any previous artwork).
- Preserves all other audio metadata tags untouched.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

import mutagen
from mutagen.id3 import APIC, ID3
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4, MP4Cover


def get_image_info(path: Path) -> tuple[int, int, str]:
    """Return (width, height, format_upper) using macOS sips."""
    res = subprocess.run(
        ["sips", "-g", "pixelWidth", "-g", "pixelHeight", "-g", "format", str(path)],
        capture_output=True,
        text=True,
        check=True,
    )
    width = height = 0
    fmt = ""
    for line in res.stdout.splitlines():
        line = line.strip()
        if line.startswith("pixelWidth:"):
            width = int(line.split(":", 1)[1].strip())
        elif line.startswith("pixelHeight:"):
            height = int(line.split(":", 1)[1].strip())
        elif line.startswith("format:"):
            fmt = line.split(":", 1)[1].strip().upper()
    return width, height, fmt


def get_cover_file(dir_path: Path, filenames: list[str]) -> Path | None:
    """Find cover.jpg, cover.png, or cover.jxl in the directory (case-insensitive)."""
    file_map = {f.lower(): f for f in filenames}
    for candidate in ("cover.jpg", "cover.jpeg", "cover.png", "cover.jxl"):
        if candidate in file_map:
            return dir_path / file_map[candidate]
    return None


def prepare_cover_data(cover_path: Path, max_size: int = 600) -> tuple[bytes, str, str]:
    """
    Inspect cover image dimensions and format using sips (decoding .jxl via djxl first).
    Returns:
        (image_bytes, mime_type, description_string)
    """
    is_jxl_file = cover_path.suffix.lower() == ".jxl"
    file_hash = hashlib.md5(str(cover_path.resolve()).encode()).hexdigest()[:10]
    decoded_jxl_path: Path | None = None
    working_path = cover_path

    try:
        if is_jxl_file:
            if not shutil.which("djxl"):
                raise RuntimeError("djxl CLI is required to decode .jxl images but was not found in PATH")
            decoded_jxl_path = Path("/tmp") / f"jxl_decoded_{file_hash}.png"
            subprocess.run(
                ["djxl", str(cover_path), str(decoded_jxl_path)],
                capture_output=True,
                check=True,
            )
            working_path = decoded_jxl_path

        width, height, img_format = get_image_info(working_path)
        if is_jxl_file:
            img_format = "JXL"

        is_jpeg = img_format in ("JPEG", "JPG")

        # If width <= max_size and already JPEG, embed directly (PNG/JXL are always converted to JPEG)
        if width <= max_size and is_jpeg and not is_jxl_file:
            desc = f"Direct original ({width}x{height}, {img_format})"
            return cover_path.read_bytes(), "image/jpeg", desc

        # Convert/resize to JPEG in /tmp via sips
        tmp_path = Path("/tmp") / f"resized_{max_size}x{max_size}_{file_hash}.jpg"

        sips_cmd = ["sips"]
        if width > max_size:
            sips_cmd.extend(["-Z", str(max_size)])
        sips_cmd.extend(["-s", "format", "jpeg", "-s", "formatOptions", "normal", str(working_path), "--out", str(tmp_path)])
        subprocess.run(sips_cmd, check=True, stdout=subprocess.DEVNULL)

        new_w, new_h, _ = get_image_info(tmp_path)

        if width <= max_size:
            desc = f"Converted via sips in /tmp ({img_format} -> JPEG {new_w}x{new_h}, {tmp_path.name})"
        else:
            desc = f"Resized via sips in /tmp preserving aspect ratio ({img_format} {width}x{height} -> JPEG {new_w}x{new_h}, {tmp_path.name})"

        return tmp_path.read_bytes(), "image/jpeg", desc
    finally:
        if decoded_jxl_path is not None and decoded_jxl_path.exists():
            decoded_jxl_path.unlink(missing_ok=True)


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


def has_identical_single_cover(file_path: Path, image_bytes: bytes) -> bool:
    """Return True if file_path has exactly one embedded image matching image_bytes in size and SHA-256 hash."""
    ext = file_path.suffix.lower()
    target_size = len(image_bytes)
    target_hash = hashlib.sha256(image_bytes).digest()

    try:
        if ext == ".mp3":
            try:
                audio = MP3(file_path)
            except Exception:
                audio = mutagen.File(file_path)
            if audio is None or audio.tags is None:
                return False
            apic_frames = [
                v for k, v in audio.tags.items()
                if k.startswith("APIC") or isinstance(v, APIC)
            ]
            if len(apic_frames) != 1:
                return False
            existing_data = apic_frames[0].data
        elif ext in (".m4a", ".mp4", ".m4b", ".m4p"):
            try:
                audio = MP4(file_path)
            except Exception:
                audio = mutagen.File(file_path)
            if audio is None or audio.tags is None:
                return False
            covers = audio.tags.get("covr")
            if not covers or len(covers) != 1:
                return False
            existing_data = bytes(covers[0])
        else:
            return False

        return (
            len(existing_data) == target_size
            and hashlib.sha256(existing_data).digest() == target_hash
        )
    except Exception:
        return False


def embed_artwork_in_file(file_path: Path, image_bytes: bytes, mime_type: str) -> bool:
    ext = file_path.suffix.lower()
    if ext == ".mp3":
        return embed_art_in_mp3(file_path, image_bytes, mime_type)
    elif ext in (".m4a", ".mp4", ".m4b", ".m4p"):
        return embed_art_in_m4a(file_path, image_bytes, mime_type)
    return False


def process_directories(root_dir: str, dry_run: bool = False, max_size: int = 600) -> None:
    target_path = Path(root_dir).resolve()
    if not target_path.exists():
        print(f"Error: Target path '{root_dir}' does not exist.", file=sys.stderr)
        sys.exit(1)

    print(f"Scanning directory: {target_path}")
    print(f"Target max size   : {max_size}px")
    print(f"Mode              : {'DRY RUN (no files modified)' if dry_run else 'LIVE (updating tags)'}")
    print("=" * 80)

    total_directories = 0
    updated_files_count = 0
    skipped_files_count = 0
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
            image_bytes, mime_type, desc = prepare_cover_data(cover_path, max_size=max_size)
        except Exception as e:
            print(f"[ERROR] Could not process cover '{cover_path}': {e}", file=sys.stderr)
            continue

        print(f"[{total_directories}] Directory: {dir_path}")
        print(f"    Cover: {cover_path.name} -> {desc}")
        print(f"    Files to embed: {len(audio_files)}")

        for af in audio_files:
            if has_identical_single_cover(af, image_bytes):
                prefix = "[DRY-RUN WOULD SKIP]" if dry_run else "[SKIPPED]"
                print(f"      {prefix} {af.name} (identical single cover already embedded)")
                skipped_files_count += 1
                continue

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
    print(f"  Files skipped         : {skipped_files_count}")
    if failed_files_count > 0:
        print(f"  Files failed          : {failed_files_count}")
    print("=" * 80)


def main():
    parser = argparse.ArgumentParser(
        description="Embed cover.jpg, cover.png, or cover.jxl into mp3/m4a files as JPEG (resizing to target size in /tmp if wider)."
    )
    parser.add_argument(
        "directory",
        nargs="?",
        default=".",
        help="Root directory to recursively scan (default: current directory)",
    )
    parser.add_argument(
        "-s", "--size",
        type=int,
        default=600,
        help="Target maximum cover width/dimension in pixels (default: 600)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Simulate the run without modifying any audio files",
    )

    args = parser.parse_args()
    if args.size <= 0:
        parser.error("--size must be a positive integer")
    process_directories(args.directory, dry_run=args.dry_run, max_size=args.size)


if __name__ == "__main__":
    main()
