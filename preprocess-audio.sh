#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

python3 "$SCRIPT_DIR/convert_to_flac.py"

python3 "$SCRIPT_DIR/split_cue_flac.py"

uv run "$SCRIPT_DIR/extract_flac_covers.py"
python3 "$SCRIPT_DIR/normalize_cover_images.py"
python3 "$SCRIPT_DIR/optimize_images.py"


uv run "$SCRIPT_DIR/convert_flac_to_alac.py"
uv run "$SCRIPT_DIR/convert_alac_to_aac.py" -r # -b 256000

uv run "$SCRIPT_DIR/embed_and_trash_cover.py"


uv run "$SCRIPT_DIR/check_albums_in_music.py"
uv run "$SCRIPT_DIR/check_consistent_tags.py"
uv run "$SCRIPT_DIR/check_short_audio.py"
uv run "$SCRIPT_DIR/check_indices.py"
uv run "$SCRIPT_DIR/check_missing_metadata.py"

uv run "$SCRIPT_DIR/random_spek_preview.py"