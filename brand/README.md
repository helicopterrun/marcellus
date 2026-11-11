# Marcellus icon: Ghost on the wall

A sheet-ghost peering through a crenel in the battlements of Elsinore. It's what the watch exists to see.

## Files

| File | Use |
| --- | --- |
| `svg/marcellus-mark.svg` | Master mark, transparent. Full colour. |
| `svg/marcellus-tile.svg` | Master app tile, full bleed (wall runs to the bottom edge). |
| `svg/marcellus-mark-amber.svg` / `-white.svg` / `-black.svg` | One-colour mark (see note below). |
| `svg/marcellus-maskable.svg` | Source for the Android maskable icon. |
| `svg/social-card.svg` | 1280 × 640 GitHub preview. Wordmark is set in Departure Mono. |
| `web/favicon.svg` | Modern browsers. |
| `web/favicon.ico` | 16 + 32, for legacy browsers. |
| `web/favicon-16.png`, `favicon-32.png` | Exact pixel renders: 1 and 2 px per cell. |
| `web/apple-touch-icon.png` | 180 × 180, opaque. |
| `web/icon-192.png`, `icon-512.png` | Manifest icons. |
| `web/icon-maskable-512.png` | Manifest, `purpose: maskable`. |
| `png/*` | 512 px transparent marks (colour, amber, white) and a 1024 px tile. |

## Grid and proportions (for Elsinore and Bernardo)

- **Grid:** 16 × 16 cells. The 16 px favicon is the master drawing, so there's no separate hand-tuning step: 16 px = 1 px per cell, 32 px = 2 px per cell. Draw siblings on the same grid.
- **Edges:** only square cells. Every SVG uses `shape-rendering="crispEdges"`. No curves, no anti-aliasing.
- **The wall:** a full wall that is 4 cells tall (rows 11–14), plus merlons 3 cells wide and 2 cells tall (rows 9–10). Each merlon sits 1 cell away from what's between them. Reuse this exact wall in sibling icons so all three stand on the same battlement.
- **Subject:** goes in the 8 cells between the merlons (cols 4–11), with its top at row 2. Elsinore could put a tower or keep there, and Bernardo a second sentry (or an animal) on the same wall.
- **Margins:** the transparent mark leaves rows 0–1 and row 15 empty. On the tile, the wall runs into row 15 so it bleeds off the bottom.
- **Maskable:** drawn at 26 px per cell on 512, centred. The ghost stays within about 165 px of centre, inside the 205 px safe circle. The outer merlons and the wall run out to the edges, so any mask crops only wall.

## Colour

| Role | Hex |
| --- | --- |
| Ghost | `#ECE9E3` warm white |
| Wall | `#E2AA52` amber |
| Tile / night sky / eyes | `#111216` panel |

The eyes are holes, so they always show the background.

**One-colour version:** in a single colour the ghost and wall would merge into one blob. In the mono marks, the ghost's body stops one cell short and floats above the wall. That's the only change.

## HTML

```html
<link rel="icon" href="/favicon.ico" sizes="32x32">
<link rel="icon" href="/favicon.svg" type="image/svg+xml">
<link rel="apple-touch-icon" href="/apple-touch-icon.png">
<link rel="manifest" href="/manifest.webmanifest">
<meta name="theme-color" content="#111216">
```

```json
"icons": [
  { "src": "/icon-192.png", "sizes": "192x192", "type": "image/png" },
  { "src": "/icon-512.png", "sizes": "512x512", "type": "image/png" },
  { "src": "/icon-maskable-512.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable" }
],
"background_color": "#111216",
"theme_color": "#111216"
```

## Notes

- **Social card PNG:** now rendered with Departure Mono as `png/social-card-1280x640.png`.
- **Light browser themes:** the favicon is a dark tile, so it stays readable on light tab bars. If you'd rather have a transparent favicon, use `marcellus-mark.svg`. Keep in mind the white ghost disappears on a light tab bar.
