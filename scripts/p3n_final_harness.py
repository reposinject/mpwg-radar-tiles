#!/usr/bin/env python3
"""Final RALA reconstruction harness. Offline only. Does not cook or upload.

Order for every candidate: native dBZ, constrained numerical reconstruction,
dense sampling, p3k, RGBA. NO-ECHO is never a numeric sample. RGB is not blurred.

Same frame and the same geographic windows as the tight review crops.
The intense ≥65 core plants 68 dBZ on the observed Dickinson peak because this
CONUS frame has no cell ≥ 65. That plant is labeled synthetic.

  PYTHONPATH=src python3 scripts/p3n_final_harness.py \\
    --grib MRMS_ReflectivityAtLowestAltitude_00.50_20260929-004243.grib2.gz \\
    --out docs/p3n-dbz-interp/judge/final-harness
"""

from __future__ import annotations

import argparse
import csv
import json
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from mpwg_radar.dbz_interp_offline import (
    CANDIDATE_BOUNDED_CUBIC,
    CANDIDATE_MONOTONE,
    CANDIDATE_PEAK_HOLD,
    CANDIDATE_TIGHT,
    local_peak_mask,
    sample_dbz_candidate,
    width_10_90,
)
from mpwg_radar.geo import BBox
from mpwg_radar.grib import ReflectivityFrame, decode_grib2
from mpwg_radar.palette import load_palette
from mpwg_radar.products import CAT_NO_ECHO, CAT_VALID, get_product
from mpwg_radar.qc import apply_mode
from mpwg_radar.tiles import SPATIAL_REVISION, sample_nearest_many

# Exact windows from scripts/p3n_dbz_interp_ab.py.
DICKINSON = BBox(-103.15, 46.70, -102.40, 47.25, "dickinson-tight")
BELLE = BBox(-103.98, 44.50, -103.45, 44.80, "belle-fourche-tight")
DICKINSON_CORE = BBox(-102.98, 46.86, -102.72, 47.06, "intense65-core")

METHODS = (
    ("raw", "CONTROL RAW", None),
    ("A", "A  bilinear + peak hold", CANDIDATE_PEAK_HOLD),
    ("B", "B  tight peak hold", CANDIDATE_TIGHT),
    ("C", "C  bounded cubic", CANDIDATE_BOUNDED_CUBIC),
    ("D", "D  monotone cubic", CANDIDATE_MONOTONE),
)

# Cooker writes 512 px for a standard 256 CSS z9 tile, so one native cell is
# about 7.3 tile pixels E–W (3.6 CSS px). This harness samples the
# reconstruction on a finer lat/lon grid. It is not a Mapbox screenshot.
TILE_PX = 512
CSS_PX = 256
Z9_CSS_PX_PER_CELL_EW = 3.65


def _font(size: int, bold: bool = False):
    name = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
    try:
        return ImageFont.truetype(f"/usr/share/fonts/truetype/dejavu/{name}", size)
    except OSError:
        return ImageFont.load_default()


def _rala_clean(frame: ReflectivityFrame) -> ReflectivityFrame:
    product = get_product("rala")
    return apply_mode(
        frame,
        "clean",
        min_dbz=product.min_dbz_override,
        apply_dbz_floor=product.apply_dbz_floor,
        apply_despeckle=product.apply_despeckle,
        edge_aware=product.edge_aware_smooth,
        apply_grid_smooth=product.apply_grid_smooth,
    )


def _query_grid(frame, bbox: BBox, samples: int):
    dlat = abs(float(frame.lat[1] - frame.lat[0]))
    dlon = abs(float(frame.lon[1] - frame.lon[0]))
    step_lat = dlat / samples
    step_lon = dlon / samples
    lats = np.arange(bbox.north - step_lat / 2.0, bbox.south - 1e-12, -step_lat, dtype=np.float64)
    lons = np.arange(bbox.west + step_lon / 2.0, bbox.east + 1e-12, step_lon, dtype=np.float64)
    lats = lats[(lats <= bbox.north) & (lats >= bbox.south)]
    lons = lons[(lons >= bbox.west) & (lons <= bbox.east)]
    qlon, qlat = np.meshgrid(lons, lats)
    return qlat, qlon


