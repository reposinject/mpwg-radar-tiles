#!/usr/bin/env python3
"""Offline A/B: bounded numerical dBZ interpolation before the p3k LUT.

Does not cook, upload, or change production stamps. Reads one MRMS RALA
GRIB and writes crops plus metrics under --out.

``--judge-only`` writes four labeled side-by-side PNGs (current p3l vs
bilinear + peak hold) and skips the metric sweep.

Example:
  python3 scripts/p3n_dbz_interp_ab.py \\
    --grib data/MRMS_ReflectivityAtLowestAltitude_00.50_20260929-004243.grib2.gz \\
    --out docs/p3n-dbz-interp
"""

from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from mpwg_radar.dbz_interp_offline import (
    CANDIDATE_BICUBIC,
    CANDIDATE_BILINEAR,
    CANDIDATE_PEAK_HOLD,
    PRODUCTION_NAME,
    Z9_HALF_PIXEL_EW,
    Z9_HALF_PIXEL_NS,
    local_peak_mask,
    paint_candidate_tile,
    sample_and_paint,
    sample_dbz_candidate,
)
from mpwg_radar.geo import BBox, latlon_to_global_xy, tiles_for_bbox
from mpwg_radar.grib import ReflectivityFrame, decode_grib2
from mpwg_radar.palette import load_palette
from mpwg_radar.products import CAT_NO_ECHO, CAT_VALID, get_product
from mpwg_radar.qc import apply_mode
from mpwg_radar.tiles import SPATIAL_REVISION, sample_masked_splat, sample_nearest_many

# Dickinson / Belle Fourche window on the frame James reviewed.
# Overzoom matches the p3n measurement (Mapbox z ≈ 10.45 on native z9 tiles).
COMPARE_ZOOM = 10.45
TILE_ZOOM = 9
OVERZOOM = 2 ** (COMPARE_ZOOM - TILE_ZOOM)

DICKINSON = BBox(-103.15, 46.70, -102.40, 47.25, "dickinson")
DICKINSON_CORE = BBox(-102.98, 46.86, -102.72, 47.06, "dickinson-core")
BELLE = BBox(-103.98, 44.50, -103.45, 44.80, "belle-fourche")

METHOD_ORDER = (
    PRODUCTION_NAME,
    CANDIDATE_BILINEAR,
    CANDIDATE_BICUBIC,
    CANDIDATE_PEAK_HOLD,
)
METHOD_TITLE = {
    PRODUCTION_NAME: "p3l production",
    CANDIDATE_BILINEAR: "bilinear on dBZ",
    CANDIDATE_BICUBIC: "bicubic clipped",
    CANDIDATE_PEAK_HOLD: "bilinear + peak hold",
    "nearest": "nearest cell",
    CANDIDATE_PEAK_HOLD + "+65": "peak hold + dBZ≥65",
}


def _font(size: int, bold: bool = False) -> ImageFont.ImageFont:
    name = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
    path = f"/usr/share/fonts/truetype/dejavu/{name}"
    try:
        return ImageFont.truetype(path, size)
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


def _max_ortho_step(dbz: np.ndarray, valid: np.ndarray) -> np.ndarray:
    step = np.zeros(dbz.shape, dtype=np.float32)
    both_h = valid[:, 1:] & valid[:, :-1]
    delta_h = np.abs(dbz[:, 1:] - dbz[:, :-1])
    step[:, 1:] = np.maximum(step[:, 1:], np.where(both_h, delta_h, 0))
    step[:, :-1] = np.maximum(step[:, :-1], np.where(both_h, delta_h, 0))
    both_v = valid[1:, :] & valid[:-1, :]
    delta_v = np.abs(dbz[1:, :] - dbz[:-1, :])
    step[1:, :] = np.maximum(step[1:, :], np.where(both_v, delta_v, 0))
    step[:-1, :] = np.maximum(step[:-1, :], np.where(both_v, delta_v, 0))
    return step


def _peak_contrast(dbz: np.ndarray, valid: np.ndarray, peaks: np.ndarray) -> np.ndarray:
    filled = np.where(valid, dbz, np.float32(-1e30))
    padded = np.pad(filled, 1, mode="constant", constant_values=np.float32(-1e30))
    height, width = dbz.shape
    neighbor = np.full(dbz.shape, np.float32(-1e30))
    for dj in (-1, 0, 1):
        for di in (-1, 0, 1):
            if dj == 0 and di == 0:
                continue
            window = padded[1 + dj : 1 + dj + height, 1 + di : 1 + di + width]
            neighbor = np.maximum(neighbor, window)
    contrast = np.full(dbz.shape, np.nan, dtype=np.float32)
    contrast[peaks] = dbz[peaks] - neighbor[peaks]
    # Isolated echo has no real neighbor. Contrast is not a blend risk.
    contrast[peaks & (neighbor < np.float32(-1e20))] = np.float32(0.0)
    return contrast


