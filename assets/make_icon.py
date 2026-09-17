"""
Generate assets/sadhguru_vo.ico for the Sadhguru VO app.

Design: a rounded deep-indigo tile (matching the app's own panel colour), a gold
outer arc and a violet inner arc reading as the two pipeline steps, a soft
sound-wave running through the middle, and a small gold dot for the voice
source. Rendered at 4x and downsampled per size so the small icons stay crisp.

Build-time only — Pillow is not a runtime dependency of the app.
"""

import math
import os

from PIL import Image, ImageDraw

OUT = r"D:\AI Automation\Sadhguru VO\assets\sadhguru_vo.ico"
SIZES = [16, 24, 32, 48, 64, 128, 256]

BG_OUTER = (30, 27, 58, 255)      # #1e1b3a  app panel indigo
BG_INNER = (17, 24, 39, 255)      # #111827  panel
GOLD     = (250, 204, 21, 255)    # #facc15  step 1
VIOLET   = (167, 139, 250, 255)   # #a78bfa  step 2
GREEN    = (34, 197, 94, 255)     # #22c55e  success accent
WAVE     = (226, 232, 240, 255)   # #e2e8f0  text


def render(px: int) -> Image.Image:
    S = px * 4                                  # supersample
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # ── rounded tile with a subtle vertical gradient ─────────────────────────
    pad = S * 0.045
    r = S * 0.22
    tile = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    td = ImageDraw.Draw(tile)
    for y in range(S):
        t = y / max(1, S - 1)
        td.line([(0, y), (S, y)], fill=tuple(
            int(BG_OUTER[i] + (BG_INNER[i] - BG_OUTER[i]) * t) for i in range(4)))
    mask = Image.new("L", (S, S), 0)
    ImageDraw.Draw(mask).rounded_rectangle([pad, pad, S - pad, S - pad],
                                          radius=r, fill=255)
    img.paste(tile, (0, 0), mask)

    # thin violet rim so the tile reads on both light and dark taskbars
    d.rounded_rectangle([pad, pad, S - pad, S - pad], radius=r,
                        outline=(91, 79, 191, 255), width=max(1, int(S * 0.012)))

    cx = cy = S / 2

    # ── one ring split into the two pipeline steps ───────────────────────────
    # Gold sweeps the top half (step 1, TTS), violet the bottom half (step 2,
    # voice change) — same radius, so they read as a single cycle rather than
    # two unrelated arcs. Gaps at 3 and 9 o'clock leave room for the endpoints.
    rad = S * 0.295
    rw  = max(2, int(S * 0.058))
    box = [cx - rad, cy - rad, cx + rad, cy + rad]
    gap = 14                                     # degrees of gap on each side
    d.arc(box, start=180 + gap, end=360 - gap, fill=GOLD,   width=rw)
    d.arc(box, start=0 + gap,   end=180 - gap, fill=VIOLET, width=rw)

    # ── sound wave through the middle ───────────────────────────────────────
    # A single smooth stroke — at 16px anything busier turns to mush.
    amp  = S * 0.115
    half = S * 0.175
    pts = []
    steps = 96
    for i in range(steps + 1):
        t = i / steps
        x = cx - half + (2 * half) * t
        # Envelope tapers both ends so the wave starts and finishes on the
        # centre line, level with the two endpoint dots.
        env = math.sin(math.pi * t) ** 0.6
        y = cy - math.sin(t * math.pi * 2.0) * amp * env
        pts.append((x, y))
    d.line(pts, fill=WAVE, width=max(1, int(S * 0.034)), joint="curve")

    # ── endpoint dots: gold = script in, green = audio out ───────────────────
    rd = S * 0.050
    for x, col in ((cx - rad, GOLD), (cx + rad, GREEN)):
        d.ellipse([x - rd, cy - rd, x + rd, cy + rd], fill=col)

    return img.resize((px, px), Image.LANCZOS)


def main() -> None:
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    frames = [render(s) for s in SIZES]
    frames[-1].save(OUT, format="ICO",
                    sizes=[(s, s) for s in SIZES],
                    append_images=frames[:-1])
    print(f"wrote {OUT} ({os.path.getsize(OUT)} bytes, sizes={SIZES})")
    # Also drop a PNG preview so the design can be eyeballed at full size.
    png = os.path.splitext(OUT)[0] + "_preview.png"
    render(256).save(png)
    print(f"wrote {png}")


if __name__ == "__main__":
    main()