def _sample(frame, key, qlat, qlon, peak_mask):
    if key == "raw":
        dbz_s, cat_s = sample_nearest_many(
            frame.lat,
            frame.lon,
            qlat,
            qlon,
            frame.dbz,
            frame.category.astype(np.float32),
        )
        cat = np.zeros(dbz_s.shape, dtype=np.uint8)
        ok = np.isfinite(cat_s)
        cat[ok] = np.rint(cat_s[ok]).astype(np.uint8)
        dbz = np.array(dbz_s, dtype=np.float32, copy=True)
        dbz[cat != CAT_VALID] = np.float32(np.nan)
        return dbz, cat
    name = dict(A=CANDIDATE_PEAK_HOLD, B=CANDIDATE_TIGHT, C=CANDIDATE_BOUNDED_CUBIC, D=CANDIDATE_MONOTONE)[key]
    mask = peak_mask if key in ("A", "B") else None
    return sample_dbz_candidate(
        name, frame.dbz, frame.lat, frame.lon, qlat, qlon, frame.category, peak_mask=mask
    )


def _on_dark(rgba: np.ndarray) -> np.ndarray:
    bg = np.zeros_like(rgba)
    bg[..., 0] = 10
    bg[..., 1] = 18
    bg[..., 2] = 32
    bg[..., 3] = 255
    alpha = rgba[..., 3:4].astype(np.float32) / 255.0
    out = bg.copy()
    out[..., :3] = np.clip(
        np.rint(rgba[..., :3].astype(np.float32) * alpha + bg[..., :3] * (1.0 - alpha)),
        0,
        255,
    ).astype(np.uint8)
    return out[..., :3]


def _gray_u16(dbz: np.ndarray) -> np.ndarray:
    """Linear map −20..80 dBZ → 1..65535. Missing stays 0."""
    out = np.zeros(dbz.shape, dtype=np.uint16)
    finite = np.isfinite(dbz)
    scaled = (dbz.astype(np.float64) - (-20.0)) / 100.0 * 65534.0 + 1.0
    out[finite] = np.clip(np.rint(scaled[finite]), 1, 65535).astype(np.uint16)
    return out


def _gray_preview(dbz: np.ndarray) -> np.ndarray:
    preview = np.zeros(dbz.shape + (3,), dtype=np.uint8)
    finite = np.isfinite(dbz)
    tone = np.clip((np.asarray(dbz, dtype=np.float64)[finite] - 0.0) / 70.0, 0.0, 1.0)
    level = np.rint(tone * 255.0).astype(np.uint8)
    preview[finite, 0] = level
    preview[finite, 1] = level
    preview[finite, 2] = level
    return preview


def _cells_in(frame, bbox: BBox):
    rows = np.flatnonzero((frame.lat >= bbox.south) & (frame.lat <= bbox.north))
    cols = np.flatnonzero((frame.lon >= bbox.west) & (frame.lon <= bbox.east))
    return rows, cols


def _center_table(frame, bbox, sampled_at_centers, cat_at_centers):
    rows, cols = _cells_in(frame, bbox)
    records = []
    if rows.size == 0 or cols.size == 0:
        return records
    # sampled_at_centers is aligned to the full frame's in-bbox mesh in row-major
    # order of rows x cols. Caller passes that mesh.
    k = 0
    for j in rows:
        for i in cols:
            source = float(frame.dbz[j, i]) if frame.category[j, i] == CAT_VALID else float("nan")
            recon = float(sampled_at_centers[k])
            err = recon - source if np.isfinite(recon) and np.isfinite(source) else float("nan")
            records.append(
                {
                    "lat": round(float(frame.lat[j]), 5),
                    "lon": round(float(frame.lon[i]), 5),
                    "category": int(frame.category[j, i]),
                    "source_dbz": None if not np.isfinite(source) else round(source, 3),
                    "recon_dbz": None if not np.isfinite(recon) else round(recon, 3),
                    "error_dbz": None if not np.isfinite(err) else round(err, 3),
                }
            )
            k += 1
    return records