def _sample(frame, name, qlat, qlon, peak_mask):
    """Sample dBZ. 1-D queries are promoted so the p3l splat sees a 2-D mask."""
    qlat = np.asarray(qlat, dtype=np.float64)
    qlon = np.asarray(qlon, dtype=np.float64)
    flat = qlat.ndim == 1
    if flat:
        qlat = qlat[:, None]
        qlon = qlon[:, None]
    if name == PRODUCTION_NAME:
        sampled, cat, _edge = sample_masked_splat(
            frame.dbz, frame.lat, frame.lon, qlat, qlon, frame.category
        )
    else:
        mask = peak_mask if name == CANDIDATE_PEAK_HOLD else None
        sampled, cat = sample_dbz_candidate(
            name,
            frame.dbz,
            frame.lat,
            frame.lon,
            qlat,
            qlon,
            frame.category,
            peak_mask=mask,
        )
    if flat:
        return sampled[:, 0], cat[:, 0]
    return sampled, cat


def _z9_pixel_query(lat_c: np.ndarray, lon_c: np.ndarray):
    lat = np.clip(np.asarray(lat_c, dtype=np.float64), -85.05112878, 85.05112878)
    lon = np.asarray(lon_c, dtype=np.float64)
    n = float(1 << TILE_ZOOM)
    tile = 512.0
    x = (lon + 180.0) / 360.0 * n
    y = (1.0 - np.arcsinh(np.tan(np.radians(lat))) / np.pi) / 2.0 * n
    px = x * tile
    py = y * tile
    cx = np.floor(px) + 0.5
    cy = np.floor(py) + 0.5
    gx = cx / tile
    gy = cy / tile
    qlon = gx / n * 360.0 - 180.0
    qlat = np.degrees(np.arctan(np.sinh(np.pi * (1.0 - 2.0 * gy / n))))
    return qlat, qlon


def _cell_index(frame: ReflectivityFrame, lat0: float, lon0: float):
    j = int(np.argmin(np.abs(frame.lat - lat0)))
    i = int(np.argmin(np.abs(frame.lon - lon0)))
    return j, i


def _inside(frame: ReflectivityFrame, bbox: BBox):
    rows = (frame.lat >= bbox.south) & (frame.lat <= bbox.north)
    cols = (frame.lon >= bbox.west) & (frame.lon <= bbox.east)
    return rows, cols


