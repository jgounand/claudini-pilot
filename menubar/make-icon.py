#!/usr/bin/env python3
"""Draw the app icon and build ClaudiniBar.icns from it.

Only needed when the design changes — the .icns is committed, so installing
needs nothing beyond a shell.

The icon reuses the one thing the app already looks like: the ring in the menu
bar that fills with usage. Anything more detailed turns to mush at sixteen
pixels, which is the size that decides an icon.

    python3 menubar/make-icon.py
"""

import math
import os
import subprocess
import sys
import tempfile

try:
    from PIL import Image, ImageChops, ImageDraw, ImageFilter
except ImportError:
    sys.exit("needs Pillow: python3 -m pip install pillow")

HERE = os.path.dirname(os.path.abspath(__file__))

FINAL = 1024
SUPERSAMPLE = 4                      # draw large, shrink down: free anti-aliasing
CANVAS = FINAL * SUPERSAMPLE
INSET = int(100 * SUPERSAMPLE)       # Apple's grid: artwork sits inside the canvas

SLATE_TOP, SLATE_MID, SLATE_BOTTOM = (0x33, 0x3a, 0x4b), (0x1b, 0x1f, 0x28), (0x0d, 0x0f, 0x14)
GREEN, AMBER, ORANGE = (0x5a, 0xd1, 0x9a), (0xf2, 0xc1, 0x4e), (0xf0, 0x8a, 0x4b)
START, SWEEP = -90, 245              # ~68% round: a gauge in use, neither empty nor spent

SIZES = (16, 32, 128, 256, 512)      # each also emitted at @2x


def lerp(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def rounded_mask(side, radius):
    mask = Image.new("L", (side, side), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, side - 1, side - 1], radius, fill=255)
    return mask


def slate(side):
    """The tile: a diagonal gradient with a little grain, so the surface reads
    as a material rather than a flat fill."""
    tile = Image.new("RGB", (side, side))
    pixels = tile.load()
    for y in range(side):
        t = y / (side - 1)
        shade = (lerp(SLATE_TOP, SLATE_MID, t / .55) if t < .55
                 else lerp(SLATE_MID, SLATE_BOTTOM, (t - .55) / .45))
        for x in range(side):
            pixels[x, y] = shade
    grain = Image.effect_noise((side, side), 10).convert("L")
    return Image.blend(tile, ImageChops.overlay(tile, Image.merge("RGB", (grain,) * 3)), .35)


def gauge(side):
    """The ring, drawn a degree at a time so the colour can travel along it."""
    ring = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    draw = ImageDraw.Draw(ring)
    centre = side // 2
    radius, thickness = int(side * .30), int(side * .155)
    box = [centre - radius, centre - radius, centre + radius, centre + radius]

    draw.arc(box, 0, 360, fill=(255, 255, 255, 38), width=thickness)
    for step in range(SWEEP):
        t = step / SWEEP
        shade = lerp(GREEN, AMBER, t / .55) if t < .55 else lerp(AMBER, ORANGE, (t - .55) / .45)
        draw.arc(box, START + step, START + step + 2, fill=shade + (255,), width=thickness)

    # Pillow thickens an arc inwards, so the stroke's centreline sits at
    # radius - thickness/2 — which is where the round caps belong. Placing them
    # on the radius itself leaves them bulging off the end of the ring.
    spine = radius - thickness / 2
    for angle, shade in ((START, GREEN), (START + SWEEP, ORANGE)):
        rad = math.radians(angle)
        x, y = centre + spine * math.cos(rad), centre + spine * math.sin(rad)
        draw.ellipse([x - thickness / 2, y - thickness / 2,
                      x + thickness / 2, y + thickness / 2], fill=shade + (255,))
    return ring


def artwork():
    side = CANVAS - 2 * INSET
    ring = gauge(side)
    card = slate(side).convert("RGBA")

    # A soft bloom under the ring, then the ring itself over it.
    bloom = Image.blend(Image.new("RGBA", (side, side), (0, 0, 0, 0)),
                        ring.filter(ImageFilter.GaussianBlur(side * .03)), .45)
    card = Image.alpha_composite(card, bloom)
    card = Image.alpha_composite(card, ring)

    # A broad highlight across the top, the way light falls on a curved face.
    sheen = Image.new("L", (side, side), 0)
    ImageDraw.Draw(sheen).ellipse([-side * .3, -side * .8, side * 1.3, side * .40], fill=36)
    card = Image.alpha_composite(card, Image.merge(
        "RGBA", (Image.new("L", (side, side), 255),) * 3
        + (sheen.filter(ImageFilter.GaussianBlur(side * .06)),)))

    mask = rounded_mask(side, int(side * .225))
    card.putalpha(mask)

    out = Image.new("RGBA", (CANVAS, CANVAS), (0, 0, 0, 0))
    shadow = Image.new("RGBA", (CANVAS, CANVAS), (0, 0, 0, 0))
    shadow.paste((0, 0, 0, 110), (INSET, INSET + int(side * .03)), mask)
    out = Image.alpha_composite(out, shadow.filter(ImageFilter.GaussianBlur(side * .03)))
    out.paste(card, (INSET, INSET), card)
    return out.resize((FINAL, FINAL), Image.LANCZOS)


def main():
    master = artwork()
    with tempfile.TemporaryDirectory() as work:
        iconset = os.path.join(work, "ClaudiniBar.iconset")
        os.makedirs(iconset)
        for size in SIZES:
            for scale, suffix in ((1, ""), (2, "@2x")):
                master.resize((size * scale, size * scale), Image.LANCZOS).save(
                    os.path.join(iconset, "icon_%dx%d%s.png" % (size, size, suffix)))
        target = os.path.join(HERE, "ClaudiniBar.icns")
        subprocess.run(["iconutil", "-c", "icns", iconset, "-o", target], check=True)
        print("wrote " + target)


if __name__ == "__main__":
    main()