def _metrics(frame, bbox, dense_dbz, dense_cat, center_rows):
    valid_src = []
    ge65 = []
    noecho = 0
    for j in range(frame.lat.size):
        if not (bbox.south <= float(frame.lat[j]) <= bbox.north):
            continue
        for i in range(frame.lon.size):
            if not (bbox.west <= float(frame.lon[i]) <= bbox.east):
                continue
            if frame.category[j, i] == CAT_VALID and np.isfinite(frame.dbz[j, i]):
                valid_src.append(float(frame.dbz[j, i]))
                if float(frame.dbz[j, i]) >= 65.0:
                    ge65.append((j, i, float(frame.dbz[j, i])))
            elif frame.category[j, i] == CAT_NO_ECHO:
                noecho += 1
    src = np.asarray(valid_src, dtype=np.float64)
    finite = np.isfinite(dense_dbz) & (dense_cat == CAT_VALID)
    recon = dense_dbz[finite].astype(np.float64)
    leaks = int(np.sum((dense_cat == CAT_NO_ECHO) & np.isfinite(dense_dbz)))
    center_err = [
        row["error_dbz"] for row in center_rows if row["error_dbz"] is not None and row["category"] == int(CAT_VALID)
    ]
    center_ge65_ok = 0
    for row in center_rows:
        if row["source_dbz"] is not None and row["source_dbz"] >= 65.0:
            if row["recon_dbz"] is not None and row["recon_dbz"] >= 65.0 - 1e-3:
                center_ge65_ok += 1
    src_min = float(np.min(src)) if src.size else None
    src_max = float(np.max(src)) if src.size else None
    rec_min = float(np.min(recon)) if recon.size else None
    rec_max = float(np.max(recon)) if recon.size else None
    over_hi = None if src_max is None or rec_max is None else max(0.0, rec_max - src_max)
    over_lo = None if src_min is None or rec_min is None else max(0.0, src_min - rec_min)
    weak_src = float(np.mean(src <= 10.0)) if src.size else None
    weak_rec = float(np.mean(recon <= 10.0)) if recon.size else None
    area_ge65_src = float(np.mean(src >= 65.0)) if src.size else None
    area_ge65_rec = float(np.mean(recon >= 65.0)) if recon.size else None
    return {
        "source_cells_valid": int(src.size),
        "source_cells_noecho": int(noecho),
        "source_min_dbz": None if src_min is None else round(src_min, 3),
        "source_max_dbz": None if src_max is None else round(src_max, 3),
        "recon_min_dbz": None if rec_min is None else round(rec_min, 3),
        "recon_max_dbz": None if rec_max is None else round(rec_max, 3),
        "overshoot_high_dbz": None if over_hi is None else round(over_hi, 4),
        "overshoot_low_dbz": None if over_lo is None else round(over_lo, 4),
        "center_mae_dbz": None if not center_err else round(float(np.mean(np.abs(center_err))), 4),
        "center_max_abs_dbz": None if not center_err else round(float(np.max(np.abs(center_err))), 4),
        "source_ge65_cells": len(ge65),
        "centers_ge65_still_ge65": center_ge65_ok,
        "source_area_fraction_ge65": None if area_ge65_src is None else round(area_ge65_src, 4),
        "recon_area_fraction_ge65": None if area_ge65_rec is None else round(area_ge65_rec, 4),
        "noecho_numeric_leaks": leaks,
        "source_weak_fraction_le_10": None if weak_src is None else round(weak_src, 4),
        "recon_weak_fraction_le_10": None if weak_rec is None else round(weak_rec, 4),
        "weak_fraction_change": None
        if weak_src is None or weak_rec is None
        else round(weak_rec - weak_src, 4),
    }


