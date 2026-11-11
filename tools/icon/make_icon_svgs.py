"""Generate the Marcellus icon SVGs (ghost over a battlement) from a 16x16 pixel map.

Usage: python tools/icon/make_icon_svgs.py <out_dir>
Writes favicon.svg (16-unit grid, rounded tile; also the docs logo) and
app-tile.svg (full-bleed square with the art inset for home-screen rounding
and Android's maskable safe zone). PNGs in src/marcellus/static/icons were
rasterized from these at 16/32/180/192/512 px with nearest-neighbour scaling.
"""
import sys
from pathlib import Path

BG, GHOST, WALL = "#111216", "#ECE9E3", "#E2AA52"  # panel, warm white, amber

# 16x16 pixel map. G ghost, e eye (background), W wall, . empty
ART = [
"................",
"................",
"......GGG.......",
".....GGGGG......",
"....GGGGGGG.....",
"....GGeGeGG.....",
"....GGGGGGG.....",
"...GGGGGGGG.....",
"....GGGGGGGG....",
".....GGGGGGG....",
".....GG.GG.GG...",
"................",
".WWW..WWWW..WWW.",
".WWW..WWWW..WWW.",
"WWWWWWWWWWWWWWWW",
"WWWWWWWWWWWWWWWW",
]
COL = {"G": GHOST, "W": WALL, "e": BG}


def rects(ox: int = 0, oy: int = 0) -> str:
    """One <rect> per horizontal run of same-coloured pixels."""
    out = []
    for y, row in enumerate(ART):
        x = 0
        while x < 16:
            c = row[x]
            if c not in COL:
                x += 1
                continue
            x2 = x
            while x2 < 16 and row[x2] == c:
                x2 += 1
            out.append(
                f'<rect x="{x + ox}" y="{y + oy}" width="{x2 - x}" height="1" fill="{COL[c]}"/>'
            )
            x = x2
    return "\n  ".join(out)


def svg(view: int, body: str) -> str:
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {view} {view}" '
        f'shape-rendering="crispEdges">\n  {body}\n</svg>\n'
    )


def favicon() -> str:
    # Exact 16-unit grid on a rounded tile: crisp at 16 and 32 px.
    return svg(
        16,
        f'<rect width="16" height="16" rx="3" fill="{BG}"/>\n'
        f'  <g clip-path="url(#c)">{rects()}</g>\n'
        '  <clipPath id="c"><rect width="16" height="16" rx="3"/></clipPath>',
    )


def app_tile() -> str:
    # Full-bleed square, art inset by 2 units; the wall runs out to the edges.
    edges = (
        f'<rect x="0" y="16" width="2" height="4" fill="{WALL}"/>'
        f'<rect x="18" y="16" width="2" height="4" fill="{WALL}"/>'
        f'<rect x="0" y="18" width="20" height="2" fill="{WALL}"/>'
    )
    return svg(20, f'<rect width="20" height="20" fill="{BG}"/>\n  {rects(2, 2)}\n  {edges}')


if __name__ == "__main__":
    out = Path(sys.argv[1])
    (out / "favicon.svg").write_text(favicon())
    (out / "app-tile.svg").write_text(app_tile())