def evaluate_site(frame, name, bbox: BBox, peak_mask: np.ndarray) -> dict:
    """Numeric A/B on one geographic window. dBZ only, no color."""
    rows, cols = _inside(frame, bbox)
    dbz = frame.dbz
    cat = frame.category
    valid = (cat == CAT_VALID) & np.isfinite(dbz)
    in_view = np.zeros(dbz.shape, dtype=bool)
    in_view[np.ix_(rows, cols)] = True
    view_valid = valid & in_view
    step = _max_ortho_step(dbz, valid)
    contrast = _peak_contrast(dbz, valid, peak_mask)
    visible_peaks = view_valid & peak_mask & (contrast >= 2.0)
    magenta = view_valid & (dbz >= 65.0)
    weak = view_valid & (dbz <= 10.0)

    dlat = float(frame.lat[1] - frame.lat[0])
    dlon = float(frame.lon[1] - frame.lon[0])

    def _query_at(js, iss, dj, di):
        qlat = frame.lat[js] + dj * dlat
        qlon = frame.lon[iss] + di * dlon
        return np.asarray(qlat, dtype=np.float64), np.asarray(qlon, dtype=np.float64)

    # Cell centers.
    js, iss = np.nonzero(view_valid)
    qlat, qlon = _query_at(js, iss, 0.0, 0.0)
    center, _center_cat = _sample(frame, name, qlat, qlon, peak_mask)
    src = dbz[js, iss]
    center_err = np.abs(center - src)
    center_ok = np.isfinite(center)
    center_rmse = (
        float(np.sqrt(np.mean(np.square(center_err[center_ok])))) if np.any(center_ok) else None
    )

    # Interior of cells that step by ≥3 dBZ: the ones that can show a block.
    stepped = view_valid & (step >= 3.0)
    js_s, is_s = np.nonzero(stepped)
    offsets = np.array([-0.2, -0.1, 0.0, 0.1, 0.2], dtype=np.float64)
    du, dv = np.meshgrid(offsets, offsets, indexing="xy")
    du_f = du.ravel()
    dv_f = dv.ravel()
    plateau_fraction = None
    subcell_rms = None
    n_stepped = int(js_s.size)
    if n_stepped:
        qlat_s = frame.lat[js_s][:, None] + dv_f[None, :] * dlat
        qlon_s = frame.lon[is_s][:, None] + du_f[None, :] * dlon
        sampled_s, _cat_s = _sample(
            frame, name, qlat_s.astype(np.float64), qlon_s.astype(np.float64), peak_mask
        )
        src_s = dbz[js_s, is_s][:, None]
        err = np.abs(sampled_s - src_s)
        finite = np.isfinite(sampled_s)
        if np.any(finite):
            plateau_fraction = float(np.mean(err[finite] <= 0.05))
            subcell_rms = float(np.sqrt(np.mean(np.square(err[finite]))))

    def _z9_loss(mask: np.ndarray) -> dict:
        js_p, is_p = np.nonzero(mask)
        empty = {
            "count": 0,
            "worst_loss_dbz": None,
            "within_0_5_dbz_fraction": None,
            "still_ge_65_fraction": None,
        }
        if js_p.size == 0:
            return empty
        qlat_p, qlon_p = _z9_pixel_query(frame.lat[js_p], frame.lon[is_p])
        got, _got_cat = _sample(frame, name, qlat_p, qlon_p, peak_mask)
        raw = dbz[js_p, is_p].astype(np.float64)
        loss = raw - got.astype(np.float64)
        finite = np.isfinite(got)
        if not np.any(finite):
            return empty
        return {
            "count": int(js_p.size),
            "worst_loss_dbz": float(np.max(loss[finite])),
            "within_0_5_dbz_fraction": float(np.mean(loss[finite] <= 0.5)),
            "still_ge_65_fraction": float(np.mean(got[finite] >= 65.0)),
        }

    # Weak-cell area lift. Offsets stay inside the cell.
    weak_mean_lift = None
    weak_lift_gt3_fraction = None
    js_w, is_w = np.nonzero(weak)
    if js_w.size:
        w_off = np.linspace(-0.4, 0.4, 5)
        wdu, wdv = np.meshgrid(w_off, w_off, indexing="xy")
        qlat_w = frame.lat[js_w][:, None] + wdv.ravel()[None, :] * dlat
        qlon_w = frame.lon[is_w][:, None] + wdu.ravel()[None, :] * dlon
        sampled_w, _cat_w = _sample(
            frame, name, qlat_w.astype(np.float64), qlon_w.astype(np.float64), peak_mask
        )
        finite = np.isfinite(sampled_w)
        # Cells with no finite sample are skipped.
        lift = []
        for row, raw in zip(sampled_w, dbz[js_w, is_w]):
            good = row[np.isfinite(row)]
            if good.size:
                lift.append(float(np.mean(good) - raw))
        if lift:
            lift_a = np.array(lift, dtype=np.float64)
            weak_mean_lift = float(np.mean(lift_a))
            weak_lift_gt3_fraction = float(np.mean(lift_a > 3.0))

    # Clear-air centers must stay empty.
    no_echo = in_view & (cat == CAT_NO_ECHO)
    js_n, is_n = np.nonzero(no_echo)
    clear_leaks = 0
    if js_n.size:
        # Cap the check. A full view can be tens of thousands of empty cells.
        take = slice(0, min(js_n.size, 4000))
        qlat_n, qlon_n = _query_at(js_n[take], is_n[take], 0.0, 0.0)
        sampled_n, cat_n = _sample(frame, name, qlat_n, qlon_n, peak_mask)
        clear_leaks = int(np.sum(np.isfinite(sampled_n) | (cat_n == CAT_VALID)))

    src_vals = dbz[view_valid]
    return {
        "valid_cells": int(view_valid.sum()),
        "max_dbz": float(np.max(src_vals)) if src_vals.size else None,
        "source_std_dbz": float(np.std(src_vals)) if src_vals.size else None,
        "step_ge_3_cells": n_stepped,
        "local_peak_cells": int((view_valid & peak_mask).sum()),
        "visible_peak_cells_contrast_ge_2": int(visible_peaks.sum()),
        "magenta_cells_ge_65": int(magenta.sum()),
        "weak_cells_le_10": int(weak.sum()),
        "center_rmse_dbz": center_rmse,
        "plateau_fraction": plateau_fraction,
        "subcell_rms_dbz": subcell_rms,
        "visible_peaks_z9": _z9_loss(visible_peaks),
        "magenta_z9": _z9_loss(magenta),
        "weak_mean_lift_dbz": weak_mean_lift,
        "weak_lift_gt3_fraction": weak_lift_gt3_fraction,
        "clear_air_leaks": clear_leaks,
    }


def gradient_ac_at_one_cell(frame, name, bbox: BBox, peak_mask, samples_per_cell: int = 8) -> dict:
    """Normalized autocorr of |d(dBZ)/dx| at a lag of one native cell.

    p3l's flat core makes the horizontal gradient a pulse at every cell
    face, so this lag is high. A ramp without that pulse decays instead of
    spiking. Rows that are mostly clear air are skipped.
    """
    dlat = abs(float(frame.lat[1] - frame.lat[0]))
    dlon = abs(float(frame.lon[1] - frame.lon[0]))
    step_lat = dlat / samples_per_cell
    step_lon = dlon / samples_per_cell
    lats = np.arange(bbox.north - step_lat / 2.0, bbox.south, -step_lat, dtype=np.float64)
    lons = np.arange(bbox.west + step_lon / 2.0, bbox.east, step_lon, dtype=np.float64)
    if lats.size < samples_per_cell * 2 or lons.size < samples_per_cell * 3:
        return {"rows": 0, "ac_at_1_cell": None}
    qlon, qlat = np.meshgrid(lons, lats)
    sampled, cat = _sample(frame, name, qlat, qlon, peak_mask)
    acc = []
    lag = samples_per_cell
    for row, crow in zip(sampled, cat):
        if float(np.mean(crow == CAT_VALID)) < 0.6:
            continue
        values = np.where(np.isfinite(row), row, np.nanmean(row))
        grad = np.abs(np.diff(values))
        grad = grad - float(np.mean(grad))
        corr = np.correlate(grad, grad, mode="full")
        mid = corr.size // 2
        if corr[mid] == 0:
            continue
        acc.append(float(corr[mid + lag] / corr[mid]))
    if not acc:
        return {"rows": 0, "ac_at_1_cell": None}
    return {"rows": len(acc), "ac_at_1_cell": float(np.median(acc))}


