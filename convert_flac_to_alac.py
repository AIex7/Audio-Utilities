#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.9"
# dependencies = [
#     "mutagen>=1.47.0",
# ]
# ///
"""
Recursively inspect .flac files using `afinfo` and convert them losslessly to
ALAC (.m4a) at their native sample rate and bit depth using `afconvert`, copy
all metadata and artwork via `mutagen`, verify the embedded hash, and remove
the original .flac.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from mutagen.flac import FLAC
from mutagen.mp4 import MP4, MP4Cover

DATA_FORMAT_RE = re.compile(
    r"Data format:\s+\d+\s+ch,\s+([0-9]+(?:\.[0-9]+)?)\s+Hz,\s+([^\s,(]+)(.*)"
)
BIT_DEPTH_SOURCE_RE = re.compile(r"source bit depth:\s*[A-Za-z]+(\d+)", re.IGNORECASE)
BIT_DEPTH_FORMAT_RE = re.compile(r"(?:from\s+)?(\d+)-bit", re.IGNORECASE)

TEXT_TAG_MAP: tuple[tuple[tuple[str, ...], str], ...] = (
    (("title",), "\xa9nam"),
    (("artist",), "\xa9ART"),
    (("albumartist", "album artist"), "aART"),
    (("album",), "\xa9alb"),
    (("composer",), "\xa9wrt"),
    (("genre",), "\xa9gen"),
    (("date", "year"), "\xa9day"),
    (("comment", "description"), "\xa9cmt"),
    (("copyright",), "cprt"),
    (("grouping", "contentgroup"), "\xa9grp"),
    (("work",), "\xa9wrk"),
    (("movementname",), "\xa9mvn"),
    (("lyrics", "unsyncedlyrics"), "\xa9lyr"),
)
JOINED_ARTIST_MP4_KEYS = {"\xa9ART", "aART"}


def _format_apple_music_artists(values: list[str]) -> str:
    """Join multiple artist values using Apple Music convention ('A & B', 'A, B & C')."""
    unique = list(dict.fromkeys(values))
    if len(unique) == 1:
        return unique[0]
    return f"{', '.join(unique[:-1])} & {unique[-1]}"


def _parse_int_part(raw: object, part_index: int = 0) -> int:
    """Parse a positive integer from a tag string like '3' or '3/12', returning 0 if missing."""
    parts = str(raw).split("/", 1)
    if part_index >= len(parts):
        return 0
    text = parts[part_index].strip()
    if not text:
        return 0
    try:
        value = int(text)
    except ValueError:
        return 0
    return value if value > 0 else 0


def _read_flac_pair(flac_tags: FLAC, number_key: str, total_keys: tuple[str, ...]) -> tuple[int, int]:
    """Read (number, total) from FLAC Vorbis comments."""
    num = 0
    total = 0
    raw_num = flac_tags.get(number_key)
    if raw_num:
        num = _parse_int_part(raw_num[0], 0)
        total = _parse_int_part(raw_num[0], 1)

    if total == 0:
        for key in total_keys:
            raw_total = flac_tags.get(key)
            if raw_total:
                total = _parse_int_part(raw_total[0], 0)
                if total > 0:
                    break

    return num, total


def copy_flac_tags_to_m4a(
    flac_path: Path,
    m4a_path: Path,
    encoded_by: str | None = None,
) -> None:
    """Copy Vorbis comments and embedded pictures from flac_path into m4a_path via mutagen."""
    flac = FLAC(flac_path)
    m4a = MP4(m4a_path)
    if m4a.tags is None:
        m4a.add_tags()

    if encoded_by:
        m4a.tags["\xa9too"] = [encoded_by]

    for flac_keys, mp4_key in TEXT_TAG_MAP:
        for key in flac_keys:
            values = flac.get(key)
            cleaned = [str(v).strip() for v in values if str(v).strip()] if values else []
            if cleaned:
                if mp4_key in JOINED_ARTIST_MP4_KEYS:
                    m4a.tags[mp4_key] = [_format_apple_music_artists(cleaned)]
                else:
                    m4a.tags[mp4_key] = cleaned
                break

    track_num, total_tracks = _read_flac_pair(flac, "tracknumber", ("tracktotal", "totaltracks"))
    if track_num > 0 or total_tracks > 0:
        m4a.tags["trkn"] = [(track_num, total_tracks)]

    disc_num, total_discs = _read_flac_pair(flac, "discnumber", ("disctotal", "totaldiscs"))
    if disc_num > 0 or total_discs > 0:
        m4a.tags["disk"] = [(disc_num, total_discs)]

    movement_num, movement_total = _read_flac_pair(
        flac, "movement", ("movementtotal",)
    )
    if movement_num == 0:
        movement_num, _ = _read_flac_pair(flac, "movementnumber", ())
    if movement_num > 0:
        m4a.tags["\xa9mvi"] = [movement_num]
    if movement_total > 0:
        m4a.tags["\xa9mvc"] = [movement_total]

    cpil_vals = flac.get("compilation")
    if cpil_vals:
        raw_cpil = str(cpil_vals[0]).strip().lower()
        if raw_cpil in {"1", "true", "yes"}:
            m4a.tags["cpil"] = True
        elif raw_cpil in {"0", "false", "no"}:
            m4a.tags["cpil"] = False

    for bpm_key in ("bpm", "tempo"):
        bpm_vals = flac.get(bpm_key)
        if bpm_vals:
            try:
                bpm_int = int(round(float(str(bpm_vals[0]).strip())))
            except ValueError:
                bpm_int = 0
            if bpm_int > 0:
                m4a.tags["tmpo"] = [bpm_int]
                break

    if flac.pictures:
        covers: list[MP4Cover] = []
        sorted_pics = sorted(flac.pictures, key=lambda pic: 0 if pic.type == 3 else 1)
        for pic in sorted_pics:
            mime = pic.mime.lower()
            if mime in {"image/jpeg", "image/jpg"}:
                covers.append(MP4Cover(pic.data, imageformat=MP4Cover.FORMAT_JPEG))
            elif mime == "image/png":
                covers.append(MP4Cover(pic.data, imageformat=MP4Cover.FORMAT_PNG))
        if covers:
            m4a.tags["covr"] = covers

    m4a.save()


def inspect_flac(afinfo: str, audio_path: Path) -> tuple[str, float, int] | None:
    """
    Run `afinfo` on audio_path and return (codec, sample_rate_hz, bit_depth),
    or None if any required field cannot be determined with certainty.
    """
    result = subprocess.run(
        [afinfo, str(audio_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return None

    stdout = result.stdout
    fmt_match = DATA_FORMAT_RE.search(stdout)
    if not fmt_match:
        return None

    sample_rate = float(fmt_match.group(1))
    codec = fmt_match.group(2).lower()
    format_rest = fmt_match.group(3)

    depth_match = BIT_DEPTH_SOURCE_RE.search(stdout) or BIT_DEPTH_FORMAT_RE.search(format_rest)
    if not depth_match:
        return None

    bit_depth = int(depth_match.group(1))
    return codec, sample_rate, bit_depth


ALAC_BIT_DEPTH_FLAGS = {
    16: 1,
    20: 2,
    24: 3,
    32: 4,
}


def alac_format_flag(bit_depth: int) -> tuple[int, int]:
    """Return (target_bit_depth, CoreAudio_alac_format_flag) preserving lossless precision."""
    if bit_depth <= 16:
        return 16, 1
    if bit_depth in ALAC_BIT_DEPTH_FLAGS:
        return bit_depth, ALAC_BIT_DEPTH_FLAGS[bit_depth]
    if bit_depth < 24:
        return 24, 3
    return 32, 4


def convert_file(
    afinfo: str,
    afconvert: str,
    audio_path: Path,
    root: Path,
    dry_run: bool = False,
) -> str:
    """Inspect a single .flac file and convert losslessly to ALAC (.m4a)."""
    rel_path = audio_path.relative_to(root)
    destination = audio_path.with_suffix(".m4a")
    if destination.exists():
        print(f"[SKIP] Output already exists: {destination.relative_to(root)}")
        return "skipped"

    info = inspect_flac(afinfo, audio_path)
    if info is None:
        print(f"[SKIP] Could not determine codec/rate/bit depth: {rel_path}")
        return "skipped"

    codec, sample_rate, bit_depth = info
    if codec != "flac":
        print(f"[SKIP] Not FLAC ({codec}): {rel_path}")
        return "skipped"

    rate_display = int(sample_rate) if sample_rate.is_integer() else sample_rate
    target_depth, depth_flag = alac_format_flag(bit_depth)
    data_format = f"alac@{rate_display}/{depth_flag}"

    if dry_run:
        print(
            f"[DRY RUN] Would convert {rel_path} -> {destination.relative_to(root)}: "
            f"{bit_depth}-bit/{rate_display} Hz -> {target_depth}-bit/{rate_display} Hz"
        )
        return "converted"

    print(
        f"[CONVERT] {rel_path} -> {destination.relative_to(root)}: "
        f"{bit_depth}-bit/{rate_display} Hz -> {target_depth}-bit/{rate_display} Hz"
    )

    with tempfile.TemporaryDirectory(dir=audio_path.parent) as temp_dir:
        temp_out = Path(temp_dir) / destination.name
        cmd = [
            afconvert,
            "-f", "m4af",
            "-d", data_format,
            "--soundcheck-generate",
            "--media-kind", "Music",
            str(audio_path),
            str(temp_out),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if result.returncode != 0 or not temp_out.exists() or temp_out.stat().st_size == 0:
            err = result.stderr.strip() or f"afconvert exited with {result.returncode}"
            print(f"[ERROR] Failed converting {rel_path}: {err}", file=sys.stderr)
            return "error"

        encoded_by_parts = [
            "afconvert",
            "-f", "m4af",
            "-d", data_format,
            "--soundcheck-generate",
            "--media-kind", "Music",
        ]
        copy_flac_tags_to_m4a(
            audio_path,
            temp_out,
            encoded_by=" ".join(encoded_by_parts),
        )

        os.replace(temp_out, destination)

    audio_path.unlink()
    return "converted"


def main() -> int:
    afinfo = shutil.which("afinfo")
    afconvert = shutil.which("afconvert")
    missing = [
        name
        for name, path in (("afinfo", afinfo), ("afconvert", afconvert))
        if not path
    ]
    if missing:
        print(f"Error: Missing required macOS tool(s) in PATH: {', '.join(missing)}", file=sys.stderr)
        return 1

    parser = argparse.ArgumentParser(
        description="Recursively convert .flac files losslessly to ALAC (.m4a) using afinfo/afconvert."
    )
    parser.add_argument(
        "directory",
        nargs="?",
        default=".",
        help="Root directory to recursively scan for .flac files (default: current directory)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview files that would be converted without modifying anything",
    )
    args = parser.parse_args()

    root = Path(args.directory).resolve()
    if not root.is_dir():
        print(f"Error: Directory does not exist: {args.directory}", file=sys.stderr)
        return 1

    flac_files: list[Path] = []
    for current_dir, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if not d.startswith("."))
        dir_path = Path(current_dir)
        for name in sorted(files):
            if name.startswith("."):
                continue
            path = dir_path / name
            if path.suffix.lower() == ".flac":
                flac_files.append(path)

    if not flac_files:
        print("No .flac files found.")
        return 0

    converted = 0
    skipped = 0
    errors = 0

    for audio_path in flac_files:
        status = convert_file(afinfo, afconvert, audio_path, root, dry_run=args.dry_run)
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
