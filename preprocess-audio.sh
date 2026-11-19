#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

python3 "$SCRIPT_DIR/convert_to_flac.py"

python3 "$SCRIPT_DIR/split_cue_flac.py"
python3 "$SCRIPT_DIR/convert_to_alac.py"
uv run "$SCRIPT_DIR/convert_alac_to_aac.py" -r -b 256000
uv run "$SCRIPT_DIR/detect-covers-for-import.py"
python3 "$SCRIPT_DIR/convert_jpeg_to_jxl.py" -e 10
uv run "$SCRIPT_DIR/embed_cover_art.py"
uv run "$SCRIPT_DIR/add_aac_hash.py"

uv run "$SCRIPT_DIR/check_albums_in_music.py"
uv run "$SCRIPT_DIR/check_short_audio.py"
uv run "$SCRIPT_DIR/check_indices.py"

uv run "$SCRIPT_DIR/random_spek_preview.py"