def harsh_peak_probe() -> dict:
    """68 dBZ and 65 dBZ spikes in 30 dBZ rain, at the worst z9 half-pixel."""
    lat = np.arange(34.10, 33.90, -0.01, dtype=np.float64)
    lon = np.arange(-101.50, -101.30, 0.01, dtype=np.float64)
    dlat = float(lat[1] - lat[0])
    dlon = float(lon[1] - lon[0])
    out = {
        "offset_cells_ns": Z9_HALF_PIXEL_NS,
        "offset_cells_ew": Z9_HALF_PIXEL_EW,
        "neighbors_dbz": 30.0,
        "cases": {},
    }
    for peak in (68.0, 65.0):
        dbz = np.full((lat.size, lon.size), 30.0, dtype=np.float32)
        cat = np.full(dbz.shape, CAT_VALID, dtype=np.uint8)
        dbz[10, 10] = peak
        frame = ReflectivityFrame(
            dbz=dbz,
            lat=lat,
            lon=lon,
            valid_time=datetime(2026, 9, 29, tzinfo=timezone.utc),
            product="synthetic",
            category=cat,
        )
        mask = local_peak_mask(dbz, cat)
        qlat = np.array([[float(lat[10]) + Z9_HALF_PIXEL_NS * dlat]])
        qlon = np.array([[float(lon[10]) + Z9_HALF_PIXEL_EW * dlon]])
        case = {}
        for name in METHOD_ORDER:
            sampled, _got = _sample(frame, name, qlat, qlon, mask)
            case[name] = round(float(sampled[0, 0]), 3)
        out["cases"][str(peak)] = case
    return out


def _time_call(fn, repeats: int = 3) -> float:
    fn()
    samples = []
    for _ in range(repeats):
        t0 = time.perf_counter()
        fn()
        samples.append(time.perf_counter() - t0)
    return float(np.median(samples) * 1000.0)


def benchmark(frame: ReflectivityFrame, peak_mask: np.ndarray) -> dict:
    j, i = _cell_index(frame, 46.945, -102.855)
    gx, gy = latlon_to_global_xy(float(frame.lon[i]), float(frame.lat[j]), TILE_ZOOM)
    z, x, y = TILE_ZOOM, int(gx), int(gy)
    from mpwg_radar.tiles import _query_lonlat

    qlon, qlat = _query_lonlat(z, x, y, 512)
    palette = load_palette("mpwg-rala-2026-09")
    times = {
        "grid_shape": list(frame.dbz.shape),
        "tile": [z, x, y],
        "query_pixels": int(qlat.size),
        "ms": {
            "p3l_splat": _time_call(
                lambda: sample_masked_splat(
                    frame.dbz, frame.lat, frame.lon, qlat, qlon, frame.category
                )
            ),
            CANDIDATE_BILINEAR: _time_call(
                lambda: sample_dbz_candidate(
                    CANDIDATE_BILINEAR,
                    frame.dbz,
                    frame.lat,
                    frame.lon,
                    qlat,
                    qlon,
                    frame.category,
                )
            ),
            CANDIDATE_BICUBIC: _time_call(
                lambda: sample_dbz_candidate(
                    CANDIDATE_BICUBIC,
                    frame.dbz,
                    frame.lat,
                    frame.lon,
                    qlat,
                    qlon,
                    frame.category,
                )
            ),
            CANDIDATE_PEAK_HOLD: _time_call(
                lambda: sample_dbz_candidate(
                    CANDIDATE_PEAK_HOLD,
                    frame.dbz,
                    frame.lat,
                    frame.lon,
                    qlat,
                    qlon,
                    frame.category,
                    peak_mask=peak_mask,
                )
            ),
            "paint_p3l": _time_call(
                lambda: paint_candidate_tile(frame, palette, z, x, y, PRODUCTION_NAME)
            ),
            "paint_peak_hold": _time_call(
                lambda: paint_candidate_tile(
                    frame,
                    palette,
                    z,
                    x,
                    y,
                    CANDIDATE_PEAK_HOLD,
                    peak_mask=peak_mask,
                )
            ),
        },
    }
    return times


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
    return out


