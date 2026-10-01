# STRUCTURE review cook — NOT PRODUCTION

James PASS (2026-10-01): STRUCTURE V1 is approved for a gated live review only. FIX2 is the immediate fallback on the same gate. The Belle bump soft-fail is documented and accepted. This cook does not retune σ, residual boosts, the 0.5 dBZ step, the palette, or NO-ECHO. There is no production switch.

Do not merge this branch for a production ship. Do not set `MPWG_RALA_DBZ_INTERP` in `/etc/mpwg-radar.env`. Do not add it to `mpwg-radar-cooker-rala.service`. The production timer must keep cooking p3l onto `rala/clean/latest`.

`monotone_pchip` (D) and the A/B local-on-`rala` tokens live on a separate unmerged draft. This branch does not implement them. An unknown token raises so a typo cannot silently paint production p3l.

## Flag

| | `MPWG_RALA_DBZ_INTERP` | Where tiles go |
| --- | --- | --- |
| Production | omit, or `off` / `p3l` / `0` / `none` / `false` / empty | `rala/clean/latest/` |
| STRUCTURE V1 (leading) | `structure_v1` (`structure-v1` accepted) | `rala-review/structure_v1/clean/` |
| FIX2 (fallback) | `fix2` | `rala-review/fix2/clean/` |

`mode_spec.spatial` stays `p3l`. Palette stays `2026-09-rala-p3k`. Native zoom stays z9. Category, NO-ECHO, and the clear-air alpha stay on the p3l splat. Only the numerical dBZ array changes, and only before `palette.colorize`. Display bands are hard-quantized to 0.5 dBZ (half up). There is no RGB blur, no sharpen, and no z10.

## What the sampler is

Offline STRUCTURE is a dense neighborhood hybrid, not a per-query PCHIP sample. The live tile path evaluates that same function at each pixel's native fractional index on the stored lat/lon axes, then quantizes. It does not build a CONUS-wide dense lattice.

Locks, unchanged from offline acceptance:

- FIX2 trend: 5×5 Gaussian local plane, σ = **0.55** cells, `soft_mode=no_special` (numerical floor 1e-12 only). Soft knots: centers are not snapped back to native dBZ.
- Residual: CLUSTER bridges, `continuous_add`, `d2_floor = (1/spc)²`. Live tiles lock `spc` to the core acceptance value **32**, so the floor stays `(1/32)² = 0.0009765625`. The wide offline grid (`spc=16`) was display-only.
- STRUCTURE V1: that FIX2 trend stays frozen. Multi-member cluster residual amplitude ×**1.55**, singleton ×**0.25** (`min_cluster_size_for_boost=2`). FIX2 leaves both scales at 1.
- Clamp to the contributing 5×5 min/max. NO-ECHO is hard: nearest native cell not valid echo → the query is not a sample.

## Paths

Consumer CDN (unchanged by a STRUCTURE or FIX2 cook):

```
https://YOUR_DOMAIN/radar/rala/clean/latest/{z}/{x}/{y}.png
```

`manifest.json` → `products.rala.latest` stays that template. `products.rala.modes` and the production `latest_frame` are left as the last production cook wrote them.

Labeled review (point the map here for the interactive test):

```
https://YOUR_DOMAIN/radar/rala-review/structure_v1/clean/latest/{z}/{x}/{y}.png
https://YOUR_DOMAIN/radar/rala-review/structure_v1/clean/latest/frame.json
```

FIX2 fallback uses the same shape with `fix2` in place of `structure_v1`.

With `R2_PREFIX=radar`, the object key is `radar/` plus that relative path. MapLibre: `tileSize: 512`. This is still native z9. Mapbox will linearly filter those pixels past zoom 9; that display filter is outside this cook.

`products.rala.review` is the cook that just updated the manifest: `label: NOT PRODUCTION`, `dbz_interp`, `spatial: p3l`, `palette_version`, `latest`, `latest_frame`, `frame_json`, `consumer_latest`, `frames`. `products.rala.reviews.<token>` keeps both tokens, so a FIX2 cook does not erase STRUCTURE.

A cook whose output tree does not already have consumer `products.rala.latest` **and** `rala/clean/latest/frame.json` uploads the review tiles and does not PUT `manifest.json`. A side directory cannot replace the CDN manifest. The local manifest is still written, and `products.rala.latest` in that file is the consumer template even when the consumer tiles were not cooked into that tree.

Review status is `status-rala-review-<token>.json`. The production `status-rala.json` is not rewritten. The publish marker is `.published-rala-review-<token>`, not `.published-rala`.

## One frame, no upload (local check)

```bash
cd /opt/mpwg-radar   # this branch, not a production env edit
MPWG_RALA_DBZ_INTERP=structure_v1 MPWG_UPLOAD=false \
  mpwg-radar cook --product rala --grib /path/to/frame.grib2.gz \
  --out output/structure-review
```