def _flatness(frame, bbox, key, peak_mask):
    """Share of the inner 0.20 cell still within 0.5 dBZ of the source, on steps ≥ 8."""
    hits = []
    peak_hits = []
    height, width = frame.dbz.shape
    for j in range(1, height - 1):
        if not (bbox.south <= float(frame.lat[j]) <= bbox.north):
            continue
        for i in range(1, width - 1):
            if not (bbox.west <= float(frame.lon[i]) <= bbox.east):
                continue
            if frame.category[j, i] != CAT_VALID:
                continue
            step = 0.0
            for dj, di in ((0, 1), (0, -1), (1, 0), (-1, 0)):
                if frame.category[j + dj, i + di] != CAT_VALID:
                    continue
                step = max(step, abs(float(frame.dbz[j + dj, i + di]) - float(frame.dbz[j, i])))
            if step < 8.0:
                continue
            offsets = np.array([0.05, 0.10, 0.15, 0.20], dtype=np.float64)
            qlat = np.full(offsets.size, float(frame.lat[j]))
            qlon = float(frame.lon[i]) + offsets * float(frame.lon[1] - frame.lon[0])
            sampled, _cat = _sample(frame, key, qlat[:, None], qlon[:, None], peak_mask)
            err = np.abs(np.asarray(sampled).reshape(-1) - float(frame.dbz[j, i]))
            hits.append(float(np.mean(err <= 0.5)))
            if bool(peak_mask[j, i]) and float(frame.dbz[j, i]) >= 50.0:
                peak_hits.append(float(np.mean(err <= 0.5)))
    def _med(xs):
        return None if not xs else round(float(np.median(xs)), 3)
    return {
        "inner_flat_fraction_steps_ge_8": _med(hits),
        "n_steps": len(hits),
        "inner_flat_fraction_peaks_ge_50": _med(peak_hits),
        "n_peaks_ge_50": len(peak_hits),
    }