def render_overzoom(frame, palette, bbox: BBox, name: str, peak_mask) -> np.ndarray:
    tiles = tiles_for_bbox(bbox, TILE_ZOOM)
    xs = [item[1] for item in tiles]
    ys = [item[2] for item in tiles]
    xmin, xmax = min(xs), max(xs)
    ymin, ymax = min(ys), max(ys)
    tile = 512
    mosaic = np.zeros(((ymax - ymin + 1) * tile, (xmax - xmin + 1) * tile, 4), np.uint8)
    for z, x, y in tiles:
        image = paint_candidate_tile(
            frame,
            palette,
            z,
            x,
            y,
            name,
            peak_mask=peak_mask if name == CANDIDATE_PEAK_HOLD else None,
        )
        arr = np.asarray(image)
        r0 = (y - ymin) * tile
        c0 = (x - xmin) * tile
        mosaic[r0 : r0 + tile, c0 : c0 + tile] = arr
    # Crop in Web Mercator tile space. Latitude is not linear in pixel y.
    x0, y0 = latlon_to_global_xy(bbox.west, bbox.north, TILE_ZOOM)
    x1, y1 = latlon_to_global_xy(bbox.east, bbox.south, TILE_ZOOM)
    c0 = int(np.clip(round((x0 - xmin) * tile), 0, mosaic.shape[1] - 1))
    c1 = int(np.clip(round((x1 - xmin) * tile), c0 + 1, mosaic.shape[1]))
    r0 = int(np.clip(round((y0 - ymin) * tile), 0, mosaic.shape[0] - 1))
    r1 = int(np.clip(round((y1 - ymin) * tile), r0 + 1, mosaic.shape[0]))
    cropped = mosaic[r0:r1, c0:c1]
    im = Image.fromarray(cropped)
    size = (
        max(1, int(round(im.width * OVERZOOM))),
        max(1, int(round(im.height * OVERZOOM))),
    )
    up = im.resize(size, Image.BILINEAR)
    return np.asarray(up)


def render_fine(frame, palette, bbox: BBox, name: str, peak_mask, samples_per_cell: int = 8):
    dlat = abs(float(frame.lat[1] - frame.lat[0]))
    dlon = abs(float(frame.lon[1] - frame.lon[0]))
    step_lat = dlat / samples_per_cell
    step_lon = dlon / samples_per_cell
    lats = np.arange(bbox.north - step_lat / 2.0, bbox.south, -step_lat, dtype=np.float64)
    lons = np.arange(bbox.west + step_lon / 2.0, bbox.east, step_lon, dtype=np.float64)
    qlon, qlat = np.meshgrid(lons, lats)
    if name == "nearest":
        sampled, cat_f = sample_nearest_many(
            frame.lat,
            frame.lon,
            qlat,
            qlon,
            frame.dbz,
            frame.category.astype(np.float32),
        )
        cat = np.full(sampled.shape, 0, dtype=np.uint8)
        finite = np.isfinite(cat_f)
        cat[finite] = np.rint(cat_f[finite]).astype(np.uint8)
        return palette.colorize(sampled, category=cat)
    rgba, _sampled, _cat = sample_and_paint(
        frame,
        palette,
        qlat,
        qlon,
        name,
        peak_mask=peak_mask if name == CANDIDATE_PEAK_HOLD else None,
    )
    return rgba


def _titled(rgba: np.ndarray, title: str, subtitle: str) -> Image.Image:
    base = Image.fromarray(_on_dark(rgba))
    # Enlarge very small cores so the block pitch is readable, without a
    # second resampling filter. Nearest keeps the pixels we actually painted.
    if base.width < 420:
        scale = max(2, int(round(420 / base.width)))
        base = base.resize((base.width * scale, base.height * scale), Image.NEAREST)
    header = 52
    canvas = Image.new("RGBA", (base.width, base.height + header), (8, 12, 20, 255))
    canvas.paste(base, (0, header))
    draw = ImageDraw.Draw(canvas)
    draw.text((8, 4), title, fill=(255, 255, 255, 255), font=_font(18))
    draw.text((8, 28), subtitle, fill=(176, 196, 214, 255), font=_font(13))
    return canvas


def _subtitle(metrics: dict) -> str:
    plateau = metrics.get("plateau_fraction")
    rmse = metrics.get("center_rmse_dbz")
    peaks = metrics.get("visible_peaks_z9") or {}
    loss = peaks.get("worst_loss_dbz")
    plateau_s = "n/a" if plateau is None else f"{plateau * 100:.0f}%"
    rmse_s = "n/a" if rmse is None else f"{rmse:.3f}"
    loss_s = "n/a" if loss is None else f"{loss:.2f} dBZ"
    return f"plateau {plateau_s}   center RMSE {rmse_s}   visible-peak z9 loss {loss_s}"


