#!/usr/bin/env python3
"""
Convert JPEG and PNG images recursively to JPEG XL (.jxl) losslessly,
verify exact reconstruction via SHA-256 hash, and only replace if the
resulting .jxl file is strictly smaller than the original.
"""

import argparse
import hashlib
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import zlib
from pathlib import Path
from typing import Optional, Tuple

JPEG_EXTENSIONS = {".jpg", ".jpeg", ".jpe", ".jfif"}
PNG_EXTENSIONS = {".png"}
ALL_EXTENSIONS = JPEG_EXTENSIONS | PNG_EXTENSIONS


def check_tool_dependencies() -> None:
    """Verify that cjxl and djxl are installed and accessible."""
    missing = [tool for tool in ("cjxl", "djxl") if shutil.which(tool) is None]
    if missing:
        print(f"Error: Missing required tools: {', '.join(missing)}", file=sys.stderr)
        print("Please install libjxl-tools (JPEG XL tools):", file=sys.stderr)
        print("  - macOS (Homebrew):  brew install jpeg-xl", file=sys.stderr)
        print("  - Debian / Ubuntu:   sudo apt install libjxl-tools", file=sys.stderr)
        print("  - Arch Linux:        sudo pacman -S libjxl", file=sys.stderr)
        sys.exit(1)


def file_sha256(filepath: Path) -> str:
    """Compute SHA-256 hash of an entire file."""
    hasher = hashlib.sha256()
    with open(filepath, "rb") as f:
        while chunk := f.read(1024 * 1024):
            hasher.update(chunk)
    return hasher.hexdigest()


