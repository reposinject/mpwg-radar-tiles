# Pipeline overlap and atomic publication

Opt-in for the STRUCTURE review cooker. Every flag below defaults off. With them unset, the composite cooker and the clean RALA cooker keep today's order: local manifest write, then `frame → root → latest` (including `latest/frame.json`) → server-side copies, then the manifest PUT. Renderer output (STRUCTURE recipe, p3l sampler, p3k palette, 0.5 dBZ quant, NO-ECHO / missing / coverage, z6–z9, 512 px PNG) does not change.

Do not set `MPWG_RALA_DBZ_INTERP` in `/etc/mpwg-radar.env`. Do not set any flag in this document on the t4g public cooker until the public flip described at the bottom.

## Flags

| Flag | Default | When set |
| --- | --- | --- |
| `MPWG_PIPELINE` | off | RALA archive loop processes the next frame on the main thread while one publisher thread uploads the previous frame. Queue `maxsize=1`, so at most two frames are in flight. |
| `MPWG_ATOMIC_PUBLISH` | off | Serial `cook()` uses the order below. The pipeline publish stage always uses that order, because that stage exists only when `MPWG_PIPELINE` is on. |
| `MPWG_PUBLISH_LATEST_ALIAS` | on (`1`) | `0` skips the `latest/` copy. The manifest `latest` template is the tip frame's timestamped prefix. |
| `MPWG_REVIEW_MANIFEST_KEY` | empty | Relative to `R2_PREFIX`. Example `rala-review/structure_v1/manifest.json`. The review cooker PUTs only that key. It does not PUT `manifest.json` at the prefix root. |
| `MPWG_RETENTION_GRACE_SECONDS` | `600` | Used only by the gated path. Remote deletes wait at least 10 minutes even if this is lower. |
| `MPWG_TILE_WORKERS` | RALA code default 2 | Set `4` on the 16 GiB review box. Process stays on the main thread with this pool. |

`MPWG_PIPELINE=1` keeps newest-first scheduling. Catch-up budget `0` still cooks one frame per pass. A newer scan is processed before back-fill. Back-fill does not move `latest`. Manifest merges and PUTs run on the publisher thread, under the existing manifest lock, in completion order. A frame older than the published tip is only added to the frames list.

## Call order

```
PROCESS (main thread)
  detect → download → decode/QC → STRUCTURE prepare → render/encode
  into a frame-private directory
        │
        ▼
enqueue (blocks while the publisher is still on the previous frame)
        │
        ▼
PUBLISH (one background thread, queue maxsize 1)
  1. PUT every timestamped frame object and the colorbar
  2. VERIFY  ListObjectsV2 …/clean/<ts>/   every expected key, expected size
  3. MANIFEST PUT (review key, or radar/manifest.json when that flag is empty)
  4. CopyObject/PUT latest tiles          (skipped when the alias flag is 0
                                           or this frame is back-fill)
  5. VERIFY latest
  6. PUT latest/frame.json                LAST
  7. List latest/ and delete keys that are not in the new tile set
```

On a frame PUT or frame VERIFY failure there is no manifest PUT and no `latest/frame.json` PUT. The previous manifest stays. The partial `clean/<ts>/` prefix is left unreferenced and is removed by retention after the grace, unless a later publish of that same timestamp succeeds. That success cancels the pending delete. The loop continues with the next frame.

A latest-stage failure does not PUT `latest/frame.json`. The previous manifest body is PUT back so the published manifest still names the previous complete frame.

### 404 on a removed latest tile

Step 7 deletes latest keys the new frame did not write. Those tiles are empty in the new frame. A client that is still showing the previous latest can 404 on them. Treat HTTP 404 on a latest tile as transparent, the same as an empty PNG. Deletes are free. This prune runs only on the gated publish path, and only when the latest alias is on.

When `MPWG_PUBLISH_LATEST_ALIAS=0`, steps 4–7 are skipped. Clients should follow the manifest `latest` template, which is the timestamped prefix of the tip (`…/clean/<ts>/{z}/{x}/{y}.png`), not `latest/`.

### Composite must not publish an in-flight RALA frame

The pre-upload local write does not advance `products.rala.latest_frame` or the frames list when `MPWG_ATOMIC_PUBLISH` or `MPWG_REVIEW_MANIFEST_KEY` is set. Only the post-VERIFY merge, under `manifest_lock`, adds the frame. A composite cook that merges during the upload still sees the previous verified tip.

