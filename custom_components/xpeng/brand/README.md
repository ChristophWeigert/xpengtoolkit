# Brand images

Home Assistant 2026.3 and later serve brand images from a `brand/` directory inside a
custom integration, and those take precedence over anything in the central
[brands repository](https://github.com/home-assistant/brands). These are ours.

## What this is, and what it is not

The mark is **this project's own** - a car with a charge bolt cut out of it - not XPENG's
logo. XPENG's mark is theirs, and there is nothing for us to use in the brands repository:
`custom_integrations/xpeng/icon.png` returns 404 (checked 2026-10-12), which is why an
install shows Home Assistant's placeholder.

If XPENG's own artwork is ever supplied by its owner, drop it in here as `icon.png` /
`logo.png` (and the `@2x` pair) and delete nothing else - the names are what Home Assistant
looks for, and it will pick the vendor mark up with no code change. Nominative use of a
vendor logo to identify which product an integration supports is the norm for integrations
in this ecosystem; the file just has to come from somewhere legitimate rather than be
redrawn by hand.

## Regenerating

The sources are `icon.svg` and `logo.svg` next to this file, kept so the images can be
changed without a design tool. Rasterise with `rsvg-convert` (macOS: `brew install librsvg`):

```bash
rsvg-convert -w 256  icon.svg -o icon.png
rsvg-convert -w 512  icon.svg -o icon@2x.png
rsvg-convert -w 512  logo.svg -o logo.png
rsvg-convert -w 1024 logo.svg -o logo@2x.png
```

Sizes are not arbitrary - they are the
[brand image specification](https://developers.home-assistant.io/docs/core/integration/brand_images):

| File | Requirement |
|---|---|
| `icon.png` | 1:1, exactly 256x256 |
| `icon@2x.png` | 1:1, exactly 512x512 |
| `logo.png` | landscape; shortest side at least 128, at most 256 |
| `logo@2x.png` | landscape; shortest side at least 256, at most 512 |

`tests/test_brand_images.py` checks those numbers, so a careless resize fails the suite
rather than the Marketplace.

There are deliberately **no `dark_` variants**: the tile carries its own contrast, and Home
Assistant falls back to the light images when a dark one is missing.
