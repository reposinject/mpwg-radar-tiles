# MPWG radar tiles

Self-hosted **NOAA MRMS** reflectivity cooker for **My Personal Weatherguy**. It crops the **CONUS MRMS mosaic**, keeps **physical dBZ** separate from color, paints the **MPWG Clean** palette (James, Sep 2026), and emits **512×512 XYZ PNGs** for Cloudflare R2. Designed to run on **AWS EC2 Amazon Linux 2023 ARM (`t4g.small`)** on a systemd timer.

No paid radar vendor. Alaska and Hawaii are outside the NOAA MRMS CONUS mosaic.

## What it does

1. Downloads free NOAA MRMS. **Production default** is `MergedReflectivityQCComposite` (QC column-max). Phase 1 also cooks **RALA** (`ReflectivityAtLowestAltitude`) as a parallel product. See [Product source](#product-source).
2. Decodes PNG-packed GRIB2 with Pillow (no GDAL/eccodes required).
3. Crops the CONUS mosaic bbox immediately, then QC / colorize / tile.
4. Stores a float32 dBZ grid plus a **valid / no-echo / missing** mask (`output/dbz/{product}/{frame}.npz`) **before** colorization.
5. Applies mode QC, then the palette (linear RGB interpolation on actual dBZ). Composite Clean keeps the 15 dBZ display cutoff. RALA uses a sibling palette with `display_min_dbz=-32` so **valid** returns, including weak and negative dBZ, stay visible. No-echo and missing stay transparent.
6. Writes `{z}/{x}/{y}.png` at 512 px.
7. Writes `frame.json` + `manifest.json` (manifest lists available products and the default).
8. Uploads **this cook's new frame + `latest` pointers + `manifest.json`** with **boto3** `put_object` to **Cloudflare R2** when `R2_*` env vars are set. Other products on disk are not re-uploaded.
9. Repeats on systemd timers. **Composite** (production default) about every 3 minutes. **RALA** about every 2 minutes, in parallel, without replacing the default product.

### CONUS crop and zooms

Default region **`conus`**: `west=-130, south=20, east=-60, north=55` — NOAA MRMS CONUS mosaic (0.01° grid), production product `MergedReflectivityQCComposite`. Lower 48, Gulf, near-shore Atlantic/Pacific, northern Mexico, southern Canada. Not Alaska or Hawaii.

Default zooms **6–8** (512 px tiles). Candidate tiles per frame (every XYZ cell that intersects the bbox, including empty ocean):

| Zoom band | Tiles / frame | Why |
| --- | --- | --- |
| **6–8 (default)** | **2302** (z6=126, z7=442, z8=1734) | Fits `t4g.small` on the ~3 min timer with empty-tile skip |
| 5–8 | 2337 | z5 is only +35 tiles if the map needs a national overview |
| 6–9 | 8902 | z9 alone is 6600; too many for t4g.small on a busy precip day |
| `central-texas` 6–9 | 80 | Local smoke / cheap debug crop |

Empty tiles are skipped (not written). The optional Worker returns a transparent PNG for missing keys, so clear air stays clear instead of purple 404s.

Named region `central-texas` (`west=-100.25, south=28.85, east=-96.15, north=32.55`) remains for smoke tests.

Env (also accepted as unprefixed `REGION` / `BBOX`):

| Variable | Default | Meaning |
| --- | --- | --- |
| `MPWG_REGION` | `conus` | `conus` or `central-texas` |
| `MPWG_BBOX` | (unset) | Optional `west,south,east,north` override in degrees |
| `MPWG_MIN_ZOOM` | `6` | Inclusive |
| `MPWG_MAX_ZOOM` | `8` | Inclusive; do not set `9` on t4g.small |
| `MPWG_TILE_SIZE` | `512` | Production contract |
| `MPWG_MODES` | `clean` | `clean`, or `clean,standard,all` |
| `MPWG_PRODUCT` | `composite` | Manual cook default. Leave `composite` in `/etc/mpwg-radar.env`. The RALA unit sets `rala` for that process only. |
| `MPWG_DISPLAY_MIN_DBZ` | (palette JSON) | Optional display cutoff override. Composite JSON=15, RALA JSON=-32. |
| `MPWG_PALETTE` | (per product) | Optional. Composite `mpwg-clean-2026-09`; RALA `mpwg-rala-2026-09` (version `2026-09-rala-p3d`, same stop table as p3c). |
| `MPWG_RETENTION_FRAMES` | `30` | Composite frame cap (count only, no age limit). |
| `MPWG_RALA_RETENTION_MINUTES` | `75` | RALA rolling window. Age is the operating limit (≥60-minute loop). |
| `MPWG_RALA_RETENTION_FRAMES` | `60` | RALA safety ceiling, above ~38 frames at a 2-minute cadence over 75 minutes. |
| `MPWG_RALA_CATCHUP_BUDGET_SECONDS` | `2700` | Stop starting more RALA catch-up frames after this long. Leftover real scans wait for the next run. |
| `MPWG_RALA_UPLOAD_CONCURRENCY` | `8` | RALA `put_object` / `copy_object` workers. Ignores `MPWG_UPLOAD_CONCURRENCY`. |

### Modes

| Mode | Default | Behavior |
| --- | --- | --- |
| **clean** | yes | **Composite:** drop mode-grid clutter &lt; ~10 dBZ, despeckle, mild 3×3 smooth; tiles use nearest sampling and the 15 dBZ Clean display cutoff. **RALA:** no dBZ floor and no despeckle (isolated valid cells kept); no grid smooth; tiles use a mask-clipped contour (`masked-splat`, see below). |
| **standard** | scaffold | ≥ ~5 dBZ, no extra cleanup (composite Clean palette still hides &lt; 15 dBZ) |
| **all** | scaffold | fill masked only (no-echo `-99` and no-coverage `-999` stay masked) |

Timer cooks `clean` unless `MPWG_MODES=clean,standard,all`.

NEXRAD Level-II is a source stub (`mpwg_radar.sources.NexradSource`) for later — not used in production.

### Product source

Production ingest stays on NOAA MRMS **`MergedReflectivityQCComposite`** until James signs off. Phase 1 adds **RALA** as a second cook path (`MPWG_PRODUCT=rala` or `mpwg-radar cook --product rala`). Confirmed against NSSL MRMS v12.2 GRIB2 tables ([mrms-support UserTable](https://github.com/NOAA-National-Severe-Storms-Laboratory/mrms-support/blob/main/GRIB2_TABLES/UserTable_MRMS_v12.2.csv)):

| NOAA name | Param | QC? | Cooker |
| --- | --- | --- | --- |
| **`MergedReflectivityQCComposite`** | 209.10.0 | **QC** column-max composite. NCEP `/2D/MergedReflectivityQCComposite/`, AWS `CONUS/MergedReflectivityQCComposite_00.50`. | **Production default** (`composite`) |
| **`ReflectivityAtLowestAltitude`** | 209.3.57 | Operational **RALA**. WDTD: derived from the 3D reflectivity cube (clutter / AP / bioscatter removed; bright band remains). NSSL table does **not** label it non-QC. Sentinels: Missing=**-99** (no-echo), No Coverage=**-999**. NCEP `/2D/ReflectivityAtLowestAltitude/`, AWS `CONUS/ReflectivityAtLowestAltitude_00.50`. | **Phase 1** (`rala`) |
| `MergedReflectivityAtLowestAltitude` | 209.3.58 | NSSL: **"Non Quality Controlled Reflectivity At Lowest Altitude"**. | Not used |

RadarScope **Typed Reflectivity at Lowest Altitude** is this RALA **dBZ field** colored by MRMS `PrecipFlag` (rain / snow / convection / …). Public NOAA does not ship a single typed-RALA GRIB2. This pass ingests the dBZ field only.

There is no operational 2D GRIB2 named `ReflectivityAtLowestAltitudeQC`. Do not “fix too much green” by blanking all values below 10/15/20 dBZ on RALA — missing/no-echo are a **category mask**, not a reflectivity cutoff. Composite Clean still uses `display_min_dbz=15` and Clean-mode clutter &lt; ~10 dBZ. RALA uses palette `mpwg-rala-2026-09` (`display_min_dbz=-32`) for **valid** returns only.

`MRMS_LATEST_URL` / `MRMS_S3_PREFIX` remain composite-era overrides. A leftover composite URL in `/etc/mpwg-radar.env` is ignored when cooking `rala`.

### MPWG Clean palette (James, Sep 2026)

Color is applied only at tile time. Physical dBZ is never quantized to the color table. Between anchors, RGB is interpolated against **actual dBZ** (0.1 dBZ LUT) — not snapped to 5 dBZ buckets. No cyan/aqua in the ramp. Values **below 15 dBZ are fully transparent** (display cutoff only; the `output/dbz/{product}/{frame}.npz` crop keeps the numbers).

| dBZ | Hex | RGB | Look |
| --- | --- | --- | --- |
| &lt; 15 | — | — | transparent |
| 15 | `#08772E` | 8, 119, 46 | green |
| 20 | `#0FA33D` | 15, 163, 61 | green |
| 25 | `#32C94B` | 50, 201, 75 | green |
| 30 | `#F4F20D` | 244, 242, 13 | yellow |
| 35 | `#F4C20D` | 244, 194, 13 | gold |
| 40 | `#F58A16` | 245, 138, 22 | orange |
| 45 | `#F34A1F` | 243, 74, 31 | red-orange |
| 50 | `#ED171C` | 237, 23, 28 | red |
| 55 | `#C91427` | 201, 20, 39 | dark red |
| 60 | `#E52AAE` | 229, 42, 174 | magenta |
| 65 | `#B52ACB` | 181, 42, 203 | purple |
| 70 | `#7828C8` | 120, 40, 200 | violet |
| 75+ | `#D9B6FF` | 217, 182, 255 | lavender |

Stops live in `src/mpwg_radar/palettes/mpwg-clean-2026-09.json`. `colorbar.png` is generated from the same table (15–75 dBZ).

### RALA palette and render (Phase 3, test product)

The echo footprint already lined up with RadarScope, so the RALA source is unchanged (`ReflectivityAtLowestAltitude`, param 57). QC and the no-echo / missing mask are unchanged. The p3c color stops are unchanged. `2026-09-rala-p3d` is a render-path stamp: the mask-clipped resample is tighter, so a cook repaints frames that still carry p3c.

RALA tiles use `src/mpwg_radar/palettes/mpwg-rala-2026-09.json` (version `2026-09-rala-p3d`). Color is a **piecewise** RGBA interpolation on actual dBZ (0.1 dBZ LUT, half-up index, no rescale). RGB at the RadarScope sample dBZ **2.0, 10.3, 24.8, 31.7, 39.7, 48.3, 56.4, 64.7** is unchanged. Stops every 2.5 dBZ are filled in between those samples so a calibration strip cannot collapse a 10 dBZ family onto one swatch. 48.3→56.4 follows the short hue arc (orange through red to magenta) instead of a guessed red cliff. 75 is white. There is no cyan/aqua stop. The hex stops are the p3c table.

Valid-dBZ alpha is `56 + 199 * t²` with `t` running from -32 to 24.8, then 255. Weak returns stay visible and quieter than the opaque greens. There is no transparent cutoff at 10 dBZ. No-echo and missing stay alpha 0 via the category mask, not via that ramp.

`python3 -m mpwg_radar color-diag` prints this stop table and the probe trace (input dBZ → decoded float32 → normalized, which is identity → LUT index → RGB → alpha → RGBA) through `palette.colorize`.

| dBZ | Hex | R | G | B | A | Look |
| --- | --- | --- | --- | --- | --- | --- |
| -32 | `#102E14` | 16 | 46 | 20 | 56 | faintest valid wisp |
| 0 | `#1B852E` | 27 | 133 | 46 | 119 | subtle weak return |
| 2 | `#1C8A30` | 28 | 138 | 48 | 127 | subtle green |
| 2.5 | `#1D8B31` | 29 | 139 | 49 | 129 | subtle green |
| 5 | `#239134` | 35 | 145 | 52 | 140 | subtle green |
| 7.5 | `#289738` | 40 | 151 | 56 | 152 | subtle green |
| 10 | `#2D9D3C` | 45 | 157 | 60 | 165 | subtle green |
| 10.3 | `#2E9E3C` | 46 | 158 | 60 | 166 | subtle green |
| 12.5 | `#30A33E` | 48 | 163 | 62 | 178 | green |
| 15 | `#33A840` | 51 | 168 | 64 | 192 | green |
| 17.5 | `#36AE42` | 54 | 174 | 66 | 207 | green |
| 20 | `#39B344` | 57 | 179 | 68 | 223 | green |
| 22.5 | `#3BB946` | 59 | 185 | 70 | 239 | green |
| 24.8 | `#3EBE48` | 62 | 190 | 72 | 255 | green |
| 25 | `#41BF47` | 65 | 191 | 71 | 255 | green toward yellow |
| 27.5 | `#62C73E` | 98 | 199 | 62 | 255 | green toward yellow |
| 30 | `#83CF34` | 131 | 207 | 52 | 255 | green toward yellow |
| 31.7 | `#9AD42E` | 154 | 212 | 46 | 255 | green toward yellow |
| 32.5 | `#A3D52B` | 163 | 213 | 43 | 255 | yellow |
| 35 | `#C1D722` | 193 | 215 | 34 | 255 | yellow |
| 37.5 | `#DEDA18` | 222 | 218 | 24 | 255 | yellow |
| 39.7 | `#F8DC10` | 248 | 220 | 16 | 255 | yellow |
| 40 | `#F8DA11` | 248 | 218 | 17 | 255 | yellow |
| 42.5 | `#F7C616` | 247 | 198 | 22 | 255 | yellow |
| 45 | `#F6B21B` | 246 | 178 | 27 | 255 | orange |
| 47.5 | `#F49E20` | 244 | 158 | 32 | 255 | orange |
| 48.3 | `#F49822` | 244 | 152 | 34 | 255 | orange |
| 50 | `#EB6320` | 235 | 99 | 32 | 255 | orange |
| 52.5 | `#DD1D1F` | 221 | 29 | 31 | 255 | red |
| 55 | `#D01A5A` | 208 | 26 | 90 | 255 | red |
| 56.4 | `#C81878` | 200 | 24 | 120 | 255 | magenta |
| 57.5 | `#CB1B85` | 203 | 27 | 133 | 255 | magenta |
| 60 | `#D222A2` | 210 | 34 | 162 | 255 | magenta |
| 62.5 | `#DA2ABF` | 218 | 42 | 191 | 255 | magenta |
| 64.7 | `#E030D8` | 224 | 48 | 216 | 255 | magenta |
| 65 | `#E136D9` | 225 | 54 | 217 | 255 | magenta toward white |
| 67.5 | `#E868E3` | 232 | 104 | 227 | 255 | magenta toward white |
| 70 | `#F09BEC` | 240 | 155 | 236 | 255 | magenta toward white |
| 72.5 | `#F7CDF6` | 247 | 205 | 246 | 255 | magenta toward white |
| 75 | `#FFFFFF` | 255 | 255 | 255 | 255 | white extreme |

**Anti-bloom rule:** a pixel is colored only when its nearest MRMS cell is real echo. A clear-air cell next to a core stays empty. Inside the echo, alpha insets the square rim over the outer part of the boundary cell, so the 0.01° grid is not a hard mosaic and the rest of the cell stays opaque. dBZ is a local resample of valid neighbors only: shared faces grade, cell centers stay near the source value, and a 25 dBZ cell beside no-echo does not become a 25→18→12→6 ramp. A single weak cell is still drawn (a small disc), not deleted and not cut off below 15 dBZ. No-echo and missing stay alpha 0. Composite tiles stay nearest-neighbor with the Clean palette.

### RALA frame archive (Phase 3)

A live loop was sparse because each cook fetched only `.latest` and a shared `MPWG_RETENTION_FRAMES` cap sliced the manifest for both products. A CONUS cook takes longer than the ~2-minute MRMS cadence, so scans published during that cook were never downloaded.

RALA now keeps a **75-minute** rolling window of real scans (max **60** frames). At a ~2-minute cadence that is about **31 frames in 60 minutes** (both ends of the hour), about **16 in 30 minutes**, and about **38** held in the full window. The frame cap sits above that count, so every real scan in the window is kept. Composite stays at `MPWG_RETENTION_FRAMES` (default 30) with no age limit. The archive only stores scans NOAA actually published.

Each RALA run lists NOAA's S3 objects inside the window (NCEP `.latest` when S3 is stale or empty) and cooks every real file not already on disk, newest first. The list is refreshed during a catch-up and merged with keys already seen, so an empty re-list cannot throw away the 2-minute objects already found. A scan published mid-run is queued. The `latest` alias stays on the newest scan when an older hole is filled. A catch-up budget (default 2700s) defers leftover scans to the next run and still cooks each real file in order. Nothing is interpolated. `manifest.json` lists those frames with the GRIB `valid_time` and `products.rala.retention.frames_last_60_minutes`.

A live loop that only shows about five or six scans in 60 minutes, about 10–12 minutes apart, is one frame per oneshot. The 2-minute timer does not start a second RALA cook while the first is still `activating`. On t4g.small that oneshot was ~590s of cook plus ~30s of upload (uploads were already succeeding, including `825/825`). Two things inside the cook dominated:

- masked-splat evaluated a 9×9 Gaussian on every pixel of every 512×512 tile, including clear air
- PNG `optimize=True` spent ~0.37s per noisy tile for a ~2% smaller file

Together that is about 0.8s per echo tile. A CONUS frame writes on the order of 800 tiles, so the oneshot cannot return before the next NOAA scan, and the scans in between never get a turn. Upload concurrency 8 and server-side `CopyObject` of `latest` PNGs stay in place (the RALA unit sets `MPWG_RALA_UPLOAD_CONCURRENCY=8` after `EnvironmentFile`). They are not what closed the gap. The cook now skips clear-air pixels in the same splat, writes PNGs at zlib level 6, renders echo tiles on 2 threads (`MPWG_TILE_WORKERS`, pinned on the RALA unit), and hardlinks `latest/` instead of copying the tile tree. A frame that used to take ~10 minutes should land in about 1–2 minutes on t4g.small when NOAA is on a 2-minute cadence, which is enough for ~30 real frames in 60 minutes (~16 in 30 minutes, ~38 in the 75-minute window). Widespread precip plus a composite cook on the same 2 vCPUs can still run long; the catch-up then fills the listed GRIBs newest-first inside the 2700s budget. A larger instance is not required for that. If `frames_last_60_minutes` stays well under ~25 after a full catch-up while NOAA's own keys are still ~2 minutes apart, the host is short on CPU (t4g.small is 2 vCPU), not on the listing.

The app probe is not in this repo. If it colorizes on its own, it must interpolate these stops, including alpha. A nearest-step LUT is the banding in the still.

Check one synthetic frame without deploying:

```bash
python3 -m mpwg_radar cook --product rala --region central-texas \
  --source synthetic --no-upload --out output/rala-phase2
```

Tiles: `output/rala-phase2/radar/rala/clean/latest/{z}/{x}/{y}.png`. Storm edges should stop at the echo mask. The outer part of a boundary cell feathers; the cell center stays opaque, and clear-air neighbors stay empty. A 25 dBZ cell beside no-echo stays near 25 dBZ (no invented 18/12/6 fringe). Inside the squall, neighboring cells grade across their shared face and a hot core stays in its own color family. `frame.json` `valid_time` is still the frame time. `mode_spec.sample` is `masked-splat`, `mode_spec.smooth_kind` is `masked-splat`, and `mode_spec.despeckle` is false. `display_min_dbz` is -32. Palette version on that frame is `2026-09-rala-p3d`.

`python3 -m mpwg_radar color-diag` still prints the p3c stop table. Decade pairs (22 vs 29, 31 vs 39, 41 vs 49) stay distinct. That check is the color ramp, not the spatial resample.

## Layout

```
src/mpwg_radar/     cooker, GRIB decoder, palette, tiles, R2 publisher
deploy/             ec2-setup.sh + systemd unit/timer
worker/             optional Cloudflare Worker (CORS tile GET)
scripts/smoke_test.sh
.env.example
```

Output:

```
output/radar/clean/{frameId}/{z}/{x}/{y}.png          # composite (production path)
output/radar/clean/{frameId}/frame.json
output/radar/clean/latest/...
output/radar/rala/clean/{frameId}/{z}/{x}/{y}.png     # RALA
output/radar/rala/clean/latest/...
output/radar/manifest.json                            # lists products + default
output/radar/colorbar.png                             # composite Clean ramp
output/radar/rala/colorbar.png                        # RALA ramp (display_min -32)
output/dbz/{product}/{frameId}.npz                    # physical crop + category mask
```

XYZ indices match OSM/Google. Each PNG is 512×512 covering the **same** geographic extent as a 256 px slippy tile at that z/x/y.

**MapLibre:** `tileSize: 512`  
**Leaflet + OSM 256 basemap:** `tileSize: 256` (browser scales the 512 PNG; alignment stays correct)

## Local smoke test

Produces sample Clean-palette 512 tiles (no R2 credentials required):

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
./scripts/smoke_test.sh
# Live NOAA MRMS (falls back to synthetic if the download fails):
./scripts/smoke_test.sh --live
```

If `python3 -m venv` is unavailable, `./scripts/smoke_test.sh` uses the current interpreter (`pip install -e .` or `PYTHONPATH=src`). Same thing:

```bash
python3 -m pip install -e .
python3 -m mpwg_radar smoke
python3 -m mpwg_radar smoke --live
```

Then:

- Tiles: `output/smoke/radar/clean/latest/{z}/{x}/{y}.png`
- Manifest: `output/smoke/radar/manifest.json`
- Preview: `output/smoke/preview.html`

```bash
python3 -m http.server --directory output/smoke 8765
# open http://127.0.0.1:8765/preview.html
```

Checks the CLI enforces: at least one **512×512 RGBA** tile and some non-zero alpha (echo).

Unit tests (no network):

```bash
pip install -e ".[dev]"
python -m pytest -m "not live"
```

Live NOAA decode: `python -m pytest -m live`.

## R2 upload

Copy `.env.example` to `.env` (or `/etc/mpwg-radar.env` on EC2). Fill:

```
R2_ACCOUNT_ID=...
R2_ACCESS_KEY_ID=...
R2_SECRET_ACCESS_KEY=...
R2_BUCKET=mpwg-radar
# optional if account id is set:
# R2_ENDPOINT=https://<ACCOUNT_ID>.r2.cloudflarestorage.com
R2_PREFIX=radar
```

`boto3` uploads only when those keys are set. Without them the cooker still writes tiles locally.

Each cook uploads only:

- `{mode}/{frameId}/**` (new tiles + `frame.json`)
- `{mode}/latest/**` (short-lived pointers)
- `colorbar.png` and `manifest.json` (manifest last, so a failed upload leaves CDN on the previous frame)

It does **not** walk or re-upload the rest of `MPWG_RETENTION_FRAMES`. Re-sending the full CONUS retention set on t4g.small (~1 GB+ of identical objects, no socket timeout) is what hung the publisher in `futex_wait`.

Optional upload limits (defaults are safe on t4g.small):

| Variable | Default | Meaning |
| --- | --- | --- |
| `MPWG_UPLOAD_CONCURRENCY` | `2` | Composite parallel `put_object` workers. RALA ignores this and uses `MPWG_RALA_UPLOAD_CONCURRENCY` (default 8). |
| `MPWG_UPLOAD_CONNECT_TIMEOUT` | `10` | boto3 connect timeout (seconds) |
| `MPWG_UPLOAD_READ_TIMEOUT` | `30` | boto3 read timeout (seconds) |
| `MPWG_UPLOAD_OBJECT_TIMEOUT` | `60` | Fail if no object completes in this many seconds |
| `MPWG_UPLOAD_TIMEOUT` | `900` | Whole-upload deadline (seconds). CONUS on t4g.small at concurrency=2 is a few objects/sec (~800–1000 objects/frame; a RALA oneshot is ~230s for ~866 objects). 180s left failed uploads every cycle. |
| `MPWG_UPLOAD_MAX_ATTEMPTS` | `2` | botocore attempts (initial + 1 retry) |

Logs look like: `R2 upload start objects=N skipped=M concurrency=2 …` then `R2 upload complete started=N uploaded=N failed=0 skipped=M duration=12.34s`. Each cook also logs one `latency product=… source_valid_time=… cook_finished_at=… upload_finished_at=… source_age_s=… cook_s=… upload_s=… lag_s=…` line. `status.json` is the composite cook (`status-rala.json` for RALA) and records `uploaded`, `upload_skipped`, `upload_duration_seconds`, and those timestamps. The same timestamps are on `manifest.json` under `products.<id>` (`source_valid_time`, `cook_finished_at`, `upload_finished_at`, `lag_seconds`).

```bash
mpwg-radar cook                  # MRMS → tiles → R2 if env set
mpwg-radar cook --no-upload      # local only
mpwg-radar cook --source synthetic --no-upload
```

Do not commit secrets. `.env` is gitignored.

## EC2 (Amazon Linux 2023 ARM / t4g.small)

1. Launch **t4g.small** in a region that can reach `mrms.ncep.noaa.gov` and `noaa-mrms-pds.s3.amazonaws.com` (and R2).
2. Clone this repo on the instance.
3. As root:

```bash
sudo bash deploy/ec2-setup.sh
sudo nano /etc/mpwg-radar.env    # paste R2 keys; confirm MPWG_REGION=conus and zooms 6–8
sudo systemctl start mpwg-radar-cooker.service
sudo systemctl status mpwg-radar-cooker.timer
journalctl -u mpwg-radar-cooker.service -n 80 -f
```

`deploy/ec2-setup.sh` installs Python, a venv, the app under `/opt/mpwg-radar`, and enables `mpwg-radar-cooker.timer` (**OnUnitActiveSec=3min**, composite) and `mpwg-radar-cooker-rala.timer` (**OnUnitActiveSec=2min**, RALA only). Leave `MPWG_PRODUCT=composite` in the env file.

The oneshot unit runs:

```
/opt/mpwg-radar/.venv/bin/mpwg-radar cook
```

Memory is capped at 1536M. Default CONUS zooms are **6–8** (~2302 candidate tiles/frame). Empty tiles are skipped before the 512×512 render, and an empty parent tile skips its children. The composite oneshot sets `TimeoutStartSec=1800`. The RALA oneshot sets `TimeoutStartSec=4200`, and `ec2-setup.sh` installs `mpwg-radar-cooker-rala.service.d/zz-catchup-timeout.conf` so a host drop-in of `TimeoutStartSec=1800` cannot SIGTERM a multi-frame catch-up. A long cook delays the next timer shot; it does not change the composite product.

### Deploy note — Sep 2026 Clean palette (15 dBZ display cutoff)

Palette + cutoff only. No `/etc/mpwg-radar.env` product-URL change (still `MergedReflectivityQCComposite`). On the cooker host:

```bash
cd /opt/mpwg-radar          # or the clone you used
sudo git pull origin main
sudo bash deploy/ec2-setup.sh
sudo systemctl restart mpwg-radar-cooker.timer
sudo systemctl start mpwg-radar-cooker.service
journalctl -u mpwg-radar-cooker.service -n 80 -f
```

Confirm the new `colorbar.png` and that `clean/latest` tiles are fully transparent below 15 dBZ. Physical `output/dbz/{product}/{frame}.npz` grids are unchanged (display threshold only).

### Deploy note — existing `/etc/mpwg-radar.env`

`ec2-setup.sh` **keeps** an existing env file, so a host that was cooking Central Texas will stay on that crop until you edit the file. After pulling this revision:

```bash
sudo nano /etc/mpwg-radar.env
```

Set (or confirm):

```
MPWG_REGION=conus
MPWG_MIN_ZOOM=6
MPWG_MAX_ZOOM=8
MPWG_TILE_SIZE=512
MPWG_MODES=clean
```

Optional bbox override: `MPWG_BBOX=-130,20,-60,55` (west,south,east,north). Then reinstall units from the repo, reload, and kick the timer:

```bash
sudo bash deploy/ec2-setup.sh          # rsyncs code, pip install -e, installs units
sudo systemctl daemon-reload
sudo systemctl restart mpwg-radar-cooker.timer
sudo systemctl start mpwg-radar-cooker.service
sudo systemctl status mpwg-radar-cooker.timer
journalctl -u mpwg-radar-cooker.service -n 80 -f
```

Confirm the new `manifest.json` has `"region": "conus"`, the CONUS bbox, and `min_zoom`/`max_zoom` 6–8. Map clients that still request z9 should set `maxzoom` / `maxNativeZoom` to 8 (or overzoom from z8).

### Deploy note — RALA cadence (parallel, default stays composite)

NOAA publishes `ReflectivityAtLowestAltitude` on the **same ~2 minute grid** as `MergedReflectivityQCComposite` (matching valid times on NCEP and on `noaa-mrms-pds`). There is no extra 15-minute RALA publication delay. A ~10 minute RALA timer plus `Conflicts=` against the composite unit was the age: Conflicts **stops** the other oneshot, so the cooks could not overlap and RALA frames landed about every 12 minutes (~15–17 minutes old).

After this revision, leave `/etc/mpwg-radar.env` at `MPWG_PRODUCT=composite`. `ec2-setup.sh` enables two timers:

| Timer | Unit | Cadence | Product |
| --- | --- | --- | --- |
| `mpwg-radar-cooker.timer` | `mpwg-radar-cooker.service` | 3 min | composite (production default) |
| `mpwg-radar-cooker-rala.timer` | `mpwg-radar-cooker-rala.service` | 2 min | rala only (`--product rala`) |

The RALA unit does **not** `Conflicts=` the composite unit. Tile prefixes differ (`clean/` vs `rala/clean/`). `manifest.json` is merged under a file lock and uploaded after that merge, so one cook cannot wipe the other product's `latest_valid_time`. Composite keeps a higher CPU weight and a lower OOM score on the 2 GB host. An unchanged MRMS valid time is not retiled. Scans that arrive while a RALA cook is still running are listed again during that run and cooked if they are still inside the 75-minute window (`products.rala.retention` on the manifest, including `frames_last_60_minutes`). The RALA oneshot `TimeoutStartSec` is 4200 so a multi-scan catch-up (2700s budget plus the 900s upload timeout) is not killed mid-PUT. Composite stays at 1800. If `systemctl cat mpwg-radar-cooker-rala.service` still shows a drop-in of `TimeoutStartSec=1800`, that drop-in wins over the unit file and cuts the catch-up off. `ec2-setup.sh` rewrites those lines to 4200 and installs `zz-catchup-timeout.conf` (it sorts after `override.conf`).

After a catch-up, `products.rala.retention.frames_last_60_minutes` should sit near ~30 when NOAA's RALA keys are ~2 minutes apart (live listings are about 117–125s). The frames are those GRIB valid times. The cooker does not insert timestamps NOAA did not publish. The RALA unit also sets `MPWG_TILE_WORKERS=2` and `MPWG_KEEP_DBZ=0` so the catch-up does not compress a physical npz for every scan. Composite still follows `MPWG_KEEP_DBZ` in the env file.

Expected age once both timers are caught up: each product's `latest_valid_time` is about one cook behind wall clock (composite often ~3–8 minutes, RALA similar and **within a few minutes of composite**). The floor is the shared ~2 minute MRMS update, not a separate RALA lag. `lag_seconds` on the product is `upload_finished_at - source_valid_time` (or cook finish, if that run did not upload).

```bash
cd /opt/mpwg-radar
sudo git pull origin main
sudo bash deploy/ec2-setup.sh
# If a hand-rolled 10-minute RALA timer or cron is still installed, disable it.
systemctl list-timers | grep -i rala
```

Do **not** set `MPWG_PRODUCT=rala` in `/etc/mpwg-radar.env`. That would make the 3-minute timer cook RALA instead of composite.

Check both timers and a latency line:

```bash
systemctl status mpwg-radar-cooker.timer mpwg-radar-cooker-rala.timer
journalctl -u mpwg-radar-cooker.service -u mpwg-radar-cooker-rala.service -n 80 | grep latency
```

On `manifest.json` (public CDN): `default_product` is `composite`. Compare `products.composite.latest_valid_time` and `products.rala.latest_valid_time` (same MRMS slot or one 2-minute step apart once RALA has completed a cycle). `products.rala.cook_finished_at` and `upload_finished_at` should be a few minutes after `source_valid_time`, not ~15.

**Preview URL** (with `R2_PREFIX=radar`):

```
https://YOUR_DOMAIN/radar/rala/clean/latest/{z}/{x}/{y}.png
```

`products.rala.latest` is the RALA template. Composite `clean/latest` is not rewritten by a RALA cook. A CONUS RALA upload still needs the 900s `MPWG_UPLOAD_TIMEOUT`. The RALA unit `TimeoutStartSec` is 4200.

### Deploy note — R2 upload hang on t4g.small (CONUS)

If cook finishes locally (`manifest.json` + tiles under `output/radar/clean/{frameId}/`) but the process sits in `futex_wait` during R2 upload and the public CDN stays stale:

1. Stop the hung oneshot (systemd `TimeoutStartSec=1800` on the composite unit should eventually SIGTERM it):

```bash
sudo systemctl stop mpwg-radar-cooker.service
sudo pkill -f 'mpwg-radar cook' || true
```

2. Pull this revision and reinstall (rsync + `pip install -e`):

```bash
cd /opt/mpwg-radar   # or the clone you used
sudo git pull        # if the instance tracks git; otherwise rsync the tree
sudo bash deploy/ec2-setup.sh
```

3. Keep `/etc/mpwg-radar.env`. You do **not** need `MPWG_RETENTION_FRAMES=5` for the upload fix (that only shrinks local disk / old full-tree walks). Optional explicit limits:

```
MPWG_UPLOAD_CONCURRENCY=2
MPWG_UPLOAD_CONNECT_TIMEOUT=10
MPWG_UPLOAD_READ_TIMEOUT=30
MPWG_UPLOAD_OBJECT_TIMEOUT=60
MPWG_UPLOAD_TIMEOUT=900
MPWG_UPLOAD_MAX_ATTEMPTS=2
```

4. Reload and run one cook:

```bash
sudo systemctl daemon-reload
sudo systemctl restart mpwg-radar-cooker.timer
sudo systemctl start mpwg-radar-cooker.service
journalctl -u mpwg-radar-cooker.service -n 120 -f
```

Success looks like `R2 upload start` with `frame=<N> latest=<N>` (one frame, not the full retention) and `R2 upload complete … failed=0` inside the 900s upload deadline. A typical CONUS frame is ~800–1000 objects. If `/etc/mpwg-radar.env` still sets `MPWG_UPLOAD_TIMEOUT=180`, remove that line so the new default applies. Then `curl` the public `manifest.json` / `clean/latest/…` and confirm `latest_frame` matches the cook `frame_id`.

A failed upload raises and leaves the previous `latest` + manifest on the CDN (manifest is written last). The next timer shot retries.

## Optional Worker

`worker/` is a small Cloudflare Worker that:

- Adds CORS for map clients
- Reads objects from the R2 bucket binding
- Returns a tiny transparent PNG when a tile key is missing (cooker skips empty tiles)

See `worker/README.md`. You can also put a custom domain on a public R2 bucket and skip the Worker.

## Client snippet (MapLibre)

```js
map.addSource('mpwg-radar', {
  type: 'raster',
  tiles: ['https://YOUR_DOMAIN/clean/latest/{z}/{x}/{y}.png'],
  tileSize: 512,
  minzoom: 6,
  maxzoom: 8,
  attribution: 'NOAA MRMS · MPWG Clean'
});
map.addLayer({
  id: 'mpwg-radar',
  type: 'raster',
  source: 'mpwg-radar',
  paint: { 'raster-opacity': 0.85 }
});
```

If `R2_PREFIX=radar`, the path is `/radar/clean/latest/{z}/{x}/{y}.png`. Poll `manifest.json` for animation frames and `products`.

RALA preview (after a rala cook; production default stays composite):

```js
tiles: ['https://YOUR_DOMAIN/radar/rala/clean/latest/{z}/{x}/{y}.png']
```

## Dependencies

Runtime: `numpy`, `pillow`, `boto3` (see `requirements.txt`). GRIB decoding is PNG template 5.41 only — current operational MRMS composite. If NOAA switches to JPEG2000, the decoder errors with a pointer to eccodes/wgrib2.

## License / attribution

Radar data: NOAA MRMS. Palette: MPWG Clean (James, Sep 2026).
