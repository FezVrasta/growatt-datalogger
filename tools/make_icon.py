#!/usr/bin/env python3
"""Generate the app icon.

Run this rather than editing the SVG by hand::

    python tools/make_icon.py

which writes the source and renders every PNG Home Assistant looks for.

Two things that are easy to get wrong and are handled here deliberately:

*Blur radii* are expressed in the leaf's own coordinate space, which the placement
transform scales up. Writing the pixel radius you want directly gives a blur that is
``SCALE`` times too large, overruns its filter region, and leaves a hard rectangular seam
where it is clipped. :func:`local` does the conversion.

*Optical centring.* The leaf is a blob on the right with a thin stem tailing off to the
lower left, so its bounding box centre is not where the eye puts it. The placement is
nudged toward the centroid of the shape treated as filled -- an enclosed outline reads as
mass even though only its edge is inked -- by :data:`OPTICAL_CORRECTION`. See
``brand/README.md``.
"""

from __future__ import annotations

import math
import shutil
import subprocess
import sys
from pathlib import Path

SIZE = 1024

#: Apple's app-icon corner, as a fraction of the width, with Figma's equivalent
#: corner-smoothing value. These two numbers *are* the iOS icon shape; see squircle().
CORNER_RADIUS_RATIO = 0.2237
CORNER_SMOOTHING = 0.6
SCALE = 12.4

STEM = "M262,174C262,174 267.028,171.687 273.739,167.063C280.631,162.313 285,157 285,157"
LEAF = (
    "M296,140C296,140 288.486,145.1 281,148C269.412,152.489 270,169 270,169"
    "C270,169 281.243,173.142 291,164C298.833,156.661 296,140 296,140Z"
)

#: The artwork's bounding-box centre, in its own units.
BBOX_CENTRE = (279.07, 156.97)

#: Where the shape's visual mass sits relative to that, measured by rendering the leaf
#: filled and taking the centroid of the result.
MASS_OFFSET = (4.169, 0.415)

#: How far to move toward the mass centroid. Full correction leaves the silhouette
#: noticeably crowded to one side, because the extent of a shape counts for something
#: perceptually as well as its weight; none of it leaves the icon looking right-heavy.
#: At 0.7 the two models happen to agree horizontally -- it puts the stroke-ink centroid
#: within a pixel of centre, which is also what the glow settles around, since the glow
#: is symmetric about the stroke.
OPTICAL_CORRECTION = 0.7

#: Pixels to lift the artwork, at 1024. Two reasons, pulling the same way. The stem
#: tails off below the leaf body, so the ink centroid sits low even once the horizontal
#: correction is applied; and the perceived centre of a frame is a little above its
#: geometric centre, so content centred by measurement alone looks like it has sunk.
OPTICAL_RISE = 14


def local(px: float) -> float:
    """A blur radius in the leaf's units that renders as ``px`` pixels at 1024."""
    return round(px / SCALE, 3)