def extract_png_raw_pixels(png_path: Path) -> bytes:
    """
    Extract raw uncompressed raster pixel bytes from a PNG file.
    Reverses PNG scanline filtering according to the W3C PNG specification.
    """
    with open(png_path, "rb") as f:
        sig = f.read(8)
        if sig != b"\x89PNG\r\n\x1a\n":
            raise ValueError(f"Not a valid PNG file: {png_path}")

        idat_chunks = bytearray()
        width = height = bit_depth = color_type = interlace = None
        palette = None

        while True:
            header = f.read(8)
            if len(header) < 8:
                break
            length, chunk_type = struct.unpack(">I4s", header)
            chunk_data = f.read(length)
            f.read(4)  # CRC

            if chunk_type == b"IHDR":
                width, height, bit_depth, color_type, _, _, interlace = struct.unpack(
                    ">IIBBBBB", chunk_data[:13]
                )
            elif chunk_type == b"PLTE":
                palette = chunk_data
            elif chunk_type == b"IDAT":
                idat_chunks.extend(chunk_data)
            elif chunk_type == b"IEND":
                break

    if width is None or height is None:
        raise ValueError("Corrupt PNG: missing IHDR")

    if interlace != 0:
        # For rare Adam7 interlaced PNGs, decode with djxl or fallback to basic reader
        raise NotImplementedError("Interlaced PNG verification requires non-interlaced comparison.")

    decompressed = zlib.decompress(bytes(idat_chunks))

    channels_map = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}
    if color_type not in channels_map:
        raise ValueError(f"Unsupported PNG color type: {color_type}")

    channels = channels_map[color_type]
    if bit_depth == 16:
        bytes_per_pixel = channels * 2
    elif bit_depth == 8:
        bytes_per_pixel = channels
    else:
        bytes_per_pixel = 1

    stride = 1 + (width * bytes_per_pixel if bit_depth >= 8 else (width * channels * bit_depth + 7) // 8)
    prev_row = bytearray(stride - 1)
    unfiltered = bytearray()
    bpp_filter = max(1, bytes_per_pixel)

    for y in range(height):
        row = decompressed[y * stride : (y + 1) * stride]
        filter_type = row[0]
        curr_row = bytearray(stride - 1)
        for x in range(stride - 1):
            filt_val = row[1 + x]
            a = curr_row[x - bpp_filter] if x >= bpp_filter else 0
            b = prev_row[x]
            c = prev_row[x - bpp_filter] if x >= bpp_filter else 0

            if filter_type == 0:  # None
                val = filt_val
            elif filter_type == 1:  # Sub
                val = (filt_val + a) & 0xFF
            elif filter_type == 2:  # Up
                val = (filt_val + b) & 0xFF
            elif filter_type == 3:  # Average
                val = (filt_val + ((a + b) // 2)) & 0xFF
            elif filter_type == 4:  # Paeth
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                pr = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                val = (filt_val + pr) & 0xFF
            else:
                raise ValueError(f"Unknown PNG filter type: {filter_type}")
            curr_row[x] = val

        unfiltered.extend(curr_row)
        prev_row = curr_row

    # If indexed palette, map indices to RGB
    if color_type == 3 and palette:
        expanded = bytearray()
        for idx in unfiltered:
            expanded.extend(palette[idx * 3 : (idx + 1) * 3])
        return bytes(expanded)

    return bytes(unfiltered)


def png_pixels_sha256(png_path: Path) -> str:
    """Compute SHA-256 of the raw raster pixels of a PNG."""
    pixels = extract_png_raw_pixels(png_path)
    return hashlib.sha256(pixels).hexdigest()


def format_bytes(size: int) -> str:
    """Format byte count into human-readable string."""
    units = ["B", "KB", "MB", "GB"]
    s = float(size)
    for u in units:
        if abs(s) < 1024.0 or u == units[-1]:
            return f"{s:.2f} {u}"
        s /= 1024.0
    return f"{s:.2f} TB"


def convert_and_verify(
    image_path: Path,
    effort: int = 7,
    overwrite: bool = False,
    allow_larger: bool = False,
    dry_run: bool = False,
) -> Tuple[bool, str, int, int]:
    """
    Losslessly convert JPEG or PNG to JXL, verify lossless reconstruction,
    and only replace if JXL is smaller than original.

    Returns:
        (success, message, original_size, jxl_size)
    """
    original_size = image_path.stat().st_size
    target_jxl = image_path.with_suffix(".jxl")
    is_jpeg = image_path.suffix.lower() in JPEG_EXTENSIONS
    is_png = image_path.suffix.lower() in PNG_EXTENSIONS

    if not is_jpeg and not is_png:
        return False, f"Unsupported format: {image_path.suffix}", original_size, 0

    if target_jxl.exists() and not overwrite:
        return False, f"Target already exists: {target_jxl.name} (use --overwrite to replace)", original_size, 0

    if dry_run:
        return True, f"[Dry-run] Would convert {image_path.name} -> {target_jxl.name} (if smaller)", original_size, 0

    with tempfile.TemporaryDirectory(prefix="jxl_conv_") as tmpdir:
        tmp_dir_path = Path(tmpdir)
        temp_jxl = tmp_dir_path / "output.jxl"

        # 1. Lossless encoding:
        # - For JPEG: -j 1 (lossless DCT transcode) + -d 0
        # - For PNG:  -d 0 (mathematically lossless Modular mode)
        cmd_encode = ["cjxl", str(image_path), str(temp_jxl), "-d", "0", "-e", str(effort), "--quiet"]
        if is_jpeg:
            cmd_encode.extend(["-j", "1"])

        res = subprocess.run(cmd_encode, capture_output=True, text=True)
        if res.returncode != 0:
            err = res.stderr.strip() or res.stdout.strip() or "Encoding failed"
            return False, f"cjxl error: {err}", original_size, 0

        jxl_size = temp_jxl.stat().st_size

        # 2. Check if JXL is actually smaller than original
        if jxl_size >= original_size and not allow_larger:
            diff = jxl_size - original_size
            pct = (diff / original_size) * 100 if original_size > 0 else 0
            return (
                False,
                f"Skipped {image_path.name}: JXL is not smaller (+{format_bytes(diff)}, +{pct:.1f}%). Original kept.",
                original_size,
                jxl_size,
            )

        # 3. Decode back and verify hash
        if is_jpeg:
            temp_restored = tmp_dir_path / "restored.jpg"
            res = subprocess.run(["djxl", str(temp_jxl), str(temp_restored), "--quiet"], capture_output=True, text=True)
            if res.returncode != 0:
                err = res.stderr.strip() or res.stdout.strip() or "Decoding failed"
                return False, f"djxl error: {err}", original_size, 0

            # For JPEG, lossless DCT transcoding reconstructs exact byte-for-byte identical file
            orig_hash = file_sha256(image_path)
            restored_hash = file_sha256(temp_restored)
            if orig_hash != restored_hash:
                return False, f"Verification failed: Checksum mismatch for {image_path.name} (original preserved)", original_size, 0

        elif is_png:
            temp_restored = tmp_dir_path / "restored.png"
            res = subprocess.run(["djxl", str(temp_jxl), str(temp_restored), "--quiet"], capture_output=True, text=True)
            if res.returncode != 0:
                err = res.stderr.strip() or res.stdout.strip() or "Decoding failed"
                return False, f"djxl error: {err}", original_size, 0

            # For PNG, verify uncompressed raster pixel SHA-256 hash
            try:
                orig_pixel_hash = png_pixels_sha256(image_path)
                restored_pixel_hash = png_pixels_sha256(temp_restored)
                if orig_pixel_hash != restored_pixel_hash:
                    return False, f"Verification failed: Pixel hash mismatch for {image_path.name} (original preserved)", original_size, 0
            except Exception as e:
                return False, f"PNG verification error on {image_path.name}: {e} (original preserved)", original_size, 0

        # 4. Atomically move converted file to final location
        shutil.move(str(temp_jxl), str(target_jxl))

        # 5. Delete original image ONLY after successful move
        try:
            image_path.unlink()
        except OSError as e:
            # If original could not be deleted (e.g. read-only permissions), remove target .jxl
            # to avoid leaving orphan duplicate files behind.
            if target_jxl.exists():
                target_jxl.unlink()
            return False, f"Permission error: Failed to delete original ({e}). Cleaned up target .jxl.", original_size, 0

    saved = original_size - jxl_size
    pct_saved = (saved / original_size) * 100 if original_size > 0 else 0
    return (
        True,
        f"{image_path.name} -> {target_jxl.name} ({format_bytes(original_size)} -> {format_bytes(jxl_size)}, -{pct_saved:.1f}%)",
        original_size,
        jxl_size,
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Recursively convert JPEG and PNG to JPEG XL (.jxl) losslessly, verify reconstruction hash, and only replace if smaller."
    )
    parser.add_argument(
        "directory",
        nargs="?",
        default=".",
        help="Root directory to search recursively (default: current directory)",
    )
    parser.add_argument(
        "-e",
        "--effort",
        type=int,
        default=7,
        choices=range(1, 11),
        metavar="1-10",
        help="Encoder effort level 1..10 (default: 7)",
    )
    parser.add_argument(
        "--allow-larger",
        action="store_true",
        help="Replace original even if resulting .jxl is larger (default is to only replace if smaller)",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Overwrite target .jxl file if it already exists",
    )
    parser.add_argument(
        "-n",
        "--dry-run",
        action="store_true",
        help="Show what would be converted without modifying or deleting files",
    )

    args = parser.parse_args()
    check_tool_dependencies()

    root_path = Path(args.directory).resolve()
    if not root_path.exists() or not root_path.is_dir():
        print(f"Error: Directory '{args.directory}' does not exist or is not a directory.", file=sys.stderr)
        sys.exit(1)

    print(f"Scanning for JPEG and PNG files in: {root_path}")
    files = [
        p for p in root_path.rglob("*")
        if p.is_file() and p.suffix.lower() in ALL_EXTENSIONS
    ]

    if not files:
        print("No JPEG or PNG files found.")
        return

    print(f"Found {len(files)} file(s) to process.\n")

    total_orig_bytes = 0
    total_jxl_bytes = 0
    success_count = 0
    skip_or_fail_count = 0

    try:
        for i, img_path in enumerate(files, start=1):
            rel_path = img_path.relative_to(root_path)
            print(f"[{i}/{len(files)}] Processing {rel_path}...")

            ok, msg, orig_sz, jxl_sz = convert_and_verify(
                image_path=img_path,
                effort=args.effort,
                overwrite=args.overwrite,
                allow_larger=args.allow_larger,
                dry_run=args.dry_run,
            )

            if ok:
                success_count += 1
                total_orig_bytes += orig_sz
                total_jxl_bytes += jxl_sz
                print(f"  ✓ {msg}")
            else:
                skip_or_fail_count += 1
                print(f"  ℹ {msg}")
    except KeyboardInterrupt:
        print("\n\n[!] Interrupted by user (Ctrl+C). Any active temporary files have been cleaned up.")
        sys.exit(130)

    print("\n" + "=" * 55)
    print(f"Summary: {success_count} replaced, {skip_or_fail_count} skipped/failed.")
    if not args.dry_run and total_orig_bytes > 0 and total_jxl_bytes > 0:
        saved_bytes = total_orig_bytes - total_jxl_bytes
        ratio = (saved_bytes / total_orig_bytes) * 100
        print(f"Original size of replaced files: {format_bytes(total_orig_bytes)}")
        print(f"New JXL size:                    {format_bytes(total_jxl_bytes)}")
        print(f"Total space saved:               {format_bytes(saved_bytes)} ({ratio:.1f}%)")
    print("=" * 55)


if __name__ == "__main__":
    main()
