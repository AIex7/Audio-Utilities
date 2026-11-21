#!/usr/bin/env python3
"""
Recursively normalize cover images (.png, .jpg, .jpeg, case-insensitive) in each directory:
- 1 image: rename to cover.png or cover.jpg.
- >1 images: keep one by priority (cover.* > folder.* > front.*, preferring .png;
  if none match those names, keep the largest file by byte size), trash the rest,
  and rename the kept image to cover.png or cover.jpg.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg"}
PREFERRED_STEMS = ("cover", "folder", "front")


def pick_winner(dir_path: Path, image_names: list[str]) -> str:
    """Choose the single image filename to keep according to name, format, and size rules."""
    def sort_key(name: str) -> tuple[int, int, int, str]:
        path = dir_path / name
        stem = path.stem.lower()
        ext = path.suffix.lower()

        if stem in PREFERRED_STEMS:
            name_rank = PREFERRED_STEMS.index(stem)
            ext_rank = 0 if ext == ".png" else 1
            size_rank = -path.stat().st_size
            return (name_rank, ext_rank, size_rank, name.lower())

        # No preferred name: keep largest file by byte size, then prefer .png
        name_rank = len(PREFERRED_STEMS)
        size_rank = -path.stat().st_size
        ext_rank = 0 if ext == ".png" else 1
        return (name_rank, size_rank, ext_rank, name.lower())

    return min(image_names, key=sort_key)


def rename_to_cover(image_path: Path, root: Path, dry_run: bool) -> bool:
    """Rename image_path to cover.png or cover.jpg, handling case-only APFS renames."""
    target_name = "cover.png" if image_path.suffix.lower() == ".png" else "cover.jpg"
    target_path = image_path.parent / target_name

    if image_path.name == target_name:
        return True

    rel_src = image_path.relative_to(root)
    rel_dst = target_path.relative_to(root)

    if dry_run:
        print(f"[DRY RUN] Would rename {rel_src} -> {rel_dst}")
        return True

    try:
        if target_path.exists() and os.path.samefile(image_path, target_path):
            temp_path = image_path.parent / f".__tmp_{target_name}"
            image_path.rename(temp_path)
            temp_path.rename(target_path)
        else:
            image_path.rename(target_path)
    except OSError as exc:
        print(f"[ERROR] Failed renaming {rel_src} -> {rel_dst}: {exc}", file=sys.stderr)
        return False

    print(f"[RENAME] {rel_src} -> {rel_dst}")
    return True


def process_directory(dir_path: Path, image_names: list[str], root: Path, dry_run: bool) -> int:
    """Normalize images in dir_path. Returns number of errors."""
    if not image_names:
        return 0

    errors = 0
    if len(image_names) == 1:
        if not rename_to_cover(dir_path / image_names[0], root, dry_run=dry_run):
            errors += 1
        return errors

    winner = pick_winner(dir_path, image_names)
    losers = [name for name in sorted(image_names) if name != winner]

    for loser in losers:
        loser_path = dir_path / loser
        rel_loser = loser_path.relative_to(root)
        if dry_run:
            print(f"[DRY RUN] Would trash {rel_loser} (keeping {winner})")
            continue
        res = subprocess.run(["trash", str(loser_path)], capture_output=True, text=True, check=False)
        if res.returncode != 0:
            err = res.stderr.strip() or f"trash exited with {res.returncode}"
            print(f"[ERROR] Failed trashing {rel_loser}: {err}", file=sys.stderr)
            errors += 1
        else:
            print(f"[TRASH] {rel_loser} (kept {winner})")

    if errors == 0:
        if not rename_to_cover(dir_path / winner, root, dry_run=dry_run):
            errors += 1

    return errors


def main() -> int:
    if shutil.which("trash") is None:
        print("Error: Missing required tool 'trash' in PATH (brew install trash).", file=sys.stderr)
        return 1

    parser = argparse.ArgumentParser(
        description=(
            "Recursively normalize cover images (.png/.jpg/.jpeg) to cover.png or cover.jpg, "
            "trashing extras by priority (cover > folder > front, preferring .png, else largest)."
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
        help="Preview renames and trashes without modifying any files",
    )
    args = parser.parse_args()

    root = Path(args.directory).resolve()
    if not root.is_dir():
        print(f"Error: Directory does not exist: {args.directory}", file=sys.stderr)
        return 1

    total_errors = 0
    for current_dir, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if not d.startswith("."))
        dir_path = Path(current_dir)
        image_names = [
            f for f in sorted(files)
            if not f.startswith(".") and Path(f).suffix.lower() in IMAGE_EXTENSIONS
        ]
        total_errors += process_directory(dir_path, image_names, root, dry_run=args.dry_run)

    return 1 if total_errors > 0 else 0


if __name__ == "__main__":
    raise SystemExit(main())
