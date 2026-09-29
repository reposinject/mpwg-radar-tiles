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

`products.rala.review` on a manifest merged from the live radar root records the same template, `dbz_interp: monotone_pchip`, `label: NOT PRODUCTION`, and `frames` (newest first). A cook whose output tree is not the live root uploads the review tiles and does not PUT `manifest.json`, so a side directory cannot replace the CDN manifest.

## Short loop

Production RALA keeps the 75-minute archive. A D cook does not enter that archive. With no count, D cooks one latest frame. The short loop is one-off:

```bash
sudo -u mpwg env \
  MPWG_RALA_DBZ_INTERP=monotone_pchip \
  MPWG_RALA_REVIEW_FRAMES=8 \
  MPWG_PRODUCT=rala \
  /opt/mpwg-radar/.venv/bin/mpwg-radar cook --product rala --review-frames 8
```

`--review-frames` and `MPWG_RALA_REVIEW_FRAMES` are the same knob. Use either. Do not put either variable in `/etc/mpwg-radar.env` or `mpwg-radar-cooker-rala.service`. The count is ignored unless the interp flag is `monotone_pchip`, so a stray value cannot thin the production archive.

The cook lists NOAA RALA scans from the last 30 minutes, takes the newest N (capped at 12; 8 is the loop), and writes each under `rala-review/monotone_pchip/clean/{frameId}/`. `latest/` stays the newest of those. It does not keep listing after that set, and it does not install a timer.

`products.rala.review.frames` matches `products.rala.modes.clean.frames`. The app reads `id` and `tiles`:

```json
{
  "label": "NOT PRODUCTION",
  "dbz_interp": "monotone_pchip",
  "spatial": "p3l",
  "latest": "rala-review/monotone_pchip/clean/latest/{z}/{x}/{y}.png",
  "latest_frame": "20260929T210000Z",
  "frame_json": "rala-review/monotone_pchip/clean/latest/frame.json",
  "consumer_latest": "rala/clean/latest/{z}/{x}/{y}.png",
  "frames": [
    {
      "id": "20260929T210000Z",
      "valid_time": "2026-09-29T21:00:00+00:00",
      "tiles": "rala-review/monotone_pchip/clean/20260929T210000Z/{z}/{x}/{y}.png",
      "frame": "rala-review/monotone_pchip/clean/20260929T210000Z/frame.json"
    }
  ]
}
```

`products.rala.latest` stays `rala/clean/latest/{z}/{x}/{y}.png`. Production `frame.json` has no `dbz_interp`.

After the cook:

```bash
curl -fsS "$BASE/radar/manifest.json" | jq '.products.rala.latest, .products.rala.review.frames'
curl -fsS "$BASE/radar/rala/clean/latest/frame.json" | jq '.mode_spec.dbz_interp, .mode_spec.spatial'
curl -fsS "$BASE/radar/rala-review/monotone_pchip/clean/latest/frame.json" | jq '.mode_spec.dbz_interp'
```

Expect the consumer `dbz_interp` key to be absent (or null) and spatial `p3l`. Expect `review.frames | length` to be the number of scans in the last 30 minutes, at most N. Point `?ralaReview=d` at that manifest. The consumer map stays on `rala/clean/latest`.

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

Omit `--grib` and omit `--review-frames` to cook one latest scan onto the review prefix. That does not fill the loop and does not enter the production 75-minute archive. The short loop is `--review-frames 8` (see above). It is not a second systemd timer.

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
