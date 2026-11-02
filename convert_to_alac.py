#!/usr/bin/env python3
"""Recursively convert WAV and FLAC files under the current directory to ALAC."""

from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile


def get_audio_hash(audio_file: Path, temp_dir: Path) -> str:
    """Compute the SHA-1 hash of the audio data in an audio file using macOS built-in tools."""
    caf_path = temp_dir / f"verify_{audio_file.stem}_{audio_file.suffix.lstrip('.')}.caf"
    convert_result = subprocess.run(
        ["afconvert", "-f", "caff", "-d", "LEI32", str(audio_file), str(caf_path)],
        capture_output=True,
        text=True,
    )
    if convert_result.returncode != 0:
        print(f"afconvert failed to decode {audio_file} for verification: {convert_result.stderr}", file=sys.stderr)
        sys.exit(1)

    hash_result = subprocess.run(
        ["afhash", str(caf_path)],
        capture_output=True,
        text=True,
    )
    match = re.search(r"Hash of audio data\s*:\s*([0-9a-fA-F]+)", hash_result.stdout)
    if match:
        return match.group(1).lower()

    # Fallback for sample rates unsupported by afhash (< 44100 Hz): hash PCM chunk using shasum
    with open(caf_path, "rb") as f:
        data = f.read()
    data_idx = data.find(b"data")
    pcm_bytes = data[data_idx + 16:] if data_idx != -1 else data
    shasum_result = subprocess.run(
        ["shasum", "-a", "1"],
        input=pcm_bytes,
        capture_output=True,
        check=True,
    )
    return shasum_result.stdout.decode().split()[0].lower()


def main() -> int:
    root = Path.cwd()
    ffmpeg = shutil.which("ffmpeg")
    sources = sorted(
        path
        for path in root.rglob("*")
        if path.is_file() and path.suffix.lower() in {".wav", ".flac"}
    )

    if not sources:
        print("No WAV or FLAC files found.")
        return 0

    failures = 0
    for source in sources:
        # Keep the source's directory and base name; use the usual ALAC container.
        destination = source.with_suffix(".m4a")
        if destination.exists():
            print(f"Skipping (output already exists): {destination.relative_to(root)}")
            continue

        print(f"Converting: {source.relative_to(root)} -> {destination.relative_to(root)}")
        # Keep afconvert responsible for audio encoding. For FLAC, remux that
        # ALAC audio with metadata (and any attached cover image) from the source.
        with tempfile.TemporaryDirectory(dir=source.parent) as temporary_dir:
            audio_only = Path(temporary_dir) / "audio.m4a"
            tagged_output = Path(temporary_dir) / "tagged.m4a"
            afconvert_result = subprocess.run(
                ["afconvert", "-f", "m4af", "-d", "alac", str(source), str(audio_only)],
                check=False,
            )
            if afconvert_result.returncode:
                failures += 1
                print(f"afconvert failed ({afconvert_result.returncode}): {source}", file=sys.stderr)
                continue

            if source.suffix.lower() == ".flac":
                if ffmpeg is None:
                    failures += 1
                    print("Cannot copy FLAC tags: ffmpeg is not installed or not on PATH.", file=sys.stderr)
                    continue
                ffmpeg_result = subprocess.run(
                    [
                        ffmpeg, "-v", "error", "-y",
                        "-i", str(audio_only), "-i", str(source),
                        "-map", "0:a:0", "-map", "1:v?",
                        "-map_metadata", "1", "-map_chapters", "1",
                        "-c", "copy", "-disposition:v:0", "attached_pic",
                        str(tagged_output),
                    ],
                    check=False,
                )
                if ffmpeg_result.returncode:
                    failures += 1
                    print(f"ffmpeg metadata remux failed ({ffmpeg_result.returncode}): {source}", file=sys.stderr)
                    continue
                final_source = tagged_output
            else:
                final_source = audio_only

            source_hash = get_audio_hash(source, Path(temporary_dir))
            dest_hash = get_audio_hash(final_source, Path(temporary_dir))
            if source_hash != dest_hash:
                print(
                    f"Audio hash mismatch for {source.relative_to(root)}: "
                    f"source={source_hash} != destination={dest_hash}. Aborting.",
                    file=sys.stderr,
                )
                sys.exit(1)

            print(
                f"Audio hashes match for {source.relative_to(root)}: "
                f"source={source_hash} == destination={dest_hash}"
            )

            shutil.copy2(final_source, destination)
        # Remove the original only after the completed output is in place.
        source.unlink()

    if failures:
        print(f"Finished with {failures} failed conversion(s).", file=sys.stderr)
        return 1
    print("Finished.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