With `MPWG_REVIEW_MANIFEST_KEY` set, the review frame is not written into `radar/manifest.json` at all. The review document is self-contained: `label: NOT PRODUCTION`, `latest_frame`, `frames` (id, valid_time, tiles template, frame.json, oldest→newest, verified only), palette, min/max zoom, retention, `updated_at`. `latest` follows the alias flag.

## Retention

Local frame directories are removed only after that frame's publish has finished.

Remote frame prefixes are deleted only after a manifest that no longer lists them has been published, and only after the grace (at least 10 minutes), so a client mid-loop does not hit a deleted frame. Pending deletes are stored in `radar/.publish-state.json`. A restart keeps the original `delete_after` and does not delete twice.

A timestamp that publishes successfully is removed from that pending list. A failed upload of `clean/<ts>/` followed by a successful retry of the same timestamp does not delete the live frame when the original grace expires. At delete time the cooker re-reads the current review manifest, or root `manifest.json` when the review key is unset, under `manifest_lock`, and skips any prefix that manifest lists.

On the gated review path two further rules apply. Catch-up does not cook a scan that would already be outside the retention window when it publishes. Remote retention also lists `clean/` under the configured review prefix and deletes objects older than the window plus grace that the manifest does not name, including prefixes missing from the state file. Any prefix containing `_opt_validate` or `_atomic_test`, and anything outside the configured review prefix, is not deleted. With the flags unset, the production cooker does not list or reconcile.

## Memory

The publisher streams each object from disk and does not keep tile bytes across frames. The STRUCTURE field and dBZ grids are released before the next prepare. Each stage logs `memory_sample` with `rss_high_water_bytes` and `cgroup_current_bytes` (null when the cgroup file is not readable) at start and end. Each frame logs one `frame_timing` JSON line: detect, download, decode, prepare start/end, tiles start/end, enqueue, queue wait, publish start, frame PUT done, verify done, manifest PUT, latest done.

The review box has 16 GiB. Target peak with two frames in flight is ≤ 9 GiB. Confirm that from `memory_sample` on the m8g. CI does not measure host RSS.

## m8g review cooker

Run this by hand from this branch. Do not `systemctl edit` the public RALA unit. Do not export these into `/etc/mpwg-radar.env`.

```bash
cd /opt/mpwg-radar
sudo -u mpwg env \
  MPWG_PIPELINE=1 \
  MPWG_ATOMIC_PUBLISH=1 \
  MPWG_PUBLISH_LATEST_ALIAS=0 \
  MPWG_REVIEW_MANIFEST_KEY=rala-review/structure_v1/manifest.json \
  MPWG_RALA_DBZ_INTERP=structure_v1 \
  MPWG_PRODUCT=rala \
  MPWG_TILE_WORKERS=4 \
  /opt/mpwg-radar/.venv/bin/mpwg-radar cook --product rala
```

`MPWG_PUBLISH_LATEST_ALIAS=0` is the gated review setting: no `latest/` alias, manifest `latest` points at the timestamped tip. Omit the live archive (pass `--grib`) for a single frame. Do not point this process at the t4g production output directory.

The live golden regression and any R2 proof stay on the m8g. CI does not call R2 or NOAA.

## What a later t4g public deploy involves

1. Ship this code with every flag above **unset** (and with `MPWG_RALA_DBZ_INTERP` unset).
2. Restart the public cooker. The restart is required for the process to pick up the new code. With the flags unset, the production path is unchanged: composite and `rala/clean/latest` keep today's upload order and the `latest/` alias.
3. Production stays on `latest/` until a separate public flip. That flip is not this deploy. It would set `MPWG_PUBLISH_LATEST_ALIAS=0` (and, if the atomic order should apply to the public RALA cooker, `MPWG_ATOMIC_PUBLISH=1`) and restart again. Do not set `MPWG_REVIEW_MANIFEST_KEY` or `MPWG_RALA_DBZ_INTERP` on the public cooker. Do not set `MPWG_PIPELINE` on the t4g box unless that host is the one meant to overlap upload and process.
4. If the public flip also turns on atomic publish with the alias still on, stale `latest/` keys are deleted after each new frame. Clients must treat 404 on a removed latest tile as transparent before that flip.
