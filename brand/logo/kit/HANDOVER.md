# OpenPiso WiFi logo — handover

## Rationale
Chosen from five rounds. Rafael's sketch put a wifi signal inside an open P. Merging the O into the P's bowl gave **OpenPiso in one glyph**. Adding a dot at the coin's centre made it read as a broadcast source. The larger dot was picked because it holds up better at small sizes.

## Colour
The palette started as Ink + Piso Orange and was switched to the OpenPiso client greens (Forest `#123C1C`, Mint `#71DA88`), plus Leaf `#2E9E4F` for the signal on white. The app icon and favicon tile are Mint on Forest, matching the OpenPiso favicon.

## Test results
- `svg_audit.py`: symbol 99/100, small cut 99/100, wordmark 97/100. No live text, rasters, filters, or masks. The w strokes are snapped to exactly 15°.
- True-size renders at 16/32 px: the full symbol's waves merge at 16 px, so the **small cut** (one wave, heavier strokes) is used for favicon, web icons, and the app icon.
- One-colour black/white and reversed versions were checked visually.

## Open items
- **Trademark search.** A `p` followed by waves resembles contactless-payment and NFC marks. Search (e.g. IPOPHL and the WIPO Global Brand Database) and run a reverse image search before wide use. Nobody has done this yet.
- **Print proofs.** CMYK/Pantone values are approximations. No PDF/EPS masters were produced. Export them from the SVGs (e.g. Inkscape) when a printer needs them.
- **Slide PNGs.** `presentation.html` (6 mockups: portal site, vendo sticker, signage, README, favicon tab, app icon) has to be opened in a browser. PNG export needs Chrome, which wasn't available.
- **Reversed thinning.** White-on-Ink geometry was not optically thinned. It's fine at normal sizes; revisit for large signage.
