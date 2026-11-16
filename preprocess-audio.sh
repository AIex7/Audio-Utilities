#!/usr/bin/env bash
set -euo pipefail

python3 ~/'Desktop/tree/scripts/Audio Utilities/split_cue_flac.py' 
python3 ~/'Desktop/tree/scripts/Audio Utilities/convert_to_alac.py' 
uv run ~/'Desktop/tree/scripts/Audio Utilities/convert_alac_to_aac.py' -r -b 256000
uv run ~/'Desktop/tree/scripts/Audio Utilities/detect-covers-for-import.py'
python3 ~/'Desktop/tree/scripts/Audio Utilities/convert_jpeg_to_jxl.py' -e 10
uv run ~/'Desktop/tree/scripts/Audio Utilities/embed_cover_art.py'
uv run ~/'Desktop/tree/scripts/Audio Utilities/add_aac_hash.py'

uv run ~/'Desktop/tree/scripts/Audio Utilities/check_albums_in_music.py'
uv run ~/'Desktop/tree/scripts/Audio Utilities/check_max_frequency.py'
uv run ~/'Desktop/tree/scripts/Audio Utilities/check_short_audio.py'
uv run ~/'Desktop/tree/scripts/Audio Utilities/check_indices.py'