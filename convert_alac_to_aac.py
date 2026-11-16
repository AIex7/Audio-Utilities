#!/usr/bin/env python3
# /// script
# requires-python = ">=3.9"
# dependencies = [
#     "mutagen>=1.47.0",
# ]
# ///
"""
Convert ALAC (Apple Lossless) M4A files to AAC at 300 kbps VBR using Apple's afconvert.

Key features:
- Uses Apple afconvert with highest quality settings:
    -d aac -f m4af -s 2 -b 300000 -q 127 -r 127 --src-complexity bats --soundcheck-generate
- Copies all MP4/M4A metadata tags and embedded cover artwork using mutagen.
- Computes and embeds an SHA-1 integrity hash of the raw encoded AAC audio packets (AAC_HASH).
- Runs afhash on the source ALAC file and embeds the audio hash line in ALAC_HASH.
- Sets the Encoded By / Encoding Tool tag (©too) to the afconvert command and flags used.
- Supports gapless album encoding (--gapless-before / --gapless-after) when all .m4a files in a directory share the same album tag.
- Protects stream-specific metadata: iTunNORM (SoundCheck), iTunSMPB (gapless), ©too, AAC_HASH, and ALAC_HASH.
- Detects ALAC streams via afinfo so non-ALAC / already AAC files are skipped.
- Safe temp file handling: encodes into a hidden temp folder on the same directory/mount.
- Atomic replacement on success: original file is only replaced once encoding succeeds with no errors.
- Graceful interrupt handling (Ctrl+C / SIGTERM): kills afconvert and cleans up temp files completely.
- Run directly in one line with uv: `uv run convert_alac_to_aac.py`
"""

import argparse
import hashlib
import os
from pathlib import Path
import re
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
from typing import Dict, List, Optional, Tuple

try:
    from mutagen.mp4 import AtomDataType, MP4, MP4FreeForm
    HAS_MUTAGEN = True
except ImportError:
    HAS_MUTAGEN = False

# Global reference to track active subprocess and temp dir for signal cleanup
_current_process: Optional[subprocess.Popen] = None
_current_temp_dir: Optional[str] = None
_interrupted: bool = False


def _signal_handler(signum, frame):
    """Handle interrupt signals (Ctrl+C, SIGTERM) gracefully."""
    global _interrupted, _current_process, _current_temp_dir
    _interrupted = True
    print("\n\n[!] Interrupt received (Ctrl+C). Aborting safely...", file=sys.stderr)

    # Terminate running afconvert process if any
    if _current_process is not None and _current_process.poll() is None:
        try:
            _current_process.terminate()
            _current_process.wait(timeout=2.0)
        except Exception:
            try:
                _current_process.kill()
            except Exception:
                pass

    # Clean up current temporary directory
    if _current_temp_dir and os.path.exists(_current_temp_dir):
        try:
            shutil.rmtree(_current_temp_dir, ignore_errors=True)
        except Exception:
            pass

    sys.exit(130)


def check_tool_installed(tool_name: str) -> bool:
    """Verify that a CLI tool is available in the system PATH."""
    return shutil.which(tool_name) is not None


def format_size(bytes_val: int) -> str:
    """Format bytes into human-readable string (KB, MB, etc.)."""
    for unit in ["B", "KB", "MB", "GB"]:
        if abs(bytes_val) < 1024.0:
            return f"{bytes_val:3.1f} {unit}"
        bytes_val /= 1024.0
    return f"{bytes_val:.1f} TB"


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

        # Isolate and inspect ONLY the 'format:' descriptor line from afinfo
        for line in res.stdout.splitlines():
            line_lower = line.lower()
            if "format:" in line_lower:
                format_part = line_lower.split("format:", 1)[1]
                # Match standalone word tokens in the audio format definition
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
                    size = struct.unpack(">Q", f.read(8))[0]
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
                    # Non-mdat atom extends to EOF (size == 0) or is malformed (< 8)
                    if data_len < 0:
                        break
                    if data_len > 0:
                        f.seek(data_len, 1)

        return hasher.hexdigest() if found_audio else None
    except Exception:
        return None


