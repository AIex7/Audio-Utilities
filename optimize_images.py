#!/usr/bin/env python3
"""
Recursively optimize .png, .jpg, and .jpeg files (case-insensitive) via `cjpegli -q 86`:
- Skips files <= 300 KiB.
- Keeps original dimensions if both width and height <= 1400; otherwise resizes
  to max 1400px (preserving aspect ratio) via `sips` before encoding with `cjpegli`.
- If the new JPEG is strictly smaller than the original, trashes the original and
  keeps the new .jpg; otherwise, trashes the new file and keeps the original.
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

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg"}
MAX_DIMENSION = 1400
MIN_SIZE_BYTES = 300 * 1024
CJPEGLI_QUALITY = 86
WIDTH_RE = re.compile(r"pixelWidth:\s*(\d+)")
HEIGHT_RE = re.compile(r"pixelHeight:\s*(\d+)")


def find_cjpegli() -> str | None:
    """Locate cjpegli in PATH or in the local jpegli build directory."""
    in_path = shutil.which("cjpegli")
    if in_path:
        return in_path
    local_bin = Path(__file__).resolve().parent / "jpegli" / "build" / "tools" / "cjpegli"
    if local_bin.is_file() and os.access(local_bin, os.X_OK):
        return str(local_bin)
    return None


def get_dimensions(sips: str, image_path: Path) -> tuple[int, int] | None:
    """Return (width, height) using `sips`, or None if unreadable."""
    res = subprocess.run(
        [sips, "-g", "pixelWidth", "-g", "pixelHeight", str(image_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    if res.returncode != 0:
        return None
    w_match = WIDTH_RE.search(res.stdout)
    h_match = HEIGHT_RE.search(res.stdout)
    if not w_match or not h_match:
        return None
    return int(w_match.group(1)), int(h_match.group(1))


def process_image(
    sips: str,
    cjpegli: str,
    trash: str,
    image_path: Path,
    root: Path,
    dry_run: bool,
) -> str:
    """Process one image file. Returns 'optimized', 'kept', 'skipped', or 'error'."""
    rel_src = image_path.relative_to(root)
    orig_size = image_path.stat().st_size
    if orig_size <= MIN_SIZE_BYTES:
        print(f"[SKIP] <= 300 KiB ({orig_size} B): {rel_src}")
        return "skipped"

    dest = image_path.with_suffix(".jpg")
    if dest != image_path and dest.exists():
        print(f"[SKIP] Target already exists: {dest.relative_to(root)}")
        return "skipped"

    dims = get_dimensions(sips, image_path)
    if dims is None:
        print(f"[ERROR] Could not read dimensions for {rel_src}", file=sys.stderr)
        return "error"

    width, height = dims
    resize = width > MAX_DIMENSION or height > MAX_DIMENSION

    if dry_run:
        resize_note = f"resize to max {MAX_DIMENSION}px + cjpegli -q {CJPEGLI_QUALITY}" if resize else f"keep {width}x{height} + cjpegli -q {CJPEGLI_QUALITY}"
        print(f"[DRY RUN] Would convert ({resize_note}) on {rel_src}")
        return "optimized"

    fd, tmp_name = tempfile.mkstemp(dir=image_path.parent, prefix=".__cjpegli_", suffix=".jpg")
    os.close(fd)
    new_path = Path(tmp_name)
    resized_png: Path | None = None

    try:
        encode_input = image_path
        if resize:
            fd_png, tmp_png_name = tempfile.mkstemp(dir=image_path.parent, prefix=".__resized_", suffix=".png")
            os.close(fd_png)
            resized_png = Path(tmp_png_name)
            sips_res = subprocess.run(
                [sips, "-Z", str(MAX_DIMENSION), "-s", "format", "png", str(image_path), "--out", str(resized_png)],
                capture_output=True,
                text=True,
                check=False,
            )
            if sips_res.returncode != 0 or not resized_png.exists() or resized_png.stat().st_size == 0:
                err = sips_res.stderr.strip() or f"sips exited with {sips_res.returncode}"
                print(f"[ERROR] sips resize failed on {rel_src}: {err}", file=sys.stderr)
                new_path.unlink(missing_ok=True)
                return "error"
            encode_input = resized_png

        res = subprocess.run(
            [cjpegli, "-q", str(CJPEGLI_QUALITY), str(encode_input), str(new_path)],
            capture_output=True,
            text=True,
            check=False,
        )
        if res.returncode != 0 or not new_path.exists() or new_path.stat().st_size == 0:
            err = res.stderr.strip() or f"cjpegli exited with {res.returncode}"
            print(f"[ERROR] cjpegli failed on {rel_src}: {err}", file=sys.stderr)
            new_path.unlink(missing_ok=True)
            return "error"

        new_size = new_path.stat().st_size
        if new_size < orig_size:
            trash_res = subprocess.run([trash, str(image_path)], capture_output=True, text=True, check=False)
            if trash_res.returncode != 0:
                err = trash_res.stderr.strip() or f"trash exited with {trash_res.returncode}"
                print(f"[ERROR] Failed trashing original {rel_src}: {err}", file=sys.stderr)
                new_path.unlink(missing_ok=True)
                return "error"
            new_path.rename(dest)
            print(f"[OPTIMIZED] {rel_src} -> {dest.name} ({orig_size} B -> {new_size} B)")
            return "optimized"

        trash_res = subprocess.run([trash, str(new_path)], capture_output=True, text=True, check=False)
        if trash_res.returncode != 0:
            new_path.unlink(missing_ok=True)
        print(f"[KEPT] {rel_src} ({orig_size} B <= new {new_size} B; trashed new file)")
        return "kept"
    except Exception as exc:
        new_path.unlink(missing_ok=True)
        print(f"[ERROR] Unexpected error on {rel_src}: {exc}", file=sys.stderr)
        return "error"
    finally:
        if resized_png is not None:
            resized_png.unlink(missing_ok=True)


def main() -> int:
    sips = shutil.which("sips")
    cjpegli = find_cjpegli()
    trash = shutil.which("trash")
    missing = [
        name
        for name, path in (("sips", sips), ("cjpegli", cjpegli), ("trash", trash))
        if not path
    ]
    if missing:
        print(f"Error: Missing required tool(s) in PATH: {', '.join(missing)}", file=sys.stderr)
        return 1

    parser = argparse.ArgumentParser(
        description=(
            f"Recursively convert .png/.jpg/.jpeg images (> 300 KiB) to JPEG via cjpegli -q {CJPEGLI_QUALITY} "
            "(max 1400px preserving aspect ratio), keeping whichever file is smaller."
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
        help="Preview sips conversions without modifying any files",
    )
    args = parser.parse_args()

    root = Path(args.directory).resolve()
    if not root.is_dir():
        print(f"Error: Directory does not exist: {args.directory}", file=sys.stderr)
        return 1

    images: list[Path] = []
    for current_dir, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if not d.startswith("."))
        dir_path = Path(current_dir)
        for name in sorted(files):
            if name.startswith("."):
                continue
            path = dir_path / name
            if path.suffix.lower() in IMAGE_EXTENSIONS:
                images.append(path)

    if not images:
        print("No PNG/JPG/JPEG files found.")
        return 0

    optimized = 0
    kept = 0
    skipped = 0
    errors = 0

    for image_path in images:
        status = process_image(sips, cjpegli, trash, image_path, root, dry_run=args.dry_run)
        if status == "optimized":
            optimized += 1
        elif status == "kept":
            kept += 1
        elif status == "skipped":
            skipped += 1
        else:
            errors += 1

    print("=" * 60)
    print(f"Done. Optimized: {optimized} | Kept original: {kept} | Skipped: {skipped} | Errors: {errors}")
    return 1 if errors > 0 else 0


if __name__ == "__main__":
    raise SystemExit(main())