def _montage(panels: list) -> Image.Image:
    width = max(panel.width for panel in panels)
    height = max(panel.height for panel in panels)
    cols = 2 if len(panels) > 1 else 1
    rows = int(np.ceil(len(panels) / cols))
    gap = 8
    canvas = Image.new(
        "RGBA",
        (cols * width + (cols + 1) * gap, rows * height + (rows + 1) * gap),
        (6, 10, 16, 255),
    )
    for index, panel in enumerate(panels):
        row, col = divmod(index, cols)
        padded = Image.new("RGBA", (width, height), (6, 10, 16, 255))
        padded.paste(panel, (0, 0))
        canvas.paste(padded, (gap + col * (width + gap), gap + row * (height + gap)))
    return canvas


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


def _plant_probe(frame: ReflectivityFrame) -> tuple:
    """Raise the Dickinson core to 68 and its south neighbor to 66.

    The real frame peaks at 58 dBZ, so magenta is not in the scene. This
    probe only measures the lock. It is not an observed field.
    """
    dbz = np.array(frame.dbz, copy=True)
    j, i = _cell_index(frame, 46.945, -102.855)
    south = j + 1 if frame.lat[1] < frame.lat[0] else j - 1
    if frame.category[j, i] != CAT_VALID or frame.category[south, i] != CAT_VALID:
        raise RuntimeError("magenta probe cells are not valid echo")
    planted = {
        "peak_lat": float(frame.lat[j]),
        "peak_lon": float(frame.lon[i]),
        "peak_original_dbz": float(dbz[j, i]),
        "peak_planted_dbz": 68.0,
        "shoulder_lat": float(frame.lat[south]),
        "shoulder_lon": float(frame.lon[i]),
        "shoulder_original_dbz": float(dbz[south, i]),
        "shoulder_planted_dbz": 66.0,
    }
    dbz[j, i] = np.float32(68.0)
    dbz[south, i] = np.float32(66.0)
    probe = ReflectivityFrame(
        dbz=dbz,
        lat=frame.lat,
        lon=frame.lon,
        valid_time=frame.valid_time,
        product=frame.product,
        source=frame.source + " + synthetic magenta probe",
        category=np.array(frame.category, copy=True),
    )
    return probe, planted, (j, i), (south, i)


def _probe_pixel_values(frame, peak_mask, peak_ji, shoulder_ji) -> dict:
    out = {}
    for label, (j, i) in ("peak68", peak_ji), ("shoulder66", shoulder_ji):
        qlat, qlon = _z9_pixel_query(
            np.array([frame.lat[j]]), np.array([frame.lon[i]])
        )
        qlat = np.asarray(qlat, dtype=np.float64)[:, None]
        qlon = np.asarray(qlon, dtype=np.float64)[:, None]
        out[label] = {"source_dbz": float(frame.dbz[j, i])}
        for name in METHOD_ORDER:
            sampled, _cat = _sample(frame, name, qlat, qlon, peak_mask)
            out[label][name] = round(float(sampled[0, 0]), 3)
        mask65 = local_peak_mask(frame.dbz, frame.category, hold_min_dbz=65.0)
        sampled65, _cat = sample_dbz_candidate(
            CANDIDATE_PEAK_HOLD,
            frame.dbz,
            frame.lat,
            frame.lon,
            qlat,
            qlon,
            frame.category,
            peak_mask=mask65,
        )
        out[label][CANDIDATE_PEAK_HOLD + "+65"] = round(float(sampled65[0, 0]), 3)
    return out


def _panel_rgb(rgba: np.ndarray, min_width: int) -> Image.Image:
    """Composite clear air on the dark map background and enlarge small fields."""
    image = Image.fromarray(_on_dark(rgba)).convert("RGB")
    if image.width < min_width:
        scale = max(2, int(round(min_width / image.width)))
        image = image.resize(
            (image.width * scale, image.height * scale), Image.Resampling.NEAREST
        )
    return image


def _judge_pair(
    left_rgba: np.ndarray,
    right_rgba: np.ndarray,
    *,
    site: str,
    frame_id: str,
    note: str,
    min_width: int = 640,
) -> Image.Image:
    """Side-by-side CURRENT p3l vs REVIEW bilinear+peak-hold, labeled for email."""
    left = _panel_rgb(left_rgba, min_width)
    right = _panel_rgb(right_rgba, min_width)
    panel_h = max(left.height, right.height)
    panel_w = max(left.width, right.width)
    gap = 16
    margin = 20
    banner_h = 78
    label_h = 64
    footer_h = 78
    width = margin * 2 + panel_w * 2 + gap
    height = banner_h + label_h + panel_h + footer_h
    canvas = Image.new("RGB", (width, height), (8, 12, 20))
    draw = ImageDraw.Draw(canvas)
    amber = (255, 196, 64)
    white = (255, 255, 255)
    muted = (176, 196, 214)
    draw.text((margin, 14), site, fill=white, font=_font(26, bold=True))
    draw.text(
        (margin, 46),
        f"{frame_id}    NOT PRODUCTION    flag default OFF",
        fill=amber,
        font=_font(16, bold=True),
    )
    columns = (
        (left, "CURRENT  ·  p3l", "flag off  ·  production splat"),
        (right, "REVIEW  ·  bilinear + peak hold", "MPWG_RALA_DBZ_INTERP=bilinear_peak_hold"),
    )
    for index, (panel, title, subtitle) in enumerate(columns):
        x = margin + index * (panel_w + gap)
        draw.text((x, banner_h + 4), title, fill=white, font=_font(20, bold=True))
        draw.text((x, banner_h + 32), subtitle, fill=muted, font=_font(14))
        padded = Image.new("RGB", (panel_w, panel_h), (8, 12, 20))
        padded.paste(panel, (0, 0))
        canvas.paste(padded, (x, banner_h + label_h))
    draw.text((margin, height - 62), note, fill=muted, font=_font(14))
    draw.text(
        (margin, height - 36),
        "Spatial stamp stays p3l. Palette stays p3k. Do not merge. Do not deploy.",
        fill=amber,
        font=_font(15, bold=True),
    )
    return canvas


