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
- Computes and embeds an SHA-256 integrity hash of the raw encoded AAC audio packets.
- Protects stream-specific metadata: iTunNORM (SoundCheck), iTunSMPB (gapless), and AUDIO_SHA256.
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
from typing import List, Optional, Tuple

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
    Compute the SHA-256 hash of the raw audio packets (the mdat atom payload),
    excluding all MP4 container headers and metadata.
    """
    hasher = hashlib.sha256()
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


def copy_tags(src_path: Path, dst_path: Path) -> Tuple[bool, Optional[str]]:
    """
    Copy all MP4/M4A metadata tags and embedded cover artwork
    from src_path to dst_path using mutagen, while protecting
    stream-specific timing, loudness, and audio integrity tags:
    - iTunNORM (SoundCheck volume normalization)
    - iTunSMPB (Gapless playback priming and remainder padding)
    - AUDIO_SHA256 (SHA-256 hash of the newly encoded AAC audio packets)

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

        # Compute SHA-256 hash of the newly encoded raw AAC audio packets (mdat payload)
        audio_hash = compute_audio_packets_hash(dst_path)

        # Protected stream-specific tags generated for this specific AAC stream
        protected_keys = {
            "----:com.apple.iTunes:iTunNORM",
            "----:com.apple.iTunes:iTunSMPB",
            "----:com.apple.iTunes:AUDIO_SHA256",
        }
        # Snapshot any existing destination values created by afconvert
        preserved_tags = {k: dst.tags[k] for k in protected_keys if k in dst.tags}

        # If audio hash was successfully computed, add it to preserved destination tags as UTF-8
        if audio_hash:
            preserved_tags["----:com.apple.iTunes:AUDIO_SHA256"] = [
                MP4FreeForm(audio_hash.encode("utf-8"), AtomDataType.UTF8)
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
) -> Tuple[bool, str]:
    """
    Convert a single M4A file from ALAC to AAC VBR.

    Parameters:
        file_path: Path to target file.
        bitrate: Bitrate in bps (e.g. 300000 for 300 kbps).
        force: If True, convert even if codec doesn't explicitly detect as ALAC.
        dry_run: If True, only simulate conversion.
        copy_metadata: If True, copy tags and artwork using mutagen.

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
        return True, f"Dry-run: Would convert to AAC {bitrate // 1000} kbps VBR"

    parent_dir = file_path.parent
    orig_size = file_path.stat().st_size

    # Create a temporary directory inside the same parent directory.
    # This guarantees that os.replace() at the end is an atomic same-filesystem operation.
    temp_dir = tempfile.mkdtemp(prefix=".afconvert_tmp_", dir=parent_dir)
    _current_temp_dir = temp_dir
    temp_out = Path(temp_dir) / file_path.name

    # afconvert options for maximum quality:
    # -d aac              : AAC audio format
    # -f m4af             : MPEG-4 Audio (.m4a) container
    # -s 2                : VBR_constrained (targets the specified bitrate using VBR)
    # -b <bitrate>        : Target bitrate (e.g., 300000 bps = 300 kbps)
    # -q 127              : Highest codec encoding quality (0-127)
    # -r 127              : Highest sample rate converter quality (0-127)
    # --src-complexity bats: Highest sample rate converter complexity (bats = best bandlimited sinc)
    # --soundcheck-generate: Calculate Apple SoundCheck volume normalization metadata
    cmd = [
        "afconvert",
        "-d", "aac",
        "-f", "m4af",
        "-s", "2",
        "-b", str(bitrate),
        "-q", "127",
        "-r", "127",
        "--src-complexity", "bats",
        "--soundcheck-generate",
        str(file_path),
        str(temp_out),
    ]

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
            tag_ok, tag_err = copy_tags(file_path, temp_out)
            if not tag_ok:
                return False, f"Tag copy failed ({tag_err}); aborted to preserve original file"

        # Verify that the final converted file (including tags & artwork) is smaller than original
        final_size = temp_out.stat().st_size
        if final_size >= orig_size:
            return False, f"Output ({format_size(final_size)}) is not smaller than original ({format_size(orig_size)}); aborted to preserve original"

        saved_bytes = orig_size - final_size
        saved_pct = (saved_bytes / orig_size) * 100 if orig_size > 0 else 0

        # Atomic replacement: replaces original file with the newly encoded file.
        # Original file is intact until this exact moment.
        os.replace(temp_out, file_path)

        return True, f"{format_size(orig_size)} -> {format_size(final_size)} ({saved_pct:.1f}% reduction)"

    except KeyboardInterrupt:
        raise
    except Exception as exc:
        return False, f"Unexpected error: {exc}"
    finally:
        _current_process = None
        # Always clean up the temporary directory
        if temp_dir and os.path.exists(temp_dir):
            try:
                shutil.rmtree(temp_dir, ignore_errors=True)
            except Exception:
                pass
        _current_temp_dir = None


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
    for tool in ["afconvert", "afinfo"]:
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

    try:
        for idx, file_path in enumerate(files, start=1):
            rel_name = file_path.name
            print(f"[{idx}/{len(files)}] Processing: {rel_name} ...", end=" ", flush=True)

            success, message = convert_alac_to_aac(
                file_path=file_path,
                bitrate=args.bitrate,
                force=args.force,
                dry_run=args.dry_run,
                copy_metadata=(not args.no_tags),
            )

            if success:
                success_count += 1
                print(f"OK ({message})")
            elif "Skipped" in message:
                skip_count += 1
                print(message)
            else:
                fail_count += 1
                print(f"FAILED ({message})")

    except KeyboardInterrupt:
        _signal_handler(signal.SIGINT, None)

    print("-" * 65)
    print(f"Finished: {success_count} converted, {skip_count} skipped, {fail_count} failed.")


if __name__ == "__main__":
    main()
