#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.9"
# dependencies = []
# ///
"""
Recursively analyze .m4a and .mp3 files using Chromaprint (`fpcalc`).
If a directory A has fewer audio files than another directory B, and every
audio file in A matches (>= 98% similar, +/- 1.0s duration) a file in B,
tag directory A Red in Finder via xattr.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import os
import plistlib
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

AUDIO_EXTENSIONS = {".m4a", ".mp3"}
SIMILARITY_THRESHOLD = 0.98
MAX_DURATION_DIFF = 1.0
MAX_FRAME_SHIFT = 8  # ~1 second of Chromaprint frames (~0.1238 s/frame)
FINDER_TAGS_XATTR = "com.apple.metadata:_kMDItemUserTags"
RED_FINDER_TAG = "Red\n6"

# 16-bit popcount lookup table for Python 3.9 compatibility
_POPCOUNT16 = [bin(i).count("1") for i in range(65536)]


def _popcount32(x: int) -> int:
    return _POPCOUNT16[x & 0xFFFF] + _POPCOUNT16[(x >> 16) & 0xFFFF]


@dataclass(frozen=True)
class AudioFingerprint:
    path: Path
    duration: float
    fingerprint: tuple[int, ...]


def run_fpcalc(fpcalc: str, audio_path: Path) -> AudioFingerprint | None:
    """Compute raw Chromaprint fingerprint and duration using `fpcalc -raw -json`."""
    res = subprocess.run(
        [fpcalc, "-raw", "-json", str(audio_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    if res.returncode != 0 or not res.stdout.strip():
        return None
    try:
        data = json.loads(res.stdout)
        duration = float(data["duration"])
        fp = tuple(int(x) & 0xFFFFFFFF for x in data["fingerprint"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None
    if not fp:
        return None
    return AudioFingerprint(path=audio_path, duration=duration, fingerprint=fp)


def max_chromaprint_similarity(fp1: tuple[int, ...], fp2: tuple[int, ...]) -> float:
    """
    Return the highest bit-level similarity [0.0, 1.0] between two raw Chromaprint
    fingerprints across small frame shifts (-MAX_FRAME_SHIFT..+MAX_FRAME_SHIFT).
    """
    len1, len2 = len(fp1), len(fp2)
    min_len = min(len1, len2)
    if min_len == 0:
        return 0.0

    best = 0.0
    for shift in range(-MAX_FRAME_SHIFT, MAX_FRAME_SHIFT + 1):
        start1 = max(0, shift)
        start2 = max(0, -shift)
        overlap = min(len1 - start1, len2 - start2)
        if overlap <= 0 or overlap < int(min_len * 0.8):
            continue

        total_bits = overlap * 32
        max_diff_bits = int((1.0 - best) * total_bits)
        diff_bits = 0
        for i in range(overlap):
            diff_bits += _popcount32(fp1[start1 + i] ^ fp2[start2 + i])
            if diff_bits > max_diff_bits:
                break
        else:
            sim = 1.0 - (diff_bits / total_bits)
            if sim > best:
                best = sim
                if best >= 1.0:
                    return 1.0
    return best


def tag_directory_red(dir_path: Path) -> None:
    """Set the Finder Red tag on dir_path via xattr, preserving any existing tags."""
    tags: list[str] = []
    read_res = subprocess.run(
        ["xattr", "-px", FINDER_TAGS_XATTR, str(dir_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    if read_res.returncode == 0 and read_res.stdout.strip():
        try:
            loaded = plistlib.loads(bytes.fromhex(read_res.stdout))
            if isinstance(loaded, list):
                tags = [t for t in loaded if isinstance(t, str)]
        except Exception:
            tags = []

    if RED_FINDER_TAG in tags:
        return

    tags.append(RED_FINDER_TAG)
    hex_plist = plistlib.dumps(tags, fmt=plistlib.FMT_BINARY).hex()
    subprocess.run(
        ["xattr", "-wx", FINDER_TAGS_XATTR, hex_plist, str(dir_path)],
        check=True,
    )


def main() -> int:
    fpcalc = shutil.which("fpcalc")
    if not fpcalc:
        print("Error: Missing required tool 'fpcalc' in PATH (brew install chromaprint).", file=sys.stderr)
        return 1

    parser = argparse.ArgumentParser(
        description=(
            "Use Chromaprint (fpcalc) to find directories A whose .m4a/.mp3 files "
            "all match (>= 98% similar, +/- 1s) files in a larger directory B, "
            "and tag directory A Red in Finder."
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
        help="Preview matches without setting Finder tags",
    )
    args = parser.parse_args()

    root = Path(args.directory).resolve()
    if not root.is_dir():
        print(f"Error: Directory does not exist: {args.directory}", file=sys.stderr)
        return 1

    dir_tracks: dict[Path, list[Path]] = {}
    all_paths: list[Path] = []

    for current_dir, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if not d.startswith("."))
        dir_path = Path(current_dir)
        audio_files = [
            dir_path / f
            for f in sorted(files)
            if not f.startswith(".") and Path(f).suffix.lower() in AUDIO_EXTENSIONS
        ]
        if audio_files:
            dir_tracks[dir_path] = audio_files
            all_paths.extend(audio_files)

    if len(dir_tracks) < 2:
        print("Nothing to compare (need at least two directories with audio files).")
        return 0

    with ThreadPoolExecutor() as pool:
        results = list(pool.map(lambda p: run_fpcalc(fpcalc, p), all_paths))

    fp_map: dict[Path, AudioFingerprint] = {
        res.path: res for res in results if res is not None
    }

    # Sort directories by track count ascending so we check smaller folders against larger ones
    dirs_by_count = sorted(dir_tracks.items(), key=lambda item: (len(item[1]), str(item[0])))
    sim_cache: dict[tuple[Path, Path], float] = {}

    def get_similarity(fp_a: AudioFingerprint, fp_b: AudioFingerprint) -> float:
        key = (fp_a.path, fp_b.path) if fp_a.path < fp_b.path else (fp_b.path, fp_a.path)
        if key not in sim_cache:
            sim_cache[key] = max_chromaprint_similarity(fp_a.fingerprint, fp_b.fingerprint)
        return sim_cache[key]

    matched_dirs = 0

    for idx_a, (dir_a, tracks_a) in enumerate(dirs_by_count):
        fps_a = [fp_map.get(p) for p in tracks_a]
        if any(fp is None for fp in fps_a):
            continue

        count_a = len(tracks_a)
        for dir_b, tracks_b in dirs_by_count[idx_a + 1:]:
            if len(tracks_b) <= count_a:
                continue

            fps_b = [fp_map[p] for p in tracks_b if p in fp_map]
            if len(fps_b) < count_a:
                continue

            matched_pairs: list[tuple[str, str, float]] = []
            all_matched = True
            for fp_a in fps_a:
                assert fp_a is not None
                best_match: tuple[str, float] | None = None
                for fp_b in fps_b:
                    if abs(fp_a.duration - fp_b.duration) > MAX_DURATION_DIFF:
                        continue
                    sim = get_similarity(fp_a, fp_b)
                    if sim >= SIMILARITY_THRESHOLD:
                        if best_match is None or sim > best_match[1]:
                            best_match = (fp_b.path.name, sim)
                if best_match is None:
                    all_matched = False
                    break
                matched_pairs.append((fp_a.path.name, best_match[0], best_match[1]))

            if all_matched:
                rel_a = "." if dir_a == root else str(dir_a.relative_to(root))
                rel_b = "." if dir_b == root else str(dir_b.relative_to(root))
                min_pct = min(m[2] for m in matched_pairs) * 100.0
                if args.dry_run:
                    print(
                        f"[DRY RUN] Would tag Red: {rel_a} ({count_a} track(s)) "
                        f"<= {rel_b} ({len(tracks_b)} track(s), min {min_pct:.2f}%)"
                    )
                else:
                    tag_directory_red(dir_a)
                    print(
                        f"[TAGGED RED] {rel_a} ({count_a} track(s)) "
                        f"<= {rel_b} ({len(tracks_b)} track(s), min {min_pct:.2f}%)"
                    )
                matched_dirs += 1
                break

    print("=" * 60)
    print(
        f"Done. Directories checked: {len(dir_tracks)} | Audio files: {len(all_paths)} | "
        f"Matched & tagged Red: {matched_dirs}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
