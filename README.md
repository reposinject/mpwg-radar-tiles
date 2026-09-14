# MPWG radar tiles

Self-hosted **NOAA MRMS** reflectivity cooker for **My Personal Weatherguy**. It crops **Central Texas**, keeps **physical dBZ** separate from color, paints the **MPWG Clean** palette (James, Sep 2026), and emits **512×512 XYZ PNGs** for Cloudflare R2. Designed to run on **AWS EC2 Amazon Linux 2023 ARM (`t4g.small`)** on a systemd timer.

No paid radar vendor. CONUS is never pre-rendered.

## What it does

1. Downloads free NOAA MRMS `MergedReflectivityQCComposite` (NCEP HTTP, AWS Open Data fallback).
2. Decodes PNG-packed GRIB2 with Pillow (no GDAL/eccodes required).
3. Crops Central Texas immediately so a 2 GB ARM instance stays comfortable.
4. Stores a float32 dBZ grid (`output/dbz/{frame}.npz`) **before** colorization.
5. Applies mode QC, then the Clean palette.
6. Writes `{z}/{x}/{y}.png` at 512 px, transparent where there is no echo.
7. Writes `frame.json` + `manifest.json`.
8. Uploads with **boto3** to **Cloudflare R2** when `R2_*` env vars are set.
9. Repeats about every 3 minutes via systemd.

### Central Texas crop

`west=-100.25, south=28.85, east=-96.15, north=32.55` — San Antonio, Austin, Waco, Killeen, College Station, Hill Country. Default zooms **6–9** (configurable; do not raise this to CONUS).

### Modes

| Mode | Default | Behavior |
| --- | --- | --- |
| **clean** | yes | ≥ ~10 dBZ, despeckle, mild 3×3 smooth |
| **standard** | scaffold | ≥ ~5 dBZ, no extra cleanup |
| **all** | scaffold | fill masked only (still hides MRMS `-99` / `-999`) |

Timer cooks `clean` unless `MPWG_MODES=clean,standard,all`.

NEXRAD Level-II is a source stub (`mpwg_radar.sources.NexradSource`) for later — not used in production.

### MPWG Clean palette (James, Sep 2026)

Color is applied only at tile time. One shade lighter than a heavy NWS precip ramp; no cyan/blue clear-air clutter.

| dBZ | Look |
| --- | --- |
| &lt; 10 | transparent (Clean) |
| 10–15 | barely tinted green |
| 15–25 | light green |
| 25–35 | medium green |
| 35–45 | yellow-green → yellow |
| 45–55 | orange |
| 55–65 | red |
| 65+ | dark red / magenta (intense cores only) |

Stops live in `src/mpwg_radar/palettes/mpwg-clean-2026-09.json`.

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
sudo nano /etc/mpwg-radar.env    # paste R2 keys
sudo systemctl start mpwg-radar-cooker.service
sudo systemctl status mpwg-radar-cooker.timer
journalctl -u mpwg-radar-cooker.service -n 80 -f
```

`deploy/ec2-setup.sh` installs Python, a venv, the app under `/opt/mpwg-radar`, and enables `mpwg-radar-cooker.timer` (**OnUnitActiveSec=3min**, inside the 2–5 min window).

The oneshot unit runs:

```
/opt/mpwg-radar/.venv/bin/mpwg-radar cook
```

Memory is capped at 1536M. Crop-first ingest is what makes `t4g.small` viable — do not expand the bbox to CONUS.

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
  maxzoom: 9,
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