def squircle(size: float) -> str:
    """Return the iOS app-icon shape as an SVG path, inscribed in a `size` square.

    Not a superellipse. The obvious implementation — `|x|^n + |y|^n = 1` with n
    around 5 — is the shape most people mean by "squircle", but it is not the one
    Apple uses, and the difference is visible: a superellipse curves continuously
    everywhere, so the edges that should be flat bow outward. Side by side against
    a real app icon it reads as pillowed.

    Apple's mask is a *rounded rectangle with continuous curvature*, as produced by
    `UIBezierPath(roundedRect:cornerRadius:)`: genuinely straight edges, with the
    corner easing curvature in over a longer run than a circular arc would. This is
    the construction Figma exposes as "corner smoothing", at the iOS values.

    A circular corner is still wrong for the reason you would expect — curvature
    jumps from 0 to 1/r at the join — but the fix is smoothing the transition, not
    abandoning the straight edge.
    """
    radius = size * CORNER_RADIUS_RATIO
    budget = size / 2

    # Keep the corner inside the space available to it; without the cap a large
    # radius yields a self-intersecting path rather than a clipped one.
    smoothing = min(CORNER_SMOOTHING, budget / radius - 1)
    p = min((1 + smoothing) * radius, budget)

    arc_measure = math.radians(90 * (1 - smoothing))
    arc = math.sin(arc_measure / 2) * radius * math.sqrt(2)

    angle_alpha = (math.pi / 2 - arc_measure) / 2
    angle_beta = math.radians(45 * smoothing)
    c = radius * math.tan(angle_alpha / 2) * math.cos(angle_beta)
    d = c * math.tan(angle_beta)

    # The straight run into each corner splits 2:1 between the two off-curve
    # control points. That ratio is what ramps curvature smoothly.
    b = (p - arc - c - d) / 3
    a = 2 * b

    def f(value: float) -> str:
        return f"{value:.4f}"

    return " ".join(
        [
            f"M {f(size - p)} 0",
            f"c {f(a)} 0 {f(a + b)} 0 {f(a + b + c)} {f(d)}",
            f"a {f(radius)} {f(radius)} 0 0 1 {f(arc)} {f(arc)}",
            f"c {f(d)} {f(c)} {f(d)} {f(b + c)} {f(d)} {f(a + b + c)}",
            f"L {f(size)} {f(size - p)}",
            f"c 0 {f(a)} 0 {f(a + b)} {f(-d)} {f(a + b + c)}",
            f"a {f(radius)} {f(radius)} 0 0 1 {f(-arc)} {f(arc)}",
            f"c {f(-c)} {f(d)} {f(-(b + c))} {f(d)} {f(-(a + b + c))} {f(d)}",
            f"L {f(p)} {f(size)}",
            f"c {f(-a)} 0 {f(-(a + b))} 0 {f(-(a + b + c))} {f(-d)}",
            f"a {f(radius)} {f(radius)} 0 0 1 {f(-arc)} {f(-arc)}",
            f"c {f(-d)} {f(-c)} {f(-d)} {f(-(b + c))} {f(-d)} {f(-(a + b + c))}",
            f"L 0 {f(p)}",
            f"c 0 {f(-a)} 0 {f(-(a + b))} {f(d)} {f(-(a + b + c))}",
            f"a {f(radius)} {f(radius)} 0 0 1 {f(arc)} {f(-arc)}",
            f"c {f(c)} {f(-d)} {f(b + c)} {f(-d)} {f(a + b + c)} {f(-d)}",
            "Z",
        ]
    )


def placement(correction: float = OPTICAL_CORRECTION) -> str:
    origin_x = BBOX_CENTRE[0] + MASS_OFFSET[0] * correction
    origin_y = BBOX_CENTRE[1] + MASS_OFFSET[1] * correction
    return (
        f"translate({SIZE / 2:.0f},{SIZE / 2 - OPTICAL_RISE:.0f}) scale({SCALE}) "
        f"translate({-origin_x:.3f},{-origin_y:.3f})"
    )


