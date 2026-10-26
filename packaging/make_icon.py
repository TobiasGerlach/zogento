"""Erzeugt das App-Icon: ein Bücherstapel auf dunkelblauem Grund.

    python packaging/make_icon.py        -> packaging/assets/icon.png, icon.ico (+ icon.icns auf macOS)
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw

ASSETS = Path(__file__).resolve().parent / "assets"
S = 1024


def draw() -> Image.Image:
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((40, 40, S - 40, S - 40), radius=200, fill=(28, 52, 92, 255))
    # drei liegende Bücher, leicht versetzt
    books = [
        ((190, 640, 834, 760), (196, 120, 60)),
        ((230, 520, 800, 640), (226, 178, 84)),
        ((170, 400, 850, 520), (122, 160, 120)),
    ]
    for (x0, y0, x1, y1), color in books:
        d.rounded_rectangle((x0, y0, x1, y1), radius=26, fill=color + (255,))
        d.rectangle((x1 - 70, y0 + 16, x1 - 20, y1 - 16), fill=(246, 240, 226, 255))  # Seitenschnitt
        d.line((x0 + 60, y0 + 20, x0 + 60, y1 - 20), fill=(255, 255, 255, 90), width=10)
    # aufrecht stehendes Buch obendrauf
    d.rounded_rectangle((420, 190, 560, 400), radius=20, fill=(176, 84, 72, 255))
    d.rectangle((440, 220, 540, 240), fill=(246, 240, 226, 255))
    return img


def main() -> int:
    ASSETS.mkdir(parents=True, exist_ok=True)
    img = draw()
    img.save(ASSETS / "icon.png")
    img.save(ASSETS / "icon.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    if sys.platform == "darwin" and shutil.which("iconutil"):
        with tempfile.TemporaryDirectory() as tmp:
            iconset = Path(tmp) / "icon.iconset"
            iconset.mkdir()
            for size in (16, 32, 128, 256, 512):
                img.resize((size, size), Image.LANCZOS).save(iconset / f"icon_{size}x{size}.png")
                img.resize((size * 2, size * 2), Image.LANCZOS).save(iconset / f"icon_{size}x{size}@2x.png")
            subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(ASSETS / "icon.icns")], check=True)
    print("Icons in", ASSETS)
    return 0


if __name__ == "__main__":
    sys.exit(main())
