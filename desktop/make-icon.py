#!/usr/bin/env python3
"""Generate the Agent Mesh app icon (Phase 5).

Draws a mesh-network mark — one hub node, five satellite nodes, ring +
spoke links, in the v1 palette — at 1024x1024 with PIL, then renders
the Tauri icon set into src-tauri/icons/: PNGs at 512/256/128/32 and a
multi-size icon.ico. The same geometry is hand-written in icon.svg
next to the outputs, so the mark can be re-drawn at any size.

Run:  python3 desktop/make-icon.py        (requires Pillow)
"""
import math
import os

from PIL import Image, ImageDraw

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "src-tauri", "icons")

BG = (13, 20, 32)            # deep navy, from the v1 palette
RING = (59, 74, 107)         # spoke blue-grey
LINK = (38, 51, 77)          # ring link
HUB = (99, 102, 241)         # indigo
SATELLITES = [(6, 182, 212),     # cyan
              (16, 185, 129),    # emerald
              (245, 158, 11),    # amber
              (168, 85, 247),    # purple
              (244, 63, 94)]     # rose

S = 1024
CENTRE = (512, 512)
RING_R = 300


def satellite_pos(i):
    ang = math.radians(-90 + i * 72)
    return (CENTRE[0] + RING_R * math.cos(ang),
            CENTRE[1] + RING_R * math.sin(ang))


def draw_master():
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([24, 24, S - 24, S - 24], radius=190, fill=BG)
    pts = [satellite_pos(i) for i in range(5)]
    for i in range(5):  # ring links
        d.line([pts[i], pts[(i + 1) % 5]], fill=LINK, width=26)
    for p in pts:       # spokes
        d.line([CENTRE, p], fill=RING, width=30)
    for p, colour in zip(pts, SATELLITES):
        d.ellipse([p[0] - 64, p[1] - 64, p[0] + 64, p[1] + 64],
                  fill=colour, outline=BG, width=10)
    d.ellipse([CENTRE[0] - 96, CENTRE[1] - 96, CENTRE[0] + 96,
               CENTRE[1] + 96], fill=HUB, outline=BG, width=10)
    d.ellipse([CENTRE[0] - 36, CENTRE[1] - 36, CENTRE[0] + 36,
               CENTRE[1] + 36], fill=(248, 250, 252))
    return img


def main():
    os.makedirs(OUT, exist_ok=True)
    master = draw_master()
    names = {512: "icon.png", 256: "128x128@2x.png",
             128: "128x128.png", 32: "32x32.png"}
    for size, name in names.items():
        master.resize((size, size), Image.LANCZOS).save(
            os.path.join(OUT, name))
    master.resize((256, 256), Image.LANCZOS).save(
        os.path.join(OUT, "icon.ico"), format="ICO",
        sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128),
               (256, 256)])
    print(f"icons written to {OUT}: " +
          ", ".join(sorted(os.listdir(OUT))))


if __name__ == "__main__":
    main()
