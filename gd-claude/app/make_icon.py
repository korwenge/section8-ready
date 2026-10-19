"""Draws the app icon (app/icon.ico) so the repo doesn't need a binary file."""

from pathlib import Path

from PIL import Image, ImageDraw


def draw(size):
    s = size / 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([2 * s, 2 * s, 62 * s, 62 * s], radius=12 * s, fill=(22, 25, 34, 255),
                        outline=(182, 242, 61, 255), width=max(1, round(3 * s)))
    # a little GD cube with a spike
    d.rectangle([14 * s, 30 * s, 32 * s, 48 * s], fill=(182, 242, 61, 255))
    d.rectangle([19 * s, 35 * s, 27 * s, 43 * s], fill=(22, 25, 34, 255))
    d.polygon([(36 * s, 48 * s), (50 * s, 48 * s), (43 * s, 30 * s)], fill=(255, 93, 108, 255))
    d.line([(12 * s, 50 * s), (52 * s, 50 * s)], fill=(232, 234, 240, 255), width=max(1, round(2 * s)))
    return img


if __name__ == "__main__":
    out = Path(__file__).with_name("icon.ico")
    draw(256).save(out, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    print(f"wrote {out}")
