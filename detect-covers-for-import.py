# /// script
# requires-python = ">=3.9"
# dependencies = [
#     "mutagen>=1.47.0",
# ]
# ///

import os
import subprocess
import sys
import tempfile
from typing import Optional, Tuple
from mutagen.mp3 import MP3
from mutagen.id3 import APIC
from mutagen.mp4 import MP4, MP4Cover

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".jxl"}
AUDIO_EXTENSIONS = {".mp3", ".m4a"}
PREFERRED_COVER_NAMES = ("cover", "folder", "front")
MAX_EMBED_IMAGE_BYTES = 500 * 1024


def _image_sort_key(filename: str) -> Tuple[int, str, str]:
    """Sort key prioritizing cover.*, folder.*, front.*, then alphabetical."""
    stem = os.path.splitext(filename)[0].lower()
    try:
        priority = PREFERRED_COVER_NAMES.index(stem)
    except ValueError:
        priority = len(PREFERRED_COVER_NAMES)
    return (priority, filename.lower(), filename)


def extract_artwork(audio_path: str) -> Optional[Tuple[bytes, str]]:
    """Extract embedded artwork from an audio file.

    Returns a tuple of (image_bytes, 'png' | 'jpg') if found, or None.
    """
    ext = os.path.splitext(audio_path)[1].lower()
    try:
        if ext == ".mp3":
            audio = MP3(audio_path)
            if audio.tags:
                for tag in audio.tags.values():
                    if isinstance(tag, APIC):
                        data = tag.data
                        mime = getattr(tag, "mime", "").lower()
                        if data.startswith(b"\x89PNG") or "png" in mime:
                            return data, "png"
                        return data, "jpg"

        elif ext == ".m4a":
            audio = MP4(audio_path)
            if audio.tags and "covr" in audio.tags:
                covers = audio.tags["covr"]
                if covers:
                    cover = covers[0]
                    data = bytes(cover)
                    fmt = getattr(cover, "imageformat", None)
                    if data.startswith(b"\x89PNG") or fmt == MP4Cover.FORMAT_PNG:
                        return data, "png"
                    return data, "jpg"

    except Exception as e:
        print(f"[WARN] Could not read artwork from {audio_path}: {e}", file=sys.stderr)

    return None


def embed_artwork(audio_path: str, image_path: str) -> None:
    """Clear existing artwork and embed image from image_path into audio_path."""
    audio_ext = os.path.splitext(audio_path)[1].lower()
    img_ext = os.path.splitext(image_path)[1].lower()

    with open(image_path, "rb") as f:
        img_data = f.read()

    if audio_ext == ".mp3":
        audio = MP3(audio_path)
        if audio.tags is None:
            audio.add_tags()
        # Remove all existing APIC (artwork) frames
        audio.tags.delall("APIC")
        mime = "image/png" if img_ext == ".png" else "image/jpeg"
        audio.tags.add(
            APIC(
                encoding=3,  # UTF-8
                mime=mime,
                type=3,      # Front cover
                desc="Cover",
                data=img_data,
            )
        )
        audio.save()

    elif audio_ext == ".m4a":
        audio = MP4(audio_path)
        if audio.tags is None:
            audio.add_tags()
        image_fmt = MP4Cover.FORMAT_PNG if img_ext == ".png" else MP4Cover.FORMAT_JPEG
        # Setting 'covr' directly replaces any existing artwork
        audio["covr"] = [MP4Cover(img_data, imageformat=image_fmt)]
        audio.save()

    else:
        raise ValueError(f"Unsupported audio format: {audio_ext}")


def convert_to_normal_jpg(image_path: str) -> str:
    """Convert an image to a normal-quality JPEG in a temporary file and return its path."""
    fd, temp_jpg_path = tempfile.mkstemp(suffix=".jpg")
    os.close(fd)
    try:
        subprocess.run(
            [
                "sips",
                "-s",
                "format",
                "jpeg",
                "-s",
                "formatOptions",
                "normal",
                image_path,
                "--out",
                temp_jpg_path,
            ],
            check=True,
            stdout=subprocess.DEVNULL,
        )
    except Exception:
        if os.path.exists(temp_jpg_path):
            os.remove(temp_jpg_path)
        raise
    return temp_jpg_path


