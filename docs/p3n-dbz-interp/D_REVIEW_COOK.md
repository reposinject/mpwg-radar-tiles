# D review cook — NOT PRODUCTION

James GO: put monotone cubic Hermite on a labeled interactive review path. The sampler is `sample_monotone_pchip` from the final harness, unchanged. Production stays flag-off = p3l.

Do not merge this branch for a production ship. Do not set `MPWG_RALA_DBZ_INTERP` in `/etc/mpwg-radar.env`. Do not add it to `mpwg-radar-cooker-rala.service`. The production timer must keep cooking p3l onto `rala/clean/latest`.

## Flag

| | `MPWG_RALA_DBZ_INTERP` | Where tiles go |
| --- | --- | --- |
| Production | omit, or `off` / `p3l` / empty | `rala/clean/latest/` |
| A | `bilinear_peak_hold` | `rala/clean/` — do not upload |
| B | `tight_peak_hold` | `rala/clean/` — do not upload |
| D | `monotone_pchip` (hyphen form accepted) | `rala-review/monotone_pchip/clean/` |

`mode_spec.spatial` stays `p3l`. Palette stays `2026-09-rala-p3k`. Category, NO-ECHO, and the clear-air alpha stay on the p3l splat. Only the numerical dBZ array changes, and only before `palette.colorize`.

## Paths

Consumer CDN (unchanged by a D cook):

```
https://YOUR_DOMAIN/radar/rala/clean/latest/{z}/{x}/{y}.png
```

`manifest.json` → `products.rala.latest` stays that template.

Labeled D review (point the map here for the interactive test):

```
https://YOUR_DOMAIN/radar/rala-review/monotone_pchip/clean/latest/{z}/{x}/{y}.png
https://YOUR_DOMAIN/radar/rala-review/monotone_pchip/clean/latest/frame.json
```

With `R2_PREFIX=radar`, the object key is `radar/` plus that relative path. MapLibre: `tileSize: 512`. This is still native z9. Mapbox will linearly filter those pixels past zoom 9; that display filter is outside this cook.

`products.rala.review` on a manifest merged from the live radar root records the same template and `dbz_interp: monotone_pchip`, with `label: NOT PRODUCTION`. A cook whose output tree is not the live root uploads the review tiles and does not PUT `manifest.json`, so a side directory cannot replace the CDN manifest.

## One frame, no upload (local check)

```bash
cd /opt/mpwg-radar   # this branch, not a production env edit
MPWG_RALA_DBZ_INTERP=monotone_pchip MPWG_UPLOAD=false \
  mpwg-radar cook --product rala --grib /path/to/frame.grib2.gz \
  --out output/d-review
```

Verify:

```bash
jq '.mode_spec.dbz_interp, .mode_spec.spatial, .palette.version, .tile_url_template' \
  output/d-review/radar/rala-review/monotone_pchip/clean/latest/frame.json
# "monotone_pchip"
# "p3l"
# "2026-09-rala-p3k"
# "rala-review/monotone_pchip/clean/latest/{z}/{x}/{y}.png"

test ! -f output/d-review/radar/rala/clean/latest/frame.json
jq '.products.rala.latest, .products.rala.review.latest' \
  output/d-review/radar/manifest.json
# "rala/clean/latest/{z}/{x}/{y}.png"
# "rala-review/monotone_pchip/clean/latest/{z}/{x}/{y}.png"
```

## Interactive upload from the live host

Run this by hand from this branch. Do not `systemctl edit` the RALA unit. Do not export the variable into the service environment.

```bash
cd /opt/mpwg-radar
sudo -u mpwg env \
  MPWG_RALA_DBZ_INTERP=monotone_pchip \
  MPWG_PRODUCT=rala \
  /opt/mpwg-radar/.venv/bin/mpwg-radar cook --product rala --grib /path/to/frame.grib2.gz
```

That uses the live output directory and R2 settings already in the process environment for this one command. Tiles upload under `rala-review/monotone_pchip/`. Because `rala/clean/latest/frame.json` is already on that tree, the manifest PUT is allowed and only adds `products.rala.review`. It does not change `products.rala.latest`.

Omit `--grib` only when James wants the review loop filled. That invocation uses the existing RALA archive and writes every pending scan into the review prefix. It does not retile `rala/clean/`. It is a long CONUS cook. It is not a second systemd timer.

After upload (`$BASE` is `R2_PUBLIC_BASE_URL`, prefix `radar` already in the URL if that is how the worker is mounted):

```bash
curl -fsS "$BASE/radar/rala-review/monotone_pchip/clean/latest/frame.json" \
  | jq '.mode_spec.dbz_interp, .mode_spec.spatial, .palette.version'
curl -fsS "$BASE/radar/manifest.json" \
  | jq '.products.rala.latest, .products.rala.review.latest, .products.rala.review.dbz_interp'
curl -fsS -o /dev/null -w '%{http_code}\n' \
  "$BASE/radar/rala/clean/latest/frame.json"
```

Expect `monotone_pchip`, `p3l`, `2026-09-rala-p3k` on the review frame. Expect `products.rala.latest` to stay `rala/clean/latest/{z}/{x}/{y}.png`. The consumer frame URL should still be HTTP 200.

Point the MPWG map tile source at the review template for the labeled test. Leave the consumer source on `rala/clean/latest`. Revert the map when the review is done. To stop serving D, delete the `rala-review/monotone_pchip/` prefix on R2. The production timer does not write that prefix.

## What this host cannot finish

The map app is not in this repo. EC2 has to run the cook against live R2 credentials from this branch. Lovable (or whoever owns the map style) has to set the review tile URL above. This cooker does not edit `/etc/mpwg-radar.env` and does not start the production service with D.
