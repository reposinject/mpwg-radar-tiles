# MPWG radar tiles

Self-hosted **NOAA MRMS** reflectivity cooker for **My Personal Weatherguy**. It crops the **CONUS MRMS mosaic**, keeps **physical dBZ** separate from color, paints the **MPWG Clean** palette (James, Sep 2026), and emits **512×512 XYZ PNGs** for Cloudflare R2. Designed to run on **AWS EC2 Amazon Linux 2023 ARM (`t4g.small`)** on a systemd timer.

No paid radar vendor. Alaska and Hawaii are outside the NOAA MRMS CONUS mosaic.

## What it does

1. Downloads free NOAA MRMS `MergedReflectivityQCComposite` (QC column-max mosaic; NCEP HTTP, AWS Open Data fallback). See [Product source](#product-source).
2. Decodes PNG-packed GRIB2 with Pillow (no GDAL/eccodes required).
3. Crops the CONUS mosaic bbox immediately, then QC / colorize / tile.
4. Stores a float32 dBZ grid (`output/dbz/{frame}.npz`) **before** colorization. The 15 dBZ Clean cutoff is display-only and does not discard this grid.
5. Applies mode QC, then the Clean palette (linear RGB interpolation on actual dBZ).
6. Writes `{z}/{x}/{y}.png` at 512 px, fully transparent below 15 dBZ.
7. Writes `frame.json` + `manifest.json`.
8. Uploads **this cook's new frame + `latest` pointers + `manifest.json`** with **boto3** `put_object` to **Cloudflare R2** when `R2_*` env vars are set. Retained historical frames are not re-uploaded.
9. Repeats about every 3 minutes via systemd.

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

### Modes

| Mode | Default | Behavior |
| --- | --- | --- |
| **clean** | yes | despeckle + mild 3×3 smooth; mode-grid clutter filter &lt; ~10 dBZ. Tiles use the palette display cutoff (&lt; 15 dBZ fully transparent). |
| **standard** | scaffold | ≥ ~5 dBZ, no extra cleanup (same Clean palette, so &lt; 15 dBZ still transparent) |
| **all** | scaffold | fill masked only (still hides MRMS `-99` / `-999`) |

Timer cooks `clean` unless `MPWG_MODES=clean,standard,all`.

NEXRAD Level-II is a source stub (`mpwg_radar.sources.NexradSource`) for later — not used in production.

### Product source

Production ingest stays on NOAA MRMS **`MergedReflectivityQCComposite`** (QC column-max mosaic). Confirmed Sep 2026:

| NOAA name | What it is | Cooker |
| --- | --- | --- |
| **`MergedReflectivityQCComposite`** | Quality-controlled **composite** (column-max) reflectivity. NCEP `/2D/MergedReflectivityQCComposite/`, AWS `CONUS/MergedReflectivityQCComposite_00.50`. | **Used** (HTTP + S3 fallback) |
| `ReflectivityAtLowestAltitude` | **RALA** — reflectivity at lowest altitude. Distinct product, not a composite. NWS MRMS v12.2 lists it as **unQC**. NCEP `/2D/ReflectivityAtLowestAltitude/`, AWS `CONUS/ReflectivityAtLowestAltitude_00.50`. | Not used |
| `MergedReflectivityAtLowestAltitude` | Merged RALA sibling (same 0.01° grid). | Not used |

There is no operational 2D GRIB2 named `ReflectivityAtLowestAltitudeQC`. Switching the cooker to RALA would change the field (lowest-altitude vs column-max) rather than swap an equivalent QC mosaic; the PNG GRIB2 decoder would likely still work, but it is not a drop-in for this locked Clean palette pass. Override later with `MRMS_LATEST_URL` / `MRMS_S3_PREFIX` if needed.

### MPWG Clean palette (James, Sep 2026)

Color is applied only at tile time. Physical dBZ is never quantized to the color table. Between anchors, RGB is interpolated against **actual dBZ** (0.1 dBZ LUT) — not snapped to 5 dBZ buckets. No cyan/aqua in the ramp. Values **below 15 dBZ are fully transparent** (display cutoff only; the `output/dbz/{frame}.npz` crop keeps the numbers).

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
output/radar/clean/{frameId}/{z}/{x}/{y}.png
output/radar/clean/{frameId}/frame.json
output/radar/clean/latest/...
output/radar/manifest.json
output/radar/colorbar.png
output/dbz/{frameId}.npz          # physical crop, local by default
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
| `MPWG_UPLOAD_CONCURRENCY` | `2` | Parallel `put_object` workers (2 vCPU; try `4` only if logs show upload bound) |
| `MPWG_UPLOAD_CONNECT_TIMEOUT` | `10` | boto3 connect timeout (seconds) |
| `MPWG_UPLOAD_READ_TIMEOUT` | `30` | boto3 read timeout (seconds) |
| `MPWG_UPLOAD_OBJECT_TIMEOUT` | `60` | Fail if no object completes in this many seconds |
| `MPWG_UPLOAD_TIMEOUT` | `180` | Fail the whole upload after this many seconds |
| `MPWG_UPLOAD_MAX_ATTEMPTS` | `2` | botocore attempts (initial + 1 retry) |

Logs look like: `R2 upload start objects=N skipped=M concurrency=2 …` then `R2 upload complete started=N uploaded=N failed=0 skipped=M duration=12.34s`. `status.json` records `uploaded`, `upload_skipped`, and `upload_duration_seconds`.

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

`deploy/ec2-setup.sh` installs Python, a venv, the app under `/opt/mpwg-radar`, and enables `mpwg-radar-cooker.timer` (**OnUnitActiveSec=3min**, inside the 2–5 min window).

The oneshot unit runs:

```
/opt/mpwg-radar/.venv/bin/mpwg-radar cook
```

Memory is capped at 1536M. Default CONUS zooms are **6–8** (~2302 candidate tiles/frame). Empty tiles are skipped before the 512×512 render so a t4g.small can finish inside the 3 minute timer.

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

Confirm the new `colorbar.png` and that `clean/latest` tiles are fully transparent below 15 dBZ. Physical `output/dbz/{frame}.npz` grids are unchanged (display threshold only).

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

### Deploy note — R2 upload hang on t4g.small (CONUS)

If cook finishes locally (`manifest.json` + tiles under `output/radar/clean/{frameId}/`) but the process sits in `futex_wait` during R2 upload and the public CDN stays stale:

1. Stop the hung oneshot (systemd `TimeoutStartSec=300` should eventually SIGTERM it):

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
MPWG_UPLOAD_TIMEOUT=180
MPWG_UPLOAD_MAX_ATTEMPTS=2
```

4. Reload and run one cook:

```bash
sudo systemctl daemon-reload
sudo systemctl restart mpwg-radar-cooker.timer
sudo systemctl start mpwg-radar-cooker.service
journalctl -u mpwg-radar-cooker.service -n 120 -f
```

Success looks like `R2 upload start` with `frame=<N> latest=<N>` (one frame, not the full retention) and `R2 upload complete … failed=0` in well under the 180s upload deadline. Then `curl` the public `manifest.json` / `clean/latest/…` and confirm `latest_frame` matches the cook `frame_id`.

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

If `R2_PREFIX=radar`, the path is `/radar/clean/latest/{z}/{x}/{y}.png`. Poll `manifest.json` for animation frames.

## Dependencies

Runtime: `numpy`, `pillow`, `boto3` (see `requirements.txt`). GRIB decoding is PNG template 5.41 only — current operational MRMS composite. If NOAA switches to JPEG2000, the decoder errors with a pointer to eccodes/wgrib2.

## License / attribution

Radar data: NOAA MRMS. Palette: MPWG Clean (James, Sep 2026).
