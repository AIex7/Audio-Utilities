#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.9"
# dependencies = [
#     "mutagen>=1.47.0",
# ]
# ///
"""
Recursively scans a directory for .m4a files, checks whether each file is AAC
(via macOS `afinfo -b`, matching convert_alac_to_aac.py), and if the
`----:com.apple.iTunes:AAC_HASH` tag is missing, computes the SHA-1 hash of the
raw `mdat` audio packets and writes only that tag without modifying anything else.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import shutil
import struct
import subprocess
import sys
from pathlib import Path
from typing import Optional

from mutagen.mp4 import AtomDataType, MP4, MP4FreeForm

AAC_HASH_TAG = "----:com.apple.iTunes:AAC_HASH"


def get_audio_codec(file_path: Path) -> Optional[str]:
    """
    Inspect file data format using macOS `afinfo -b`.
    Parses strictly the 'format:' stream line to prevent false matches on filenames/paths.
    Returns codec name (e.g. 'alac', 'aac') or 'other' / None if undetected.
    """
    try:
        res = subprocess.run(
            ["afinfo", "-b", str(file_path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        if res.returncode != 0:
            return None

        for line in res.stdout.splitlines():
            line_lower = line.lower()
            if "format:" in line_lower:
                format_part = line_lower.split("format:", 1)[1]
                tokens = set(re.findall(r"\b[a-z0-9_-]+\b", format_part))
                if "alac" in tokens:
                    return "alac"
                if "aac" in tokens:
                    return "aac"
                return "other"

        return "other"
    except Exception:
        return None


def compute_audio_packets_hash(file_path: Path) -> Optional[str]:
    """
    Compute the SHA-1 hash of the raw audio packets (the mdat atom payload),
    excluding all MP4 container headers and metadata.
    """
    hasher = hashlib.sha1()
    found_audio = False
    try:
        with open(file_path, "rb") as f:
            while True:
                hdr = f.read(8)
                if len(hdr) < 8:
                    break
                size, name = struct.unpack(">I4s", hdr)
                if size == 1:
                    ext_hdr = f.read(8)
                    if len(ext_hdr) < 8:
                        break
                    size = struct.unpack(">Q", ext_hdr)[0]
                    data_len = size - 16
                elif size == 0:
                    data_len = -1
                else:
                    data_len = size - 8

                if name == b"mdat":
                    found_audio = True
                    if data_len == -1:
                        while True:
                            chunk = f.read(65536)
                            if not chunk:
                                break
                            hasher.update(chunk)
                    else:
                        remaining = data_len
                        while remaining > 0:
                            chunk = f.read(min(65536, remaining))
                            if not chunk:
                                break
                            hasher.update(chunk)
                            remaining -= len(chunk)
                else:
                    if data_len < 0:
                        break
                    if data_len > 0:
                        f.seek(data_len, 1)

        return hasher.hexdigest() if found_audio else None
    except Exception:
        return None


def process_directory(root_dir: str, dry_run: bool = False) -> int:
    target_path = Path(root_dir).resolve()
    if not target_path.exists():
        print(f"Error: Target path '{root_dir}' does not exist.", file=sys.stderr)
        return 1

    print(f"Scanning directory: {target_path}")
    print(f"Mode              : {'DRY RUN (no files modified)' if dry_run else 'LIVE (updating tags)'}")
    print("=" * 80)

    total_m4a = 0
    skipped_non_aac = 0
    already_tagged = 0
    updated = 0
    errors = 0

    for current_dir, dirs, files in os.walk(target_path):
        dirs[:] = sorted(d for d in dirs if not d.startswith("."))

        for filename in sorted(files):
            if filename.startswith("."):
                continue
            file_path = Path(current_dir) / filename
            if file_path.suffix.lower() != ".m4a":
                continue

            total_m4a += 1
            codec = get_audio_codec(file_path)
            if codec != "aac":
                skipped_non_aac += 1
                continue

            try:
                audio = MP4(str(file_path))
            except Exception as e:
                print(f"[ERROR] Failed reading tags for {file_path}: {e}", file=sys.stderr)
                errors += 1
                continue

            if audio.tags and audio.tags.get(AAC_HASH_TAG):
                already_tagged += 1
                continue

            audio_hash = compute_audio_packets_hash(file_path)
            if not audio_hash:
                print(f"[ERROR] Could not compute mdat SHA-1 hash for {file_path}", file=sys.stderr)
                errors += 1
                continue

            aac_hash_str = f"SHA1 Hash of mdat audio packets : {audio_hash}"
            if dry_run:
                print(f"[DRY RUN] Would add AAC_HASH to {file_path} ({audio_hash})")
                updated += 1
                continue

            try:
                if audio.tags is None:
                    audio.add_tags()
                audio.tags[AAC_HASH_TAG] = [
                    MP4FreeForm(aac_hash_str.encode("utf-8"), AtomDataType.UTF8)
                ]
                audio.save()
                print(f"[UPDATED] Added AAC_HASH to {file_path} ({audio_hash})")
                updated += 1
            except Exception as e:
                print(f"[ERROR] Failed saving AAC_HASH for {file_path}: {e}", file=sys.stderr)
                errors += 1

    print("=" * 80)
    print(
        f"Done. .m4a found: {total_m4a} | Updated: {updated} | "
        f"Already had AAC_HASH: {already_tagged} | Non-AAC skipped: {skipped_non_aac} | Errors: {errors}"
    )
    return 1 if errors > 0 else 0


def main() -> int:
    if not shutil.which("afinfo"):
        print("Error: Required macOS tool 'afinfo' was not found in PATH.", file=sys.stderr)
        return 1

    parser = argparse.ArgumentParser(
        description="Recursively add missing AAC_HASH tags to AAC .m4a files without modifying anything else."
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
        help="Preview changes without modifying any files",
    )

    args = parser.parse_args()
    return process_directory(args.directory, dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
