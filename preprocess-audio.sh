#!/usr/bin/env bash
set -euo pipefail

python3 ~/'Desktop/tree/scripts/Audio Utilities/convert_to_alac.py' 
uv run ~/'Desktop/tree/scripts/Audio Utilities/convert_alac_to_aac.py' -r -b 288000 
uv run ~/'Desktop/tree/scripts/Audio Utilities/detect-covers-for-import.py'
uv run ~/'Desktop/tree/scripts/Audio Utilities/embed_cover_art.py'