def write_judge_crops(frame, palette, peak_mask, probe, probe_mask, out_dir: Path) -> None:
    """Four labeled pairs: Dickinson, Belle Fourche, core field, magenta probe."""
    out_dir.mkdir(parents=True, exist_ok=True)
    frame_id = frame.frame_id or "unknown-frame"
    jobs = (
        (
            "dickinson_overzoom_p3l_vs_peakhold.png",
            "Dickinson ND  ·  Mapbox-like overzoom",
            "Real z9 tiles, then the same ~2.73× overzoom as the p3n measurement.",
            lambda name, mask: render_overzoom(frame, palette, DICKINSON, name, mask),
            peak_mask,
            720,
        ),
        (
            "belle_fourche_overzoom_p3l_vs_peakhold.png",
            "Belle Fourche  ·  Mapbox-like overzoom",
            "Weak-fringe window. Real z9 tiles, same overzoom.",
            lambda name, mask: render_overzoom(frame, palette, BELLE, name, mask),
            peak_mask,
            720,
        ),
        (
            "dickinson_core_p3l_vs_peakhold.png",
            "Dickinson core  ·  8 samples per native cell",
            "Field view of the core. No RGB blur. Clear air stays empty.",
            lambda name, mask: render_fine(
                frame, palette, DICKINSON_CORE, name, mask, samples_per_cell=8
            ),
            peak_mask,
            900,
        ),
        (
            "magenta_probe_p3l_vs_peakhold.png",
            "Magenta probe  ·  Dickinson core overzoom",
            "SYNTHETIC. 68 planted on the 58 cell, 66 on the south neighbor. Not observed.",
            lambda name, mask: render_overzoom(probe, palette, DICKINSON_CORE, name, mask),
            probe_mask,
            640,
        ),
    )
    for filename, site, note, render, mask, min_width in jobs:
        print("judge", filename)
        pair = _judge_pair(
            render(PRODUCTION_NAME, mask),
            render(CANDIDATE_PEAK_HOLD, mask),
            site=site,
            frame_id=frame_id,
            note=note,
            min_width=min_width,
        )
        pair.save(out_dir / filename)
        print("wrote", out_dir / filename, pair.size)


