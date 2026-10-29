"""Build the app icon -> icon.ico (16..256 px, BMP entries) and icon.png next to this file.

If icon_source.png exists it is used as the artwork (square crop, rounded corners, thin green border),
so you can drop in any picture you like. Otherwise a green "page + magnifier" icon is drawn.

After changing the artwork run register.py again. Explorer caches icons by file path, so if the old
icon keeps showing, give ICON_NAME a new value and re-register.
"""
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

HERE = Path(__file__).parent
SOURCE = HERE / "icon_source.png"
ICON_NAME = "icon"          # -> icon.ico / icon.png
S = 1024                     # working size; downsampled for clean edges
CORNER = 0.18                # corner radius as a fraction of the size
BORDER = 0.035               # green border width as a fraction of the size (photo mode)

GREEN_DARK = (22, 101, 52)
GREEN = (34, 139, 70)
GREEN_LIGHT = (72, 187, 108)
PAGE = (250, 252, 250)
LINE = (120, 180, 135)
GLASS_RIM = (18, 70, 38)
GLASS_FILL = (170, 230, 190, 110)


def _gradient(top, bottom) -> Image.Image:
    grad = Image.new("RGBA", (S, S))
    gd = ImageDraw.Draw(grad)
    for y in range(S):
        t = y / S
        c = tuple(int(top[i] * (1 - t) + bottom[i] * t) for i in range(3))
        gd.line([(0, y), (S, y)], fill=c + (255,))
    return grad


def build_from_image(source: Path) -> Image.Image:
    src = Image.open(source).convert("RGB")
    w, h = src.size
    side = min(w, h)
    left, top = (w - side) // 2, (h - side) // 2
    src = src.crop((left, top, left + side, top + side)).resize((S, S), Image.LANCZOS)

    im = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    r = int(S * CORNER)
    outer = Image.new("L", (S, S), 0)
    ImageDraw.Draw(outer).rounded_rectangle((0, 0, S - 1, S - 1), radius=r, fill=255)
    im.paste(_gradient(GREEN_LIGHT, GREEN_DARK), (0, 0), outer)
    b = int(S * BORDER)
    inner = Image.new("L", (S, S), 0)
    ImageDraw.Draw(inner).rounded_rectangle((b, b, S - 1 - b, S - 1 - b), radius=max(1, r - b), fill=255)
    im.paste(src, (0, 0), inner)
    return im


def build_drawn() -> Image.Image:
    """Green rounded tile, a white page with a folded corner and text lines, and a magnifying glass."""
    im = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    mask = Image.new("L", (S, S), 0)
    ImageDraw.Draw(mask).rounded_rectangle((40, 40, S - 40, S - 40), radius=200, fill=255)
    im.paste(_gradient(GREEN_LIGHT, GREEN_DARK), (0, 0), mask)

    shadow = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    ImageDraw.Draw(shadow).rounded_rectangle((250, 190, 700, 820), radius=28, fill=(0, 0, 0, 70))
    im.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(24)))

    d = ImageDraw.Draw(im)
    fold = 120
    x0, y0, x1, y1 = 230, 170, 680, 800
    d.polygon([(x0, y0), (x1 - fold, y0), (x1, y0 + fold), (x1, y1), (x0, y1)], fill=PAGE)
    d.polygon([(x1 - fold, y0), (x1 - fold, y0 + fold), (x1, y0 + fold)], fill=(205, 225, 210))
    for i, (lx1, ly) in enumerate([(560, 300), (610, 380), (610, 460), (560, 540), (610, 620), (480, 700)]):
        d.rounded_rectangle((290, ly, lx1, ly + 34), radius=17, fill=LINE if i else GREEN)

    cx, cy, r = 640, 640, 170
    glass = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    g = ImageDraw.Draw(glass)
    g.line([(cx + r * 0.72, cy + r * 0.72), (900, 900)], fill=GLASS_RIM, width=84)
    g.ellipse((cx - r, cy - r, cx + r, cy + r), fill=GLASS_FILL, outline=GLASS_RIM, width=46)
    g.arc((cx - r + 60, cy - r + 60, cx + r - 60, cy + r - 60), start=200, end=260, fill=(255, 255, 255, 200), width=22)
    im.alpha_composite(glass)
    return im


def save(big: Image.Image, name: str = ICON_NAME):
    base = big.resize((256, 256), Image.LANCZOS)
    base.save(HERE / f"{name}.png")
    # Save from the 256 px frame: Pillow downsamples to each listed size (it never upsamples, so
    # saving from a small frame silently drops the larger entries). BMP entries for shell compatibility.
    sizes = [16, 20, 24, 32, 40, 48, 64, 96, 128, 256]
    base.save(HERE / f"{name}.ico", format="ICO", sizes=[(s, s) for s in sizes], bitmap_format="bmp")
    print("wrote", HERE / f"{name}.ico", "and", f"{name}.png")


def main():
    if SOURCE.exists():
        save(build_from_image(SOURCE))
    else:
        save(build_drawn())


if __name__ == "__main__":
    main()
