# /// script
# requires-python = ">=3.9"
# dependencies = [
#     "mutagen>=1.47.0",
#     "send2trash>=1.8.0",
# ]
# ///

import os
import sys
from typing import Optional, Tuple
from mutagen.mp3 import MP3
from mutagen.id3 import APIC
from mutagen.mp4 import MP4, MP4Cover
from send2trash import send2trash

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png"}
AUDIO_EXTENSIONS = {".mp3", ".m4a"}


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
        audio["covr"] = [MP4Cover(img_data, image_format=image_fmt)]
        audio.save()

    else:
        raise ValueError(f"Unsupported audio format: {audio_ext}")


def rename_image_to_cover(image_path: str) -> str:
    """Rename single image file to lowercase cover.jpg or cover.png."""
    dir_path, filename = os.path.split(image_path)
    img_ext = os.path.splitext(filename)[1].lower()

    target_name = "cover.png" if img_ext == ".png" else "cover.jpg"
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

        # Condition 1: If there is no image in the directory, scan audio files
        # and extract the first embedded image found to cover.jpg or cover.png
        if len(image_files) == 0 and len(audio_files) > 0:
            for audio_name in sorted(audio_files):
                audio_path = os.path.join(current_dir, audio_name)
                extracted = extract_artwork(audio_path)
                if extracted is not None:
                    img_data, img_type = extracted
                    target_name = f"cover.{img_type}"
                    target_path = os.path.join(current_dir, target_name)
                    try:
                        with open(target_path, "wb") as f:
                            f.write(img_data)
                        print(f"[EXTRACT] Extracted {target_name} from {audio_path}")
                    except Exception as e:
                        print(f"[ERROR] Failed writing {target_path}: {e}", file=sys.stderr)
                    break
            continue

        # Condition 2: If the directory contains a single image and any audio files
        if len(image_files) != 1 or len(audio_files) == 0:
            continue

        image_path = os.path.join(current_dir, image_files[0])

        if len(audio_files) == 1:
            audio_path = os.path.join(current_dir, audio_files[0])
            print(f"[EMBED] Embedding {image_path} into {audio_path}...")
            try:
                embed_artwork(audio_path, image_path)
                print(f"[TRASH] Trashing {image_path}...")
                send2trash(image_path)
            except Exception as e:
                print(f"[ERROR] Failed processing {audio_path}: {e}", file=sys.stderr)
        else:
            # More than one audio file: rename image to cover.jpg or cover.png
            try:
                new_path = rename_image_to_cover(image_path)
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
