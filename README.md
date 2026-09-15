# MPWG radar tiles

Self-hosted **NOAA MRMS** reflectivity cooker for **My Personal Weatherguy**. It crops the **CONUS MRMS mosaic**, keeps **physical dBZ** separate from color, paints the **MPWG Clean** palette (James, Sep 2026), and emits **512×512 XYZ PNGs** for Cloudflare R2. Designed to run on **AWS EC2 Amazon Linux 2023 ARM (`t4g.small`)** on a systemd timer.

No paid radar vendor. Alaska and Hawaii are outside the NOAA MRMS CONUS mosaic.

## What it does

1. Downloads free NOAA MRMS `MergedReflectivityQCComposite` (NCEP HTTP, AWS Open Data fallback).
2. Decodes PNG-packed GRIB2 with Pillow (no GDAL/eccodes required).
3. Crops the CONUS mosaic bbox immediately, then QC / colorize / tile.
4. Stores a float32 dBZ grid (`output/dbz/{frame}.npz`) **before** colorization.
5. Applies mode QC, then the Clean palette.
6. Writes `{z}/{x}/{y}.png` at 512 px, transparent where there is no echo.
7. Writes `frame.json` + `manifest.json`.
8. Uploads **the new frame + `latest/` + `manifest.json`** to **Cloudflare R2** when `R2_*` env vars are set (not the whole local retention tree).
9. Repeats about every 3 minutes via systemd.

### CONUS crop and zooms

Default region **`conus`**: `west=-130, south=20, east=-60, north=55` — NOAA MRMS `MergedReflectivityQCComposite` mosaic (0.01° grid). Lower 48, Gulf, near-shore Atlantic/Pacific, northern Mexico, southern Canada. Not Alaska or Hawaii.

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
| `MPWG_RETENTION_FRAMES` | `30` | Frames kept **on local disk** for `manifest.json` animation. R2 upload does **not** re-push this history each cook |
| `MPWG_UPLOAD_WORKERS` | `4` | Concurrent R2 PUTs (clamped 1–8; t4g.small-safe) |
| `MPWG_UPLOAD_TIMEOUT_SECONDS` | `120` | Fail the cook if the upload phase exceeds this |
| `MPWG_R2_CONNECT_TIMEOUT` | `10` | Seconds to establish each R2 connection |
| `MPWG_R2_READ_TIMEOUT` | `30` | Seconds waiting on each R2 response |
| `MPWG_UPLOAD_ALL_FRAMES` | `false` | `true` re-uploads every retained local frame (repair / backfill only) |

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

Each cook PUTs only:

1. `{mode}/{frameId}/` tiles + `frame.json` (the frame just cooked)
2. `{mode}/latest/` (live map pointer; short Cache-Control)
3. `colorbar.png`
4. `manifest.json` last (so animation clients do not see a frame before its tiles exist)

Older frames listed in `manifest.json` stay on **local disk** (`MPWG_RETENTION_FRAMES`, default 30) and are **not** re-uploaded. They remain on R2 from the cook that created them until local retention prunes them, at which point the cooker deletes that prefix on R2.

This matters on CONUS z6–8: a full retention tree can be ~1 GB. The previous cook path re-walked that tree every cycle with boto3 `upload_file` (no per-PUT timeout). That is a verified code path; a hang in `futex_wait` after ~1 GB sent is consistent with TransferManager threads / a stuck socket, but that hang was not reproduced here. Uploads now use `put_object` with connect/read timeouts, a worker pool, and an overall phase deadline.

`boto3` uploads only when those keys are set. Without them the cooker still writes tiles locally.

```bash
mpwg-radar cook                  # MRMS → tiles → R2 if env set
mpwg-radar cook --no-upload      # local only
mpwg-radar cook --source synthetic --no-upload
```

Do not commit secrets. `.env` is gitignored.

If a cook exceeds `MPWG_UPLOAD_TIMEOUT_SECONDS` (default 120), the process exits non-zero instead of sitting in the upload phase. systemd `TimeoutStartSec=300` is the last-resort backstop. After a timeout, R2 may have a partial new-frame prefix; the next successful cook overwrites it. Live `latest/` and `manifest.json` are updated only after the new frame PUTs finish.

Repair / backfill (re-push every retained local frame — can be large):

```
MPWG_UPLOAD_ALL_FRAMES=true
MPWG_UPLOAD_TIMEOUT_SECONDS=300
```

Then run one cook and set `MPWG_UPLOAD_ALL_FRAMES` back to false.

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

### Redeploy this R2 upload fix on EC2

On the cooker host, as root, from the repo checkout (or after `git pull`):

```bash
cd /path/to/mpwg-radar-tiles
sudo git pull                         # or rsync this revision onto the host
sudo bash deploy/ec2-setup.sh         # rsyncs to /opt/mpwg-radar, pip install -e, reinstalls units
```

`ec2-setup.sh` **keeps** `/etc/mpwg-radar.env`. Optional knobs (defaults are already t4g.small-safe):

```
MPWG_RETENTION_FRAMES=30
MPWG_UPLOAD_WORKERS=4
MPWG_UPLOAD_TIMEOUT_SECONDS=120
MPWG_R2_CONNECT_TIMEOUT=10
MPWG_R2_READ_TIMEOUT=30
# MPWG_UPLOAD_ALL_FRAMES=false
```

Then reload and kick one cook:

```bash
sudo systemctl daemon-reload
sudo systemctl restart mpwg-radar-cooker.timer
sudo systemctl start mpwg-radar-cooker.service
sudo systemctl status mpwg-radar-cooker.timer
journalctl -u mpwg-radar-cooker.service -n 120 -f
```

Look for `R2 upload scope=new-frame` (not `all-frames`), object counts on the order of **one** CONUS frame plus `latest/` (not ~30× that), and `Cook complete` without a hang. CDN `manifest.json` `updated_at` should advance; `clean/latest/{z}/{x}/{y}.png` should stop 404ing for the new frame.

If a previous cook is still running (`systemctl is-active mpwg-radar-cooker.service`), stop it before restarting so it cannot keep the timer blocked:

```bash
sudo systemctl stop mpwg-radar-cooker.service
```

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