Verify:

```bash
jq '.mode_spec.dbz_interp, .mode_spec.spatial, .palette.version, .tile_url_template' \
  output/structure-review/radar/rala-review/structure_v1/clean/latest/frame.json
# "structure_v1"
# "p3l"
# "2026-09-rala-p3k"
# "rala-review/structure_v1/clean/latest/{z}/{x}/{y}.png"

test ! -f output/structure-review/radar/rala/clean/latest/frame.json
jq '.products.rala.latest, .products.rala.review.latest, .products.rala.review.dbz_interp' \
  output/structure-review/radar/manifest.json
# "rala/clean/latest/{z}/{x}/{y}.png"
# "rala-review/structure_v1/clean/latest/{z}/{x}/{y}.png"
# "structure_v1"
```

FIX2 is the same command with `MPWG_RALA_DBZ_INTERP=fix2`. The frame lands at `rala-review/fix2/clean/latest/frame.json`. If both tokens have been cooked into one tree, `products.rala.review` names the cook that just finished and `products.rala.reviews.structure_v1` plus `products.rala.reviews.fix2` both remain.

## Interactive upload from the live host

Run this by hand from this branch. Do not `systemctl edit` the RALA unit. Do not export the variable into the service environment. Do not put it in `/etc/mpwg-radar.env`.

STRUCTURE (leading):

```bash
cd /opt/mpwg-radar
sudo -u mpwg env \
  MPWG_RALA_DBZ_INTERP=structure_v1 \
  MPWG_PRODUCT=rala \
  /opt/mpwg-radar/.venv/bin/mpwg-radar cook --product rala --grib /path/to/frame.grib2.gz
```

FIX2 fallback is the same command with `MPWG_RALA_DBZ_INTERP=fix2`.

That uses the live output directory and R2 settings already in the process environment for this one command. Tiles upload under `rala-review/<token>/`. Because `rala/clean/latest/frame.json` is already on that tree, the manifest PUT is allowed and only adds `products.rala.review` plus `products.rala.reviews.<token>`. It does not change `products.rala.latest`.

Omit `--grib` only when James wants the review archive filled from live MRMS. That invocation uses the existing 75-minute / 60-frame RALA window and writes every pending scan into the review prefix. It does not retile `rala/clean/`. It is a long CONUS cook. It is not a second systemd timer.

After upload (`$BASE` is `R2_PUBLIC_BASE_URL`, prefix `radar` already in the URL if that is how the worker is mounted):

```bash
curl -fsS "$BASE/radar/rala-review/structure_v1/clean/latest/frame.json" \
  | jq '.mode_spec.dbz_interp, .mode_spec.spatial, .palette.version'
curl -fsS "$BASE/radar/manifest.json" \
  | jq '.products.rala.latest, .products.rala.review.latest, .products.rala.review.dbz_interp, .products.rala.reviews.structure_v1.latest'
curl -fsS -o /dev/null -w '%{http_code}\n' \
  "$BASE/radar/rala/clean/latest/frame.json"
```

Expect `structure_v1`, `p3l`, `2026-09-rala-p3k` on the review frame. Expect `products.rala.latest` to stay `rala/clean/latest/{z}/{x}/{y}.png`. The consumer frame URL should still be HTTP 200.

For the FIX2 cook, point those curls at `rala-review/fix2/...` and expect `dbz_interp` `fix2`. `products.rala.reviews.structure_v1` should still be present if STRUCTURE was cooked on that tree earlier.

Point the MPWG map tile source at the review template for the labeled test. Leave the consumer source on `rala/clean/latest`. Revert the map when the review is done. To stop serving a token, delete that `rala-review/<token>/` prefix on R2. The production timer does not write that prefix.

## Perf

The field is built once per frame and shared read-only across the two tile workers. A Central Texas synthetic grid (371×411, ~33k valid cells) prepared in about 0.6 s. A full-echo 512 px tile then sampled in about 3 s; residual IDW is the cost. Empty tiles are skipped before render, but prepare still runs once per frame.

CONUS echo is a fraction of the mosaic, not the whole grid. On a stormy day, hundreds to thousands of echo tiles at ~3 s each means one frame is many minutes to about an hour, and a 75-minute archive of many frames is hours. Use one `--grib` frame for the interactive test. Do not hang this on the 2-minute production timer, and do not raise `MemoryMax` or tile workers for it from this branch.

## What this host cannot finish

The map app is not in this repo. EC2 has to run the cook against live R2 credentials from this branch. Lovable (or whoever owns the map style) has to set the review tile URL above. This cooker does not edit `/etc/mpwg-radar.env` and does not start the production service with STRUCTURE or FIX2.
