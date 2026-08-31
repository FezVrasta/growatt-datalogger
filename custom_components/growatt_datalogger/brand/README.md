# Brand assets

Home Assistant serves these. Since **2026.3** a custom integration ships its own brand
images in a `brand/` folder inside the integration, and Home Assistant exposes them at
`/api/brands/integration/growatt_datalogger/`, taking priority over the brands CDN. The
central [home-assistant/brands](https://github.com/home-assistant/brands) repository no
longer accepts custom integrations, so there is nothing to submit anywhere — the files
here are the whole story.

See the [Brands Proxy API announcement](https://developers.home-assistant.io/blog/2026/02/24/brands-proxy-api/).

Recognised filenames are `icon.png`, `logo.png`, their `@2x` variants, and `dark_`
prefixed versions of each. This icon is a self-contained dark tile, the way an app icon
is, so it reads correctly on light and dark themes alike and no `dark_` variant is
needed. On Home Assistant older than 2026.3 the integration simply shows the generic
icon; nothing breaks.

## Regenerating

`icon.svg` is generated, not hand-edited. Change `tools/make_icon.py` and re-run it — it
writes the source and renders every PNG:

```sh
python tools/make_icon.py
```

That needs `rsvg-convert` (`brew install librsvg`, `apt install librsvg2-bin`).

## Design

An iOS-style squircle — a superellipse with exponent 5, so the curvature eases into the
straight edges rather than meeting them at a tangent the way a rounded rectangle's
circular arcs do — holding a leaf drawn as a neon tube.

The leaf is built from five passes: two wide blurred blooms, a tighter halo, a soft sheen,
and a crisp near-white core. A single blur reads as a flat coloured smear; it is the white
core inside a saturated halo that makes the stroke look like it is emitting light rather
than merely being green. A radial gradient centred on the leaf's mass, not on the canvas,
spills that light onto the tile, and a vignette darkens the corners so the middle reads as
the bright part.

## Optical centring

The leaf is a blob on the right with a thin stem tailing off to the lower left, so its
bounding box centre is not where the eye puts it. Centring by bounding box leaves the icon
visibly heavy to the right, with dead space in the lower left.

Two measurements, both taken by rendering the artwork and weighing pixels:

| Model | Where its centre sits, relative to the tile centre |
|---|---|
| Bounding box | (+1.5, 0) px — the naive placement was bbox-centred |
| Stroke ink | (+35.6, +19.0) px |
| Filled silhouette | (+51.7, +5.2) px |

The filled-silhouette figure treats the closed leaf outline as solid, because an enclosed
region reads as mass even though only its edge is inked. That is the better model of
perceived weight, but correcting all the way to it crowds the silhouette against one edge,
since a shape's *extent* counts perceptually as well as its weight.

`OPTICAL_CORRECTION = 0.7` splits that. It also happens to land the stroke-ink centroid
within a pixel of centre, which is what the glow settles around, the glow being symmetric
about the stroke. `OPTICAL_RISE = 14` px then lifts the artwork: the stem drags the ink
centroid low, and the perceived centre of a frame sits a little above its geometric
centre, so content centred purely by measurement looks like it has sunk.

The result puts the ink centroid at (511.4, 513.4) in a 1024 tile.

## Two things that will bite you if you edit this

**Blur radii are in the leaf's own coordinate space**, which the placement transform
scales by 12.4. Writing the pixel radius you want directly gives a blur 12.4 times too
large, which overruns its filter region and leaves a hard rectangular seam where it is
clipped. `local()` does the conversion.

**`feComponentTransfer` renders with a visible rectangular seam in librsvg**, which is
what Home Assistant and most Linux tooling use. Intensity comes from stacking two blurred
passes instead. It looks the same and renders consistently.

(And XML comments cannot contain `--`, which is easy to trip over when writing prose into
the generated SVG.)
