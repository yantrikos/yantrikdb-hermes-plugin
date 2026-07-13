#!/usr/bin/env python3
"""Render ``demos/self_directing_memory.py`` into demo.gif (terminal style).

Pure Pillow — no VHS/ffmpeg (VHS hangs on Windows). Runs the demo, captures
its output, and draws a scrolling-reveal terminal GIF (Catppuccin Mocha).

    pip install pillow
    python assets/demos/self-directing/render.py

Requires a Python with `yantrikdb>=0.9.0` + the plugin importable (the demo
uses the embedded engine).
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent.parent
DEMO = REPO / "demos" / "self_directing_memory.py"
OUT = HERE / "demo.gif"

# Catppuccin Mocha
BG, FG, DIM = (30, 30, 46), (205, 214, 244), (108, 112, 134)
GREEN, YELLOW, BLUE, MAUVE, RED = (
    (166, 227, 161), (249, 226, 175), (137, 180, 250),
    (203, 166, 247), (243, 139, 168),
)
FS, LH, PAD, ROWS, COLS = 15, 22, 22, 30, 94
W = PAD * 2 + COLS * 9
H = PAD * 2 + ROWS * LH + 26


def _font(names, size):
    for n in names:
        try:
            return ImageFont.truetype(n, size)
        except OSError:
            continue
    return ImageFont.load_default()


MONO = ["CascadiaMono.ttf", "consola.ttf", "DejaVuSansMono.ttf",
        "/System/Library/Fonts/Menlo.ttc"]
FONT = _font(MONO, FS)
UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f-]{20,}")


def color_for(ln: str):
    s = ln.strip()
    if set(s) == {"-"} and len(s) > 5:
        return DIM
    if s.startswith("## "):
        return MAUVE
    if re.match(r"^\d+\. ", s):
        return BLUE
    if s.startswith("- [") or s.startswith("Open tasks"):
        return GREEN
    if s.startswith("Unanswered") or (s.startswith("- ") and "gap" not in s.lower()):
        return YELLOW
    if "[done]" in s or "task closed" in s or "recall now answers" in s:
        return GREEN
    if "->" in s or s.startswith("Address") or s.startswith("("):
        return DIM
    return FG


def draw(revealed):
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, W, 26], fill=(24, 24, 37))
    for i, c in enumerate((RED, YELLOW, GREEN)):
        d.ellipse([PAD + i * 20, 8, PAD + i * 20 + 11, 19], fill=c)
    d.text((W // 2 - 150, 6), "yantrikdb - self-directing memory", font=FONT, fill=DIM)
    y = 26 + PAD
    for ln in revealed[-ROWS:]:
        d.text((PAD, y), ln, font=FONT, fill=color_for(ln))
        y += LH
    return img


def main() -> int:
    proc = subprocess.run([sys.executable, str(DEMO)], capture_output=True, text=True)
    raw = proc.stdout.splitlines()
    if not raw:
        print("demo produced no output:\n", proc.stderr[-500:]); return 1
    lines = []
    for ln in raw:
        ln = UUID.sub(lambda m: "…" + m.group(0)[-6:], ln.replace("\t", "  "))
        lines.append(ln[:COLS - 1] + "…" if len(ln) > COLS else ln)

    frames, durations, revealed = [], [], []
    for ln in lines:
        revealed.append(ln)
        frames.append(draw(revealed))
        s = ln.strip()
        durations.append(1100 if re.match(r"^\d+\. ", s) else (90 if not s else 240))
    frames.append(draw(revealed)); durations.append(3200)
    frames[0].save(OUT, save_all=True, append_images=frames[1:],
                   duration=durations, loop=0, disposal=2, optimize=True)
    print(f"wrote {OUT} ({len(frames)} frames, {OUT.stat().st_size // 1024} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
