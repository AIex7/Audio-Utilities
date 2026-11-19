#!/usr/bin/env python3
# /// script
# requires-python = ">=3.9"
# dependencies = [
#   "mutagen",
#   "pillow",
# ]
# ///
"""Recursively find .mp3 and .m4a (ALAC/AAC) files, pick 3 at random,
render Doric Siren Purple light-theme ffmpeg spectrograms stacked vertically
with name and bitrate, and display them via qlmanage.
"""

from __future__ import annotations

import argparse
import random
import shutil
import subprocess
import tempfile
from pathlib import Path

import mutagen
from PIL import Image, ImageDraw, ImageFont

AUDIO_EXTS = {".mp3", ".m4a"}

# Doric Siren Purple palette
BG_MAIN = (255, 255, 255)      # --bg-main (#ffffff)
FG_MAIN = (0, 0, 0)            # --fg-main (#000000)
BORDER = (171, 149, 176)       # --border (#ab95b0)
FG_RED = (160, 16, 16)         # --fg-red (#a01010)
FG_GREEN = (16, 103, 16)       # --fg-green (#106710)
FG_BLUE = (16, 48, 119)        # --fg-blue (#103077)
FG_PURPLE = (73, 58, 136)      # --fg-shadow-intense (#493a88)

DORIC_SPEK_STOPS: list[tuple[int, tuple[int, int, int]]] = [
    (0, FG_MAIN),
    (64, (104, 32, 59)),       # --fg-accent-primary (#68203b)
    (128, (91, 44, 146)),      # --cursor (#5b2c92)
    (185, (176, 165, 220)),    # --bg-shadow-intense (#b0a5dc)
    (225, (204, 205, 224)),    # --bg-shadow-subtle (#cccde0)
    (255, BG_MAIN),
]


def _build_doric_lut() -> list[tuple[int, int, int]]:
    lut: list[tuple[int, int, int]] = []
    for v in range(256):
        for i in range(len(DORIC_SPEK_STOPS) - 1):
            v0, c0 = DORIC_SPEK_STOPS[i]
            v1, c1 = DORIC_SPEK_STOPS[i + 1]
            if v0 <= v <= v1:
                t = (v - v0) / (v1 - v0) if v1 > v0 else 0.0
                r = int(round(c0[0] + t * (c1[0] - c0[0])))
                g = int(round(c0[1] + t * (c1[1] - c0[1])))
                b = int(round(c0[2] + t * (c1[2] - c0[2])))
                lut.append((r, g, b))
                break
    return lut


DORIC_LUT = _build_doric_lut()


def find_audio_files(root: Path) -> list[Path]:
    return sorted(
        p
        for p in root.rglob("*")
        if p.is_file() and not p.name.startswith(".") and p.suffix.lower() in AUDIO_EXTS
    )


def read_codec_and_bitrate(path: Path) -> tuple[str, int | None]:
    codec = path.suffix.lower().lstrip(".").upper()
    kbps: int | None = None
    try:
        info = mutagen.File(path)
        if info and info.info:
            if getattr(info.info, "bitrate", 0):
                kbps = round(info.info.bitrate / 1000)
            raw_codec = str(getattr(info.info, "codec", "") or "").lower()
            if path.suffix.lower() == ".m4a":
                if "alac" in raw_codec:
                    codec = "ALAC"
                elif raw_codec:
                    codec = "AAC"
    except Exception:
        pass
    return codec, kbps


def bitrate_color(codec: str, kbps: int | None) -> tuple[int, int, int]:
    if codec == "ALAC":
        return FG_GREEN
    if kbps is None:
        return FG_PURPLE
    if (codec == "MP3" and kbps >= 300) or (codec == "AAC" and kbps >= 256):
        return FG_BLUE
    return FG_RED


def render_spek(audio_path: Path, out_png: Path) -> None:
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-v",
            "error",
            "-i",
            str(audio_path),
            "-lavfi",
            "showspectrumpic=s=1000x320:color=intensity:scale=log:drange=110,format=gray,negate",
            str(out_png),
        ],
        check=True,
    )
    with Image.open(out_png) as img:
        gray = img.convert("L")
        themed = Image.new("RGB", gray.size)
        themed.putdata([DORIC_LUT[p] for p in gray.getdata()])
        themed.save(out_png)


def load_label_font(size: int = 20) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for font_path in (
        "/System/Library/Fonts/Helvetica.ttc",
        "/System/Library/Fonts/Supplemental/Arial.ttf",
    ):
        if Path(font_path).is_file():
            try:
                return ImageFont.truetype(font_path, size=size)
            except Exception:
                pass
    return ImageFont.load_default()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Randomly pick 3 .mp3/.m4a files and display Doric Siren Purple ffmpeg spectrograms via qlmanage."
    )
    parser.add_argument(
        "root",
        nargs="?",
        default=".",
        help="Root directory to search recursively (default: current directory).",
    )
    args = parser.parse_args()

    root = Path(args.root).resolve()
    files = find_audio_files(root)
    if not files:
        raise SystemExit(f"No .mp3 or .m4a files found under {root}")

    chosen = random.sample(files, min(3, len(files)))
    for i, p in enumerate(chosen, 1):
        codec, kbps = read_codec_and_bitrate(p)
        br = f"{kbps} kbps" if kbps is not None else "? kbps"
        print(f"[{i}] {p.relative_to(root)} ({codec}, {br})")

    temp_dir = Path(tempfile.mkdtemp(prefix="random_spek_"))
    try:
        rendered: list[tuple[Path, str, str, tuple[int, int, int]]] = []
        for i, audio_path in enumerate(chosen, 1):
            out_png = temp_dir / f"spek_{i}.png"
            render_spek(audio_path, out_png)
            codec, kbps = read_codec_and_bitrate(audio_path)
            br_str = f"[{codec} | {kbps} kbps]" if kbps is not None else f"[{codec} | ? kbps]"
            label = f"{i}. {audio_path.name}  "
            rendered.append((out_png, label, br_str, bitrate_color(codec, kbps)))

        header_h = 42
        imgs = [Image.open(item[0]) for item in rendered]
        try:
            width = max(im.width for im in imgs)
            total_h = sum(header_h + im.height for im in imgs)
            canvas = Image.new("RGB", (width, total_h), BG_MAIN)
            draw = ImageDraw.Draw(canvas)
            font = load_label_font(20)

            y = 0
            for idx, (im, (_, label, br_str, br_col)) in enumerate(zip(imgs, rendered)):
                if idx > 0:
                    draw.line([(0, y), (width - 1, y)], fill=BORDER, width=1)
                draw.line([(0, y + header_h - 1), (width - 1, y + header_h - 1)], fill=BORDER, width=1)
                draw.text((16, y + 9), label, fill=FG_MAIN, font=font)
                br_x = 16 + int(draw.textlength(label, font=font))
                draw.text((br_x, y + 9), br_str, fill=br_col, font=font)
                canvas.paste(im, (0, y + header_h))
                y += header_h + im.height

            combined_png = temp_dir / "random_spek_3.png"
            canvas.save(combined_png)
        finally:
            for im in imgs:
                im.close()

        subprocess.run(["qlmanage", "-p", str(combined_png)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


if __name__ == "__main__":
    main()