def build(correction: float = OPTICAL_CORRECTION) -> str:
    tile = squircle(SIZE)
    place = placement(correction)

    # A filter region large enough to cover the whole tile in the leaf's coordinate
    # space, so a wide blur is never clipped.
    region = 'filterUnits="userSpaceOnUse" x="215" y="95" width="130" height="130"'

    return f"""<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" width="{SIZE}" height="{SIZE}"
     viewBox="0 0 {SIZE} {SIZE}" role="img" aria-label="Growatt Datalogger">
  <title>Growatt Datalogger</title>

  <defs>
    <!-- The light the leaf throws into the tile it sits in. Centred on the leaf's mass,
         which the optical correction leaves slightly right of and above the middle of
         the tile, rather than on the canvas, so the falloff reads as emission coming
         from the shape instead of as a decorative background gradient. -->
    <radialGradient id="spill" cx="51.5%" cy="49%" r="60%">
      <stop offset="0%"   stop-color="#31FF86" stop-opacity="0.30"/>
      <stop offset="38%"  stop-color="#15C062" stop-opacity="0.17"/>
      <stop offset="72%"  stop-color="#083B21" stop-opacity="0.08"/>
      <stop offset="100%" stop-color="#000000" stop-opacity="0"/>
    </radialGradient>

    <radialGradient id="vignette" cx="50%" cy="50%" r="70%">
      <stop offset="45%"  stop-color="#000000" stop-opacity="0"/>
      <stop offset="100%" stop-color="#000000" stop-opacity="0.62"/>
    </radialGradient>

    <linearGradient id="edge" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0%"   stop-color="#FFFFFF" stop-opacity="0.10"/>
      <stop offset="45%"  stop-color="#FFFFFF" stop-opacity="0.02"/>
      <stop offset="100%" stop-color="#FFFFFF" stop-opacity="0"/>
    </linearGradient>

    <linearGradient id="core" x1="0.2" y1="0" x2="0.8" y2="1">
      <stop offset="0%"   stop-color="#FFFFFF"/>
      <stop offset="45%"  stop-color="#D8FFE6"/>
      <stop offset="100%" stop-color="#8CFFB8"/>
    </linearGradient>

    <!-- Bloom, halo, sheen, core. A single blur reads as a flat coloured smear; it is
         the near-white core inside a saturated halo that makes the stroke look lit.
         Intensity comes from stacking passes rather than from feComponentTransfer,
         which librsvg renders with a visible rectangular seam. -->
    <filter id="bloom" {region}>
      <feGaussianBlur stdDeviation="{local(48)}"/>
    </filter>
    <filter id="halo" {region}>
      <feGaussianBlur stdDeviation="{local(13)}"/>
    </filter>
    <filter id="sheen" {region}>
      <feGaussianBlur stdDeviation="{local(4)}"/>
    </filter>

    <clipPath id="tile"><path d="{tile}"/></clipPath>
  </defs>

  <g clip-path="url(#tile)">
    <rect width="{SIZE}" height="{SIZE}" fill="#040805"/>
    <rect width="{SIZE}" height="{SIZE}" fill="url(#spill)"/>

    <g transform="{place}" fill="none" stroke-linecap="round" stroke-linejoin="round">
      <g filter="url(#bloom)" stroke="#1BFF70" stroke-width="4.0" opacity="0.62">
        <path d="{LEAF}"/><path d="{STEM}"/>
      </g>
      <g filter="url(#bloom)" stroke="#3BFF8B" stroke-width="2.9" opacity="0.55">
        <path d="{LEAF}"/><path d="{STEM}"/>
      </g>
      <g filter="url(#halo)" stroke="#5CFF9C" stroke-width="3.0" opacity="0.95">
        <path d="{LEAF}"/><path d="{STEM}"/>
      </g>
      <g filter="url(#sheen)" stroke="#C8FFDD" stroke-width="2.7" opacity="0.9">
        <path d="{LEAF}"/><path d="{STEM}"/>
      </g>
      <g stroke="url(#core)" stroke-width="2.2">
        <path d="{LEAF}"/><path d="{STEM}"/>
      </g>
    </g>

    <rect width="{SIZE}" height="{SIZE}" fill="url(#vignette)"/>
    <!-- A faint top-edge highlight, the way a physical tile catches light. -->
    <path d="{tile}" fill="url(#edge)"/>
  </g>
</svg>
"""


#: Where Home Assistant looks for a custom integration's brand images. Since 2026.3 they
#: ship inside the integration and are served from /api/brands/integration/<domain>/;
#: the central home-assistant/brands repository no longer takes custom integrations.
BRAND_DIR = (
    Path(__file__).resolve().parent.parent / "custom_components" / "growatt_datalogger" / "brand"
)

#: The filenames Home Assistant will serve, and the size to render each at. The icon and
#: the logo are the same artwork here: the mark works alone and there is no wordmark.
RENDERS = {
    "icon.png": 256,
    "icon@2x.png": 512,
    "logo.png": 256,
    "logo@2x.png": 512,
}


def main() -> int:
    BRAND_DIR.mkdir(parents=True, exist_ok=True)
    source = BRAND_DIR / "icon.svg"
    source.write_text(build())
    print(f"wrote {source}")

    if shutil.which("rsvg-convert") is None:
        print("rsvg-convert not found; PNGs not regenerated", file=sys.stderr)
        return 1

    for name, size in RENDERS.items():
        subprocess.run(
            [
                "rsvg-convert",
                "-w",
                str(size),
                "-h",
                str(size),
                str(source),
                "-o",
                str(BRAND_DIR / name),
            ],
            check=True,
        )
        print(f"wrote {BRAND_DIR / name} ({size}px)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
