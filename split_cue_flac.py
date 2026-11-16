#!/usr/bin/env python3
"""
Recursively scans a directory for folders containing a single .flac file and a
single .cue file. For each matching folder:
1. Runs `shnsplit` to extract individual .flac tracks.
2. Runs `cuetag.sh` on the newly extracted .flac tracks to copy metadata tags.
3. If both steps succeed, moves the original .flac and .cue files to macOS Trash.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path


def find_cuetag_cmd() -> str | None:
    """Return the available cuetag executable ('cuetag.sh' or 'cuetag')."""
    for candidate in ("cuetag.sh", "cuetag"):
        if shutil.which(candidate):
            return candidate
    return None


def process_directory(root_dir: str, cuetag_cmd: str, dry_run: bool = False) -> int:
    target_path = Path(root_dir).resolve()
    if not target_path.exists():
        print(f"Error: Target path '{root_dir}' does not exist.", file=sys.stderr)
        return 1

    print(f"Scanning directory: {target_path}")
    print(f"Mode              : {'DRY RUN (no files modified)' if dry_run else 'LIVE (splitting, tagging, trashing)'}")
    print("=" * 80)

    matched_dirs = 0
    succeeded = 0
    errors = 0

    for current_dir, dirs, files in os.walk(target_path):
        dirs[:] = sorted(d for d in dirs if not d.startswith("."))
        dir_path = Path(current_dir)

        flac_files = sorted(
            f for f in files if not f.startswith(".") and Path(f).suffix.lower() == ".flac"
        )
        cue_files = sorted(
            f for f in files if not f.startswith(".") and Path(f).suffix.lower() == ".cue"
        )

        if len(flac_files) != 1 or len(cue_files) != 1:
            continue

        matched_dirs += 1
        flac_name = flac_files[0]
        cue_name = cue_files[0]
        flac_path = dir_path / flac_name
        cue_path = dir_path / cue_name

        print(f"[MATCH] {dir_path}: {flac_name} + {cue_name}")

        if dry_run:
            print(f"  [DRY RUN] Would run shnsplit, {cuetag_cmd}, and trash originals")
            succeeded += 1
            continue

        before_flacs = {
            p.name for p in dir_path.iterdir() if p.is_file() and p.suffix.lower() == ".flac"
        }

        split_cmd = [
            "shnsplit",
            "-f",
            cue_name,
            "-o",
            "flac",
            "-t",
            "%n. %t",
            "-O",
            "always",
            flac_name,
        ]
        split_res = subprocess.run(split_cmd, cwd=dir_path)
        if split_res.returncode != 0:
            print(f"  [ERROR] shnsplit failed (exit {split_res.returncode}) in {dir_path}", file=sys.stderr)
            errors += 1
            continue

        new_flacs = sorted(
            p.name
            for p in dir_path.iterdir()
            if p.is_file() and p.suffix.lower() == ".flac" and p.name not in before_flacs
        )
        if not new_flacs:
            print(f"  [ERROR] shnsplit produced no new .flac files in {dir_path}", file=sys.stderr)
            errors += 1
            continue

        tag_cmd = [cuetag_cmd, cue_name, *new_flacs]
        tag_res = subprocess.run(tag_cmd, cwd=dir_path)
        if tag_res.returncode != 0:
            print(f"  [ERROR] {cuetag_cmd} failed (exit {tag_res.returncode}) in {dir_path}", file=sys.stderr)
            errors += 1
            continue

        try:
            subprocess.run(["trash", str(flac_path), str(cue_path)], check=True)
            print(f"  [DONE] Extracted & tagged {len(new_flacs)} track(s); trashed {flac_name} and {cue_name}")
            succeeded += 1
        except Exception as e:
            print(f"  [ERROR] Failed trashing originals in {dir_path}: {e}", file=sys.stderr)
            errors += 1

    print("=" * 80)
    print(f"Done. Matched dirs: {matched_dirs} | Succeeded: {succeeded} | Errors: {errors}")
    return 1 if errors > 0 else 0


def main() -> int:
    cuetag_cmd = find_cuetag_cmd()
    missing = [tool for tool in ("shnsplit", "flac", "trash") if not shutil.which(tool)]
    if not cuetag_cmd:
        missing.append("cuetag.sh")

    if missing:
        print(f"Error: Missing required tool(s) in PATH: {', '.join(missing)}", file=sys.stderr)
        return 1

    parser = argparse.ArgumentParser(
        description="Recursively split single-FLAC + CUE directories with shnsplit, tag with cuetag.sh, and trash originals."
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
        help="Preview matching directories without modifying any files",
    )

    args = parser.parse_args()
    return process_directory(args.directory, cuetag_cmd=cuetag_cmd, dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
