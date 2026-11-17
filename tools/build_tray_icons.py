"""Rebuild the checked-in tray ICO files (build tool only; requires Pillow)."""
from pathlib import Path

from PIL import Image


ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "assets" / "tray"
SIZES = (16, 20, 24, 32, 40, 48, 64)


def frame(source, size, dim=False):
    scale = 4
    side = size * scale
    image = source.resize((side, side), Image.Resampling.LANCZOS)
    if dim:
        image.putalpha(image.getchannel("A").point(lambda value: round(value * 0.18)))
    return image.resize((size, size), Image.Resampling.LANCZOS)


def main():
    source = Image.open(ASSETS / "wechat-source.ico").convert("RGBA")
    for name, dim in (("normal", False), ("dim", True)):
        frames = [frame(source, size, dim) for size in SIZES]
        frames[-1].save(ASSETS / (name + ".ico"), sizes=[(size, size) for size in SIZES],
                        append_images=frames[:-1])


if __name__ == "__main__":
    main()