def _synthetic_widths():
    """Controlled 1D steps. Real crops rarely contain a clean 60→65 pair."""
    levels = [20, 30, 40, 50, 60, 70]
    lat = np.arange(35.2, 34.8, -0.01, dtype=np.float64)
    lon = np.arange(-100.0, -100.0 + 0.01 * len(levels), 0.01, dtype=np.float64)[: len(levels)]
    dbz = np.broadcast_to(np.array(levels, dtype=np.float32), (lat.size, len(levels))).copy()
    cat = np.full(dbz.shape, CAT_VALID, dtype=np.uint8)
    frame = ReflectivityFrame(
        dbz=dbz,
        lat=lat,
        lon=lon,
        valid_time=datetime(2026, 9, 29, tzinfo=timezone.utc),
        product="synthetic-staircase",
        source="synthetic",
        category=cat,
    )
    # The staircase frame has no valid_time; the sampler does not read it.
    peak_mask = local_peak_mask(dbz, cat)
    out = {"layout": "one cell per level, 20 then 30 then 40 then 50 then 60 then 70", "segments": {}}
    for i, (lo, hi) in enumerate(zip(levels, levels[1:])):
        label = f"{lo}->{hi if hi < 65 else '>=65'}"
        qlat = np.full(401, float(lat[lat.size // 2]))
        qlon = np.linspace(float(lon[i]), float(lon[i + 1]), 401)
        seg = {}
        for key, _title, _name in METHODS:
            sampled, _c = _sample(frame, key, qlat[:, None], qlon[:, None], peak_mask)
            y = np.asarray(sampled).reshape(-1)
            seg[key] = {
                "width_10_90_cells": round(float(width_10_90(y)), 3),
                "min_dbz": round(float(np.nanmin(y)), 3),
                "max_dbz": round(float(np.nanmax(y)), 3),
            }
        out["segments"][label] = seg
    # Flat plateaus on both sides of one step. This is the block edge.
    flat_levels = [20, 20, 30, 30]
    lon2 = np.arange(-100.0, -100.0 + 0.01 * 4, 0.01)[:4]
    dbz2 = np.broadcast_to(np.array(flat_levels, dtype=np.float32), (lat.size, 4)).copy()
    cat2 = np.full(dbz2.shape, CAT_VALID, dtype=np.uint8)
    frame2 = ReflectivityFrame(
        dbz=dbz2,
        lat=lat,
        lon=lon2,
        valid_time=frame.valid_time,
        product="synthetic-step",
        source="synthetic",
        category=cat2,
    )
    mask2 = local_peak_mask(dbz2, cat2)
    qlat = np.full(401, float(lat[lat.size // 2]))
    qlon = np.linspace(float(lon2[1]), float(lon2[2]), 401)
    flat = {}
    for key, _title, _name in METHODS:
        sampled, _c = _sample(frame2, key, qlat[:, None], qlon[:, None], mask2)
        y = np.asarray(sampled).reshape(-1)
        flat[key] = {
            "width_10_90_cells": round(float(width_10_90(y)), 3),
            "min_dbz": round(float(np.nanmin(y)), 3),
            "max_dbz": round(float(np.nanmax(y)), 3),
        }
    out["flat_to_flat_20_30"] = flat
    return out


def _plant_intense(frame: ReflectivityFrame) -> ReflectivityFrame:
    dbz = np.array(frame.dbz, copy=True)
    j = int(np.argmin(np.abs(frame.lat - 46.945)))
    i = int(np.argmin(np.abs(frame.lon - (-102.855))))
    original = float(dbz[j, i])
    dbz[j, i] = np.float32(68.0)
    planted = ReflectivityFrame(
        dbz=dbz,
        lat=frame.lat,
        lon=frame.lon,
        valid_time=frame.valid_time,
        product=frame.product,
        source=frame.source + " + synthetic 68 on the Dickinson peak",
        category=np.array(frame.category, copy=True),
    )
    planted.plant_note = {  # type: ignore[attr-defined]
        "lat": float(frame.lat[j]),
        "lon": float(frame.lon[i]),
        "original_dbz": original,
        "planted_dbz": 68.0,
        "reason": "This CONUS frame has no observed cell >= 65. The plant is not observed dBZ.",
    }
    return planted


def _draw_centers(rgb: np.ndarray, qlat, qlon, centers) -> np.ndarray:
    im = Image.fromarray(rgb)
    draw = ImageDraw.Draw(im)
    if qlat.size == 0:
        return np.asarray(im)
    north = float(qlat[0, 0])
    south = float(qlat[-1, 0])
    west = float(qlon[0, 0])
    east = float(qlon[0, -1])
    span_lat = north - south
    span_lon = east - west
    if span_lat <= 0 or span_lon <= 0:
        return np.asarray(im)
    h, w = rgb.shape[:2]
    for lat, lon in centers[:: max(1, len(centers) // 400)]:
        y = int(round((north - lat) / span_lat * (h - 1)))
        x = int(round((lon - west) / span_lon * (w - 1)))
        draw.ellipse((x - 1, y - 1, x + 1, y + 1), fill=(255, 220, 80))
    return np.asarray(im)


def _sheet(panels, banner: str, note: str) -> Image.Image:
    labeled = []
    for image, title in panels:
        image = np.asarray(image)
        header = 36
        canvas = Image.new("RGB", (image.shape[1], image.shape[0] + header), (8, 12, 20))
        canvas.paste(Image.fromarray(image), (0, header))
        draw = ImageDraw.Draw(canvas)
        draw.text((8, 8), title, fill=(255, 255, 255), font=_font(16, bold=True))
        labeled.append(canvas)
    gap = 10
    margin = 16
    banner_h = 64
    footer_h = 72
    width = margin * 2 + sum(p.width for p in labeled) + gap * (len(labeled) - 1)
    height = banner_h + max(p.height for p in labeled) + footer_h
    canvas = Image.new("RGB", (width, height), (8, 12, 20))
    draw = ImageDraw.Draw(canvas)
    draw.text((margin, 8), banner, fill=(255, 255, 255), font=_font(22, bold=True))
    draw.text((margin, 36), "NOT PRODUCTION    flag default OFF    do not merge    do not deploy", fill=(255, 196, 64), font=_font(14, bold=True))
    x = margin
    y = banner_h
    for panel in labeled:
        canvas.paste(panel, (x, y))
        x += panel.width + gap
    draw.text((margin, height - 58), note, fill=(176, 196, 214), font=_font(13))
    draw.text(
        (margin, height - 34),
        "Cooker tiles stay 512 px at native z9. This picture is the dense reconstruction, not a second Mapbox blur.",
        fill=(255, 196, 64),
        font=_font(13, bold=True),
    )
    return canvas


def _write_csv(path: Path, rows: list):
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = ["lat", "lon", "category", "source_dbz", "recon_dbz", "error_dbz"]
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def _jsonable(value):
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, (np.floating,)):
        return float(value)
    if isinstance(value, (np.integer,)):
        return int(value)
    return value


def render_site(frame, palette, bbox, samples, peak_mask, out_dir: Path, site_key: str):
    qlat, qlon = _query_grid(frame, bbox, samples)
    rows, cols = _cells_in(frame, bbox)
    if rows.size and cols.size:
        clat = frame.lat[rows]
        clon = frame.lon[cols]
        cqlon, cqlat = np.meshgrid(clon, clat)
    else:
        cqlat = np.zeros((0, 0))
        cqlon = cqlat
    centers = [(float(frame.lat[j]), float(frame.lon[i])) for j in rows for i in cols]
    print(f"  {site_key} grid {qlat.shape} samples/cell {samples}")
    p3k_panels = []
    gray_panels = []
    per = {}
    site_dir = out_dir / site_key
    site_dir.mkdir(parents=True, exist_ok=True)
    for key, title, _name in METHODS:
        dense, dense_cat = _sample(frame, key, qlat, qlon, peak_mask)
        if cqlat.size:
            at_centers, _cc = _sample(frame, key, cqlat, cqlon, peak_mask)
            center_rows = _center_table(frame, bbox, np.asarray(at_centers).reshape(-1), None)
        else:
            center_rows = []
        stats = _metrics(frame, bbox, dense, dense_cat, center_rows)
        stats["flatness"] = _flatness(frame, bbox, key, peak_mask)
        stats["samples_per_cell"] = samples
        stats["image_hw"] = [int(qlat.shape[0]), int(qlat.shape[1])]
        rgba = palette.colorize(dense, category=dense_cat)
        p3k = _on_dark(rgba)
        gray = _gray_preview(dense)
        u16 = _gray_u16(dense)
        Image.fromarray(u16).save(site_dir / f"{key}_numerical_u16.png")
        Image.fromarray(gray).save(site_dir / f"{key}_numerical_preview.png")
        Image.fromarray(p3k).save(site_dir / f"{key}_p3k.png")
        overlay = _draw_centers(p3k, qlat, qlon, centers)
        Image.fromarray(overlay).save(site_dir / f"{key}_centers.png")
        _write_csv(site_dir / f"{key}_centers.csv", center_rows)
        # Error field versus nearest source, diverging around 0. ±15 dBZ.
        nearest, nearest_cat = _sample(frame, "raw", qlat, qlon, peak_mask)
        err = np.asarray(dense, dtype=np.float64) - np.asarray(nearest, dtype=np.float64)
        err_rgb = np.zeros(dense.shape + (3,), dtype=np.uint8)
        show = np.isfinite(err) & (nearest_cat == CAT_VALID)
        mag = np.clip(err / 15.0, -1.0, 1.0)
        err_rgb[show, 0] = np.rint(np.clip(mag[show], 0, 1) * 255).astype(np.uint8)
        err_rgb[show, 2] = np.rint(np.clip(-mag[show], 0, 1) * 255).astype(np.uint8)
        err_rgb[show, 1] = 40
        Image.fromarray(err_rgb).save(site_dir / f"{key}_error.png")
        per[key] = stats
        p3k_panels.append((p3k, title))
        gray_panels.append((gray, title))
        print(
            f"    {key} src {stats['source_min_dbz']}..{stats['source_max_dbz']} "
            f"recon {stats['recon_min_dbz']}..{stats['recon_max_dbz']} "
            f"overshoot {stats['overshoot_high_dbz']}/{stats['overshoot_low_dbz']} "
            f"center mae {stats['center_mae_dbz']} leaks {stats['noecho_numeric_leaks']} "
            f"flat {stats['flatness']['inner_flat_fraction_steps_ge_8']}"
        )
    note = (
        f"{frame.frame_id}  {samples} samples/cell  {qlat.shape[1]}×{qlat.shape[0]} px  "
        f"z9 cooker tile is {TILE_PX}px ({CSS_PX} CSS). ~{Z9_CSS_PX_PER_CELL_EW} CSS px/cell E–W."
    )
    _sheet(p3k_panels, f"{bbox.name}  p3k", note).save(out_dir / f"{site_key}_p3k_compare.png")
    _sheet(gray_panels, f"{bbox.name}  numerical dBZ", note).save(out_dir / f"{site_key}_numerical_compare.png")
    return {
        "bbox": bbox.as_dict(),
        "samples_per_cell": samples,
        "image_hw": [int(qlat.shape[0]), int(qlat.shape[1])],
        "methods": per,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--grib", type=Path, required=True)
    parser.add_argument("--out", type=Path, default=Path("docs/p3n-dbz-interp/judge/final-harness"))
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    palette = load_palette("mpwg-rala-2026-09")
    if palette.version != "2026-09-rala-p3k":
        raise SystemExit(palette.version)
    if SPATIAL_REVISION != "p3l":
        raise SystemExit(SPATIAL_REVISION)
    region = BBox(-104.10, 44.40, -102.30, 47.35, "nd-sd")
    print("decoding", args.grib)
    frame = _rala_clean(decode_grib2(args.grib, bbox=region, product="ReflectivityAtLowestAltitude"))
    print(frame.frame_id, frame.dbz.shape, "max", float(np.nanmax(frame.dbz)))
    peak_mask = local_peak_mask(frame.dbz, frame.category)
    intense = _plant_intense(frame)
    intense_mask = local_peak_mask(intense.dbz, intense.category)
    print("transitions")
    transitions = _synthetic_widths()
    print("sites")
    sites = {
        "dickinson_tight": render_site(frame, palette, DICKINSON, 8, peak_mask, args.out, "dickinson_tight"),
        "belle_fourche_tight": render_site(frame, palette, BELLE, 8, peak_mask, args.out, "belle_fourche_tight"),
        "intense65_core": render_site(intense, palette, DICKINSON_CORE, 8, intense_mask, args.out, "intense65_core"),
        "intense65_core_16x": render_site(intense, palette, DICKINSON_CORE, 16, intense_mask, args.out, "intense65_core_16x"),
    }
    report = {
        "frame_id": frame.frame_id,
        "not_production": True,
        "flag_default": "off",
        "spatial": SPATIAL_REVISION,
        "palette": palette.version,
        "order": "native dBZ → constrained reconstruction → dense sampling → p3k → RGBA",
        "tile_resolution": {
            "cooker_z9_tile_px": TILE_PX,
            "css_tile_px": CSS_PX,
            "approx_css_px_per_cell_ew_at_z9": Z9_CSS_PX_PER_CELL_EW,
            "harness": "8 samples per native 0.01° cell, and 16 on the intense core",
            "mapbox_second_blur": (
                "Publishing only the z9 512px PNG leaves Mapbox to linearly filter "
                "those pixels when the map zooms past 9. That second filter widens "
                "whatever ramp is in the tile. This harness is the reconstruction "
                "itself, before that display filter. It is not an app screenshot."
            ),
        },
        "intense_plant": intense.plant_note,
        "transitions": transitions,
        "sites": sites,
        "methods": {
            "raw": "Nearest native cell. The control. Interiors are constant because the source cell is constant.",
            "A": "Masked bilinear, with the p3l seam held on local maxima (including tied plateaus). 10–90% width 0.80 cell on a plain ramp. The hold keeps a flat core out to 0.28 cell.",
            "B": "Same hold as A. The bilinear fraction uses power 2, so a plain ramp's 10–90% width is 0.50 cell. Peaks are still the flat p3l core.",
            "C": "Catmull-Rom on valid taps, kept only when the value is already inside the 2×2 cell-center range. Otherwise the masked bilinear sample is left in place, so a clip cannot stamp a shelf. Centers match. NO-ECHO is not a tap.",
            "D": "Successive monotone cubic Hermite. Slopes are zero at a local extremum and capped so each segment stays between its endpoints. The peak is the source value at the cell center and is not held across a core. Centers match. NO-ECHO is not a tap.",
        },
    }
    (args.out / "metrics.json").write_text(json.dumps(_jsonable(report), indent=2) + "\n")
    print("wrote", args.out)


if __name__ == "__main__":
    main()