def rename_image_to_cover(image_path: str) -> str:
    """Rename single image file to lowercase cover.jpg, cover.png, or cover.jxl."""
    dir_path, filename = os.path.split(image_path)
    img_ext = os.path.splitext(filename)[1].lower()

    ext_to_target = {
        ".png": "cover.png",
        ".jxl": "cover.jxl",
    }
    target_name = ext_to_target.get(img_ext, "cover.jpg")
    target_path = os.path.join(dir_path, target_name)

    # If the file is already named exactly target_name, no-op
    if filename == target_name:
        return target_path

    # Safely handle case-only renames on case-insensitive filesystems (e.g. APFS, NTFS)
    if os.path.exists(target_path) and os.path.samefile(image_path, target_path):
        temp_path = os.path.join(dir_path, f".__tmp_{target_name}")
        os.rename(image_path, temp_path)
        os.rename(temp_path, target_path)
    else:
        os.rename(image_path, target_path)

    return target_path


def process_directories(root_dir: str) -> None:
    """Recursively process directories for image and audio files."""
    for current_dir, dirs, files in os.walk(root_dir):
        # Skip hidden directories
        dirs[:] = [d for d in dirs if not d.startswith(".")]

        image_files = []
        audio_files = []

        for file_name in files:
            if file_name.startswith("."):
                continue
            ext = os.path.splitext(file_name)[1].lower()
            if ext in IMAGE_EXTENSIONS:
                image_files.append(file_name)
            elif ext in AUDIO_EXTENSIONS:
                audio_files.append(file_name)

        image_files.sort(key=_image_sort_key)

        # If there are multiple images in the directory, trash all except the first
        if len(image_files) > 1:
            kept = [image_files[0]]
            for extra_name in image_files[1:]:
                extra_path = os.path.join(current_dir, extra_name)
                try:
                    print(f"[TRASH] Trashing extra image {extra_path}...")
                    subprocess.run(["trash", extra_path], check=True)
                except Exception as e:
                    print(f"[ERROR] Failed trashing {extra_path}: {e}", file=sys.stderr)
                    kept.append(extra_name)
            image_files = kept

        # Condition 1: If there is no image in the directory and multiple audio files,
        # extract cover.jpg or cover.png only if every audio file has the same embedded artwork
        if len(image_files) == 0 and len(audio_files) > 1:
            first_artwork: Optional[Tuple[bytes, str]] = None
            first_audio_path = ""
            all_same = True

            for audio_name in sorted(audio_files):
                audio_path = os.path.join(current_dir, audio_name)
                extracted = extract_artwork(audio_path)
                if extracted is None:
                    all_same = False
                    break
                if first_artwork is None:
                    first_artwork = extracted
                    first_audio_path = audio_path
                elif extracted[0] != first_artwork[0]:
                    all_same = False
                    break

            if all_same and first_artwork is not None:
                img_data, img_type = first_artwork
                target_name = f"cover.{img_type}"
                target_path = os.path.join(current_dir, target_name)
                try:
                    with open(target_path, "wb") as f:
                        f.write(img_data)
                    print(f"[EXTRACT] Extracted {target_name} from {first_audio_path}")
                except Exception as e:
                    print(f"[ERROR] Failed writing {target_path}: {e}", file=sys.stderr)
            continue

        # Condition 2: If the directory contains a single image and multiple audio files
        if len(image_files) != 1 or len(audio_files) <= 1:
            continue

        image_path = os.path.join(current_dir, image_files[0])

        # More than one audio file: rename image to cover.jpg, cover.png, or cover.jxl
        try:
            new_path = rename_image_to_cover(image_path)
            if new_path != image_path:
                print(f"[RENAME] {image_path} -> {new_path}")
        except Exception as e:
            print(f"[ERROR] Failed renaming {image_path}: {e}", file=sys.stderr)


def main():
    target_dir = sys.argv[1] if len(sys.argv) > 1 else "."
    abs_target_dir = os.path.abspath(target_dir)
    print(f"Scanning directory: {abs_target_dir}")
    process_directories(abs_target_dir)
    print("Done.")


if __name__ == "__main__":
    main()
