# /// script
# requires-python = ">=3.9"
# dependencies = [
#     "numpy",
# ]
# ///
"""Recursively pick 5 random .mp3/.m4a files and print the highest frequency with content (read-only)."""

from __future__ import annotations

import argparse
import os
import random
import shutil
import subprocess
import sys
from pathlib import Path

import numpy as np

AUDIO_EXTENSIONS = {".mp3", ".m4a"}
ANSI_GREEN = "\033[32m"
ANSI_ORANGE = "\033[38;5;208m"
ANSI_YELLOW = "\033[33m"
ANSI_RESET = "\033[0m"


def get_sample_rate(audio_path: Path) -> int:
    """Probe the audio stream sample rate in Hz using ffprobe."""
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "a:0",
            "-show_entries",
            "stream=sample_rate",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(audio_path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return int(result.stdout.strip().splitlines()[0])


def highest_frequency_hz(audio_path: Path, db_threshold: float = -70.0) -> float:
    """Compute the highest frequency (Hz) with spectral energy >= db_threshold."""
    sample_rate = get_sample_rate(audio_path)
    pcm = subprocess.check_output(
        [
            "ffmpeg",
            "-v",
            "error",
            "-i",
            str(audio_path),
            "-f",
            "f32le",
            "-ac",
            "1",
            "-ar",
            str(sample_rate),
            "-",
        ]
    )
    samples = np.frombuffer(pcm, dtype=np.float32)
    if len(samples) == 0:
        return 0.0

    n_fft = 4096
    if len(samples) < n_fft:
        n_fft = len(samples)

    window = np.hanning(n_fft)
    n_frames = len(samples) // n_fft
    frames = samples[: n_frames * n_fft].reshape(n_frames, n_fft) * window

    mags = np.abs(np.fft.rfft(frames, axis=1)) / (window.sum() / 2.0)
    peak_db = 20.0 * np.log10(np.max(mags, axis=0) + 1e-12)

    freqs = np.fft.rfftfreq(n_fft, d=1.0 / sample_rate)
    active_bins = np.where(peak_db >= db_threshold)[0]
    return float(freqs[active_bins[-1]]) if len(active_bins) else 0.0


def color_for_frequency(freq_hz: float) -> str:
    """Return ANSI color code based on frequency thresholds (>20 kHz green, >18 kHz orange, else yellow)."""
    if freq_hz > 20000.0:
        return ANSI_GREEN
    if freq_hz > 18000.0:
        return ANSI_ORANGE
    return ANSI_YELLOW


def collect_audio_files(root_dir: Path) -> list[Path]:
    """Read-only recursive walk collecting .mp3 and .m4a files."""
    found: list[Path] = []
    for current_dir, dirs, files in os.walk(root_dir):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for file_name in files:
            if file_name.startswith("."):
                continue
            path = Path(current_dir) / file_name
            if path.suffix.lower() in AUDIO_EXTENSIONS:
                found.append(path)
    return found


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Check highest frequency with content for 5 random .mp3/.m4a files."
    )
    parser.add_argument(
        "directory",
        nargs="?",
        default=".",
        help="Root directory to recursively scan (default: current directory)",
    )
    args = parser.parse_args()

    for tool in ("ffmpeg", "ffprobe"):
        if shutil.which(tool) is None:
            print(f"Error: {tool} is required but was not found in PATH.", file=sys.stderr)
            return 1

    root_dir = Path(args.directory).resolve()
    if not root_dir.exists():
        print(f"Error: Directory does not exist: {root_dir}", file=sys.stderr)
        return 1

    audio_files = collect_audio_files(root_dir)
    if not audio_files:
        print("No .mp3 or .m4a files found.")
        return 0

    sample = random.sample(audio_files, min(5, len(audio_files)))
    for audio_path in sample:
        rel_path = audio_path.relative_to(root_dir)
        try:
            freq_hz = highest_frequency_hz(audio_path)
            color = color_for_frequency(freq_hz)
            print(f"{color}\033[1m{freq_hz / 1000.0:.2f}\033[22m kHz - {rel_path}{ANSI_RESET}")
        except Exception as err:
            print(f"[ERROR] {rel_path}: {err}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