def _run_judge_only(grib: Path, out_dir: Path) -> None:
    palette = load_palette("mpwg-rala-2026-09")
    if palette.version != "2026-09-rala-p3k":
        raise SystemExit(f"unexpected palette {palette.version}")
    if SPATIAL_REVISION != "p3l":
        raise SystemExit(f"unexpected spatial stamp {SPATIAL_REVISION}")
    region = BBox(-104.10, 44.40, -102.30, 47.35, "nd-sd")
    print("decoding", grib)
    decoded = decode_grib2(grib, bbox=region, product="ReflectivityAtLowestAltitude")
    frame = _rala_clean(decoded)
    print(f"frame {frame.frame_id} grid {frame.dbz.shape}")
    peak_mask = local_peak_mask(frame.dbz, frame.category)
    probe, _planted, _peak_ji, _shoulder_ji = _plant_probe(frame)
    probe_mask = local_peak_mask(probe.dbz, probe.category)
    write_judge_crops(frame, palette, peak_mask, probe, probe_mask, out_dir)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--grib", type=Path, required=True)
    parser.add_argument("--out", type=Path, default=Path("docs/p3n-dbz-interp"))
    parser.add_argument(
        "--skip-full-bench",
        action="store_true",
        help="Time the regional crop instead of the full CONUS grid",
    )
    parser.add_argument(
        "--judge-only",
        action="store_true",
        help="Write the four labeled p3l vs peak-hold crops and skip metrics",
    )
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    if args.judge_only:
        _run_judge_only(args.grib, args.out)
        return

    palette = load_palette("mpwg-rala-2026-09")
    if palette.version != "2026-09-rala-p3k":
        raise SystemExit(f"unexpected palette {palette.version}")
    if SPATIAL_REVISION != "p3l":
        raise SystemExit(f"unexpected spatial stamp {SPATIAL_REVISION}")

    region = BBox(-104.10, 44.40, -102.30, 47.35, "nd-sd")
    print("decoding", args.grib)
    decoded = decode_grib2(
        args.grib, bbox=region, product="ReflectivityAtLowestAltitude"
    )
    raw = np.array(decoded.dbz, copy=True)
    frame = _rala_clean(decoded)
    delta = np.nan_to_num(frame.dbz) - np.nan_to_num(raw)
    mode_max_abs = float(np.max(np.abs(delta)))
    print(
        f"frame {frame.frame_id} grid {frame.dbz.shape} "
        f"axes {frame.lat.dtype}/{frame.lon.dtype} "
        f"rala-clean max |ΔdBZ| {mode_max_abs:.4f}"
    )
    peak_mask = local_peak_mask(frame.dbz, frame.category)

    sites = {}
    for bbox in (DICKINSON, DICKINSON_CORE, BELLE):
        print("metrics", bbox.name)
        methods = {
            name: evaluate_site(frame, name, bbox, peak_mask) for name in METHOD_ORDER
        }
        if bbox.name in ("dickinson-core", "belle-fourche"):
            for name in METHOD_ORDER:
                methods[name]["grid_pitch"] = gradient_ac_at_one_cell(
                    frame, name, bbox, peak_mask
                )
        sites[bbox.name] = {"bbox": bbox.as_dict(), "methods": methods}

    print("harsh probe")
    harsh = harsh_peak_probe()
    probe, planted, peak_ji, shoulder_ji = _plant_probe(frame)
    probe_mask = local_peak_mask(probe.dbz, probe.category)
    probe_pixels = _probe_pixel_values(probe, probe_mask, peak_ji, shoulder_ji)

    print("benchmark")
    if args.skip_full_bench:
        bench_frame = frame
        bench_mask = peak_mask
    else:
        full = decode_grib2(args.grib, product="ReflectivityAtLowestAltitude")
        bench_frame = full
        bench_mask = local_peak_mask(full.dbz, full.category)
    bench = benchmark(bench_frame, bench_mask)
    print("bench ms", json.dumps(bench["ms"]))

    print("rendering")
    panels = []
    for name in METHOD_ORDER:
        rgba = render_overzoom(frame, palette, DICKINSON, name, peak_mask)
        panels.append(
            _titled(rgba, METHOD_TITLE[name], _subtitle(sites["dickinson"]["methods"][name]))
        )
    montage = _montage(panels)
    montage.save(args.out / "dickinson_mapbox_overzoom.png")

    core_panels = []
    for name in ("nearest",) + METHOD_ORDER:
        rgba = render_fine(frame, palette, DICKINSON_CORE, name, peak_mask, samples_per_cell=8)
        if name == "nearest":
            subtitle = "source cells, no seam"
        else:
            subtitle = _subtitle(sites["dickinson-core"]["methods"][name])
        core_panels.append(_titled(rgba, METHOD_TITLE[name], subtitle))
    _montage(core_panels).save(args.out / "dickinson_core_field.png")

    belle_panels = []
    for name in METHOD_ORDER:
        rgba = render_overzoom(frame, palette, BELLE, name, peak_mask)
        belle_panels.append(
            _titled(rgba, METHOD_TITLE[name], _subtitle(sites["belle-fourche"]["methods"][name]))
        )
    _montage(belle_panels).save(args.out / "belle_fourche_mapbox_overzoom.png")

    probe_panels = []
    for name in METHOD_ORDER:
        rgba = render_overzoom(probe, palette, DICKINSON_CORE, name, probe_mask)
        peak_v = probe_pixels["peak68"][name]
        shoulder_v = probe_pixels["shoulder66"][name]
        probe_panels.append(
            _titled(
                rgba,
                METHOD_TITLE[name],
                f"planted 68 → z9 px {peak_v:.1f}    shoulder 66 → {shoulder_v:.1f}",
            )
        )
    _montage(probe_panels).save(args.out / "magenta_probe_mapbox_overzoom.png")

    report = {
        "frame_id": frame.frame_id,
        "product": "ReflectivityAtLowestAltitude",
        "spatial_production": SPATIAL_REVISION,
        "palette": palette.version,
        "rala_clean_max_abs_dbz_change": mode_max_abs,
        "lat_dtype": str(frame.lat.dtype),
        "lon_dtype": str(frame.lon.dtype),
        "compare_zoom": COMPARE_ZOOM,
        "overzoom_factor": OVERZOOM,
        "note": (
            "Offline only. Production stamps stay p3l / 2026-09-rala-p3k. "
            "The magenta probe plants 68 and 66 dBZ into the Dickinson core; "
            "the real frame has no cell ≥65."
        ),
        "sites": sites,
        "harsh_z9_half_pixel": harsh,
        "magenta_probe": {"planted": planted, "z9_pixel_dbz": probe_pixels},
        "benchmark_ms": bench,
    }
    (args.out / "metrics.json").write_text(json.dumps(_jsonable(report), indent=2) + "\n")
    print("wrote", args.out)


if __name__ == "__main__":
    main()