def get_source_afhash(file_path: Path) -> Optional[str]:
    """
    Run afhash on the source ALAC file and return the hash line, e.g.:
    'MD5 Hash of audio data : 3206c181358afc7edccb2e770ac0abc0' or
    'Hash of audio data : <sha1>'.
    """
    try:
        result = subprocess.run(
            ["afhash", str(file_path)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            return None
        for line in result.stdout.splitlines():
            if "Hash of audio data" in line:
                return line.strip()
        return None
    except Exception:
        return None


def copy_tags(
    src_path: Path,
    dst_path: Path,
    encoded_by: Optional[str] = None,
) -> Tuple[bool, Optional[str]]:
    """
    Copy all MP4/M4A metadata tags and embedded cover artwork
    from src_path to dst_path using mutagen, while protecting
    stream-specific timing, loudness, encoder, and audio integrity tags:
    - iTunNORM (SoundCheck volume normalization)
    - iTunSMPB (Gapless playback priming and remainder padding)
    - ©too (Encoded By / Encoding Tool and afconvert flags)
    - AAC_HASH (SHA-1 hash of the newly encoded AAC mdat audio packets)
    - ALAC_HASH (afhash output of the source ALAC audio data)

    Returns:
        (success: bool, error_message: Optional[str])
    """
    if not HAS_MUTAGEN:
        return False, "mutagen library is not installed"
    try:
        src = MP4(str(src_path))
        dst = MP4(str(dst_path))
        if dst.tags is None:
            dst.add_tags()

        # Compute SHA-1 hash of the newly encoded raw AAC audio packets (mdat payload)
        audio_hash = compute_audio_packets_hash(dst_path)
        # Run afhash on the source ALAC file
        alac_hash = get_source_afhash(src_path)

        # Protected stream-specific tags
        protected_keys = {
            "----:com.apple.iTunes:iTunNORM",
            "----:com.apple.iTunes:iTunSMPB",
            "\xa9too",
            "----:com.apple.iTunes:AAC_HASH",
            "----:com.apple.iTunes:ALAC_HASH",
        }
        # Snapshot any existing destination values created by afconvert
        preserved_tags = {k: dst.tags[k] for k in protected_keys if k in dst.tags}

        # Record afconvert command and flags in the Encoded By / Encoding Tool tag
        if encoded_by:
            preserved_tags["\xa9too"] = [encoded_by]

        # Store AAC mdat SHA-1 hash in AAC_HASH as UTF-8
        if audio_hash:
            aac_hash_str = f"SHA1 Hash of mdat audio packets : {audio_hash}"
            preserved_tags["----:com.apple.iTunes:AAC_HASH"] = [
                MP4FreeForm(aac_hash_str.encode("utf-8"), AtomDataType.UTF8)
            ]

        # Store source ALAC afhash output in ALAC_HASH as UTF-8
        if alac_hash:
            preserved_tags["----:com.apple.iTunes:ALAC_HASH"] = [
                MP4FreeForm(alac_hash.encode("utf-8"), AtomDataType.UTF8)
            ]

        # Copy all source tags except the protected ones
        if src.tags:
            for key, val in src.tags.items():
                if key in protected_keys:
                    continue
                dst.tags[key] = val

        # Ensure protected tags are retained on destination
        for key, val in preserved_tags.items():
            dst.tags[key] = val

        dst.save()

        # Verify that tags were written successfully
        verify_dst = MP4(str(dst_path))
        if not verify_dst.tags:
            return False, "tags missing after writing to output"

        return True, None
    except Exception as exc:
        return False, str(exc)


def convert_alac_to_aac(
    file_path: Path,
    bitrate: int = 300000,
    force: bool = False,
    dry_run: bool = False,
    copy_metadata: bool = True,
    gapless_before: Optional[Path] = None,
    gapless_after: Optional[Path] = None,
    staged_out: Optional[Path] = None,
) -> Tuple[bool, str]:
    """
    Convert a single M4A file from ALAC to AAC VBR.

    Parameters:
        file_path: Path to target file.
        bitrate: Bitrate in bps (e.g. 300000 for 300 kbps).
        force: If True, convert even if codec doesn't explicitly detect as ALAC.
        dry_run: If True, only simulate conversion.
        copy_metadata: If True, copy tags and artwork using mutagen.
        gapless_before: Optional previous track in a gapless album.
        gapless_after: Optional next track in a gapless album.
        staged_out: Optional pre-allocated temp output path; when provided,
            the caller handles atomic os.replace() and temp cleanup.

    Returns:
        (success: bool, message: str)
    """
    global _current_process, _current_temp_dir

    if not file_path.is_file():
        return False, f"File not found: {file_path}"

    codec = get_audio_codec(file_path)
    if not force:
        if codec == "aac":
            return False, "Skipped (already AAC)"
        elif codec != "alac":
            return False, f"Skipped (format is '{codec}', not ALAC; use --force to override)"

    if dry_run:
        gapless_note = " (gapless)" if (gapless_before or gapless_after) else ""
        return True, f"Dry-run: Would convert to AAC {bitrate // 1000} kbps VBR{gapless_note}"

    parent_dir = file_path.parent
    orig_size = file_path.stat().st_size

    # Create a temporary directory inside the same parent directory unless staged_out is provided.
    # This guarantees that os.replace() at the end is an atomic same-filesystem operation.
    own_temp_dir: Optional[str] = None
    if staged_out is not None:
        temp_out = staged_out
    else:
        own_temp_dir = tempfile.mkdtemp(prefix=".afconvert_tmp_", dir=parent_dir)
        _current_temp_dir = own_temp_dir
        temp_out = Path(own_temp_dir) / file_path.name

    # afconvert options for maximum quality:
    # -d aac              : AAC audio format
    # -f m4af             : MPEG-4 Audio (.m4a) container
    # -s 2                : VBR_constrained (targets the specified bitrate using VBR)
    # -b <bitrate>        : Target bitrate (e.g., 300000 bps = 300 kbps)
    # -q 127              : Highest codec encoding quality (0-127)
    # -r 127              : Highest sample rate converter quality (0-127)
    # --src-complexity bats: Highest sample rate converter complexity (bats = best bandlimited sinc)
    # --soundcheck-generate: Calculate Apple SoundCheck volume normalization metadata
    base_cmd = [
        "afconvert",
        "-d", "aac",
        "-f", "m4af",
        "-s", "2",
        "-b", str(bitrate),
        "-q", "127",
        "-r", "127",
        "--src-complexity", "bats",
        "--soundcheck-generate",
    ]
    cmd = list(base_cmd)
    if gapless_before is not None:
        cmd.extend(["--gapless-before", str(gapless_before)])
    if gapless_after is not None:
        cmd.extend(["--gapless-after", str(gapless_after)])
    cmd.extend([str(file_path), str(temp_out)])

    try:
        _current_process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        stdout, stderr = _current_process.communicate()

        if _interrupted:
            return False, "Aborted by user"

        if _current_process.returncode != 0:
            err_msg = stderr.strip() or stdout.strip() or f"exit code {_current_process.returncode}"
            return False, f"afconvert failed: {err_msg}"

        # Verify output file exists and is non-empty
        if not temp_out.is_file() or temp_out.stat().st_size == 0:
            return False, "Conversion failed: output temp file is missing or empty"

        # Copy original tags and artwork into the temporary file before replacement.
        # If tag copying fails, abort and delete the temp file so the original ALAC file is safe.
        if copy_metadata:
            encoded_by_parts = list(base_cmd)
            if gapless_before is not None:
                encoded_by_parts.append("--gapless-before")
            if gapless_after is not None:
                encoded_by_parts.append("--gapless-after")
            encoded_by = " ".join(encoded_by_parts)
            tag_ok, tag_err = copy_tags(file_path, temp_out, encoded_by=encoded_by)
            if not tag_ok:
                return False, f"Tag copy failed ({tag_err}); aborted to preserve original file"

        # Verify that the final converted file (including tags & artwork) is smaller than original
        final_size = temp_out.stat().st_size
        if final_size >= orig_size:
            return False, f"Output ({format_size(final_size)}) is not smaller than original ({format_size(orig_size)}); aborted to preserve original"

        saved_bytes = orig_size - final_size
        saved_pct = (saved_bytes / orig_size) * 100 if orig_size > 0 else 0

        # Atomic replacement unless caller staged the directory for gapless batch replacement
        if staged_out is None:
            os.replace(temp_out, file_path)

        return True, f"{format_size(orig_size)} -> {format_size(final_size)} ({saved_pct:.1f}% reduction)"

    except KeyboardInterrupt:
        raise
    except Exception as exc:
        return False, f"Unexpected error: {exc}"
    finally:
        _current_process = None
        # Always clean up our own temporary directory if we created one
        if own_temp_dir and os.path.exists(own_temp_dir):
            try:
                shutil.rmtree(own_temp_dir, ignore_errors=True)
            except Exception:
                pass
            if _current_temp_dir == own_temp_dir:
                _current_temp_dir = None


def get_track_metadata(file_path: Path) -> Tuple[Optional[str], int, int]:
    """Return (album, disc_number, track_number) from MP4 tags."""
    if not HAS_MUTAGEN:
        return None, 0, 0
    try:
        audio = MP4(str(file_path))
        tags = audio.tags or {}
        album_vals = tags.get("\xa9alb")
        album = str(album_vals[0]).strip() if album_vals and album_vals[0] else None
        disc_vals = tags.get("disk")
        disc = int(disc_vals[0][0]) if disc_vals and disc_vals[0] else 0
        track_vals = tags.get("trkn")
        track = int(track_vals[0][0]) if track_vals and track_vals[0] else 0
        return album or None, disc, track
    except Exception:
        return None, 0, 0


def build_gapless_plan(
    dir_files: List[Path],
) -> Tuple[List[Path], Dict[Path, Tuple[Optional[Path], Optional[Path]]]]:
    """
    For a directory of .m4a files, verify that every file shares the exact same
    non-empty album tag. If so, return the tracks sorted by (disc, track, name)
    and a mapping of file -> (gapless_before, gapless_after) within each disc.
    Otherwise, return (dir_files, {}).
    """
    if len(dir_files) <= 1 or not HAS_MUTAGEN:
        return dir_files, {}

    meta = {p: get_track_metadata(p) for p in dir_files}
    albums = {meta[p][0] for p in dir_files}
    if None in albums or len(albums) != 1:
        return dir_files, {}

    sorted_files = sorted(dir_files, key=lambda p: (meta[p][1], meta[p][2], p.name))
    neighbors: Dict[Path, Tuple[Optional[Path], Optional[Path]]] = {}
    for i, p in enumerate(sorted_files):
        disc = meta[p][1]
        before = sorted_files[i - 1] if i > 0 and meta[sorted_files[i - 1]][1] == disc else None
        after = (
            sorted_files[i + 1]
            if i + 1 < len(sorted_files) and meta[sorted_files[i + 1]][1] == disc
            else None
        )
        neighbors[p] = (before, after)
    return sorted_files, neighbors


def find_m4a_files(targets: List[Path], recursive: bool = False) -> List[Path]:
    """Find all .m4a files in target paths or directories."""
    found: List[Path] = []
    for target in targets:
        if target.is_file():
            if target.suffix.lower() == ".m4a":
                found.append(target)
        elif target.is_dir():
            pattern = "**/*.m4a" if recursive else "*.m4a"
            found.extend(p for p in target.glob(pattern) if p.is_file())
    # Sort files naturally
    return sorted(found)


def main():
    # Register interrupt signals
    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    parser = argparse.ArgumentParser(
        description="Convert ALAC M4A audio files to AAC at 300 kbps VBR using Apple afconvert with best quality settings."
    )
    parser.add_argument(
        "paths",
        nargs="*",
        default=["."],
        help="Files or directories to process (default: current working directory)",
    )
    parser.add_argument(
        "-r", "--recursive",
        action="store_true",
        help="Search directories recursively for .m4a files",
    )
    parser.add_argument(
        "-b", "--bitrate",
        type=int,
        default=300000,
        help="Target bitrate in bits per second (default: 300000 for 300 kbps)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Convert all specified .m4a files even if afinfo doesn't detect 'alac'",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Simulate conversions without modifying files",
    )
    parser.add_argument(
        "--no-tags",
        action="store_true",
        help="Skip copying metadata tags and artwork from original file",
    )

    args = parser.parse_args()

    # Check for required macOS utilities
    for tool in ["afconvert", "afinfo", "afhash"]:
        if not check_tool_installed(tool):
            print(f"Error: Required tool '{tool}' not found in PATH. This script requires macOS.", file=sys.stderr)
            sys.exit(1)

    if not HAS_MUTAGEN and not args.no_tags:
        print(
            "Error: 'mutagen' is required to preserve audio tags.\n"
            "Run this script directly with uv (which manages dependencies automatically in one line):\n"
            "    uv run convert_alac_to_aac.py\n"
            "Or install mutagen with pip:\n"
            "    pip install mutagen\n"
            "Or pass --no-tags to convert without preserving tags.",
            file=sys.stderr,
        )
        sys.exit(1)

    target_paths = [Path(p) for p in args.paths]
    files = find_m4a_files(target_paths, recursive=args.recursive)

    if not files:
        print("No .m4a files found.")
        sys.exit(0)

    print(f"Found {len(files)} .m4a file(s). Target: AAC {args.bitrate // 1000}k VBR (best quality)")
    print("-" * 65)

    success_count = 0
    skip_count = 0
    fail_count = 0

    # Group files by parent directory (preserving discovery order)
    files_by_dir: Dict[Path, List[Path]] = {}
    for file_path in files:
        files_by_dir.setdefault(file_path.parent, []).append(file_path)

    idx = 0
    total_files = len(files)

    try:
        global _current_temp_dir
        for parent_dir, dir_files in files_by_dir.items():
            ordered_files, gapless_neighbors = build_gapless_plan(dir_files)
            use_gapless_batch = bool(gapless_neighbors) and not args.dry_run

            batch_temp_dir: Optional[str] = None
            staged_replacements: List[Tuple[Path, Path]] = []

            if use_gapless_batch:
                batch_temp_dir = tempfile.mkdtemp(prefix=".afconvert_tmp_", dir=parent_dir)
                _current_temp_dir = batch_temp_dir

            try:
                for file_path in ordered_files:
                    idx += 1
                    rel_name = file_path.name
                    print(f"[{idx}/{total_files}] Processing: {rel_name} ...", end=" ", flush=True)

                    before, after = gapless_neighbors.get(file_path, (None, None))
                    staged_out = (Path(batch_temp_dir) / file_path.name) if batch_temp_dir else None

                    success, message = convert_alac_to_aac(
                        file_path=file_path,
                        bitrate=args.bitrate,
                        force=args.force,
                        dry_run=args.dry_run,
                        copy_metadata=(not args.no_tags),
                        gapless_before=before,
                        gapless_after=after,
                        staged_out=staged_out,
                    )

                    if success:
                        success_count += 1
                        if staged_out is not None:
                            staged_replacements.append((staged_out, file_path))
                        print(f"OK ({message})")
                    elif "Skipped" in message:
                        skip_count += 1
                        print(message)
                    else:
                        fail_count += 1
                        print(f"FAILED ({message})")

                # Replace all succeeded tracks in the directory after all gapless encodes finish
                for staged_out, target_path in staged_replacements:
                    os.replace(staged_out, target_path)
            finally:
                if batch_temp_dir and os.path.exists(batch_temp_dir):
                    try:
                        shutil.rmtree(batch_temp_dir, ignore_errors=True)
                    except Exception:
                        pass
                    if _current_temp_dir == batch_temp_dir:
                        _current_temp_dir = None

    except KeyboardInterrupt:
        _signal_handler(signal.SIGINT, None)

    print("-" * 65)
    print(f"Finished: {success_count} converted, {skip_count} skipped, {fail_count} failed.")


if __name__ == "__main__":
    main()
