"""Render 512×512 XYZ PNG tiles from a physical dBZ grid."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

import numpy as np
from PIL import Image

from mpwg_radar.geo import BBox, iter_tiles, tile_bounds, tile_pixel_centers
from mpwg_radar.grib import ReflectivityFrame
from mpwg_radar.palette import Palette
from mpwg_radar.products import (
    CAT_MISSING,
    CAT_NO_ECHO,
    CAT_VALID,
    SAMPLE_MASKED_BILINEAR,
    SAMPLE_NEAREST,
)

log = logging.getLogger(__name__)


def sample_nearest(
    dbz: np.ndarray,
    lat: np.ndarray,
    lon: np.ndarray,
    qlat: np.ndarray,
    qlon: np.ndarray,
) -> np.ndarray:
    """Nearest-neighbor sample of a regular lat/lon dBZ grid."""
    return sample_nearest_many(lat, lon, qlat, qlon, dbz)[0]


def _grid_fractional(
    lat: np.ndarray,
    lon: np.ndarray,
    qlat: np.ndarray,
    qlon: np.ndarray,
):
    """Continuous row/col index of each query on a regular lat/lon axis.

    Returns None when the axis is too short or has a zero step. Index 0 is
    ``lat[0]`` / ``lon[0]``; increasing index follows the stored axis even
    when latitude runs north-to-south.
    """
    if lat.size < 2 or lon.size < 2:
        return None
    dlat = float(lat[1] - lat[0])
    dlon = float(lon[1] - lon[0])
    if dlat == 0.0 or dlon == 0.0:
        return None
    j_f = (np.asarray(qlat, dtype=np.float64) - float(lat[0])) / dlat
    i_f = (np.asarray(qlon, dtype=np.float64) - float(lon[0])) / dlon
    return j_f, i_f


def _nearest_indexers(
    lat: np.ndarray,
    lon: np.ndarray,
    qlat: np.ndarray,
    qlon: np.ndarray,
):
    frac = _grid_fractional(lat, lon, qlat, qlon)
    if frac is None:
        dlat = float(lat[1] - lat[0]) if lat.size > 1 else -0.01
        dlon = float(lon[1] - lon[0]) if lon.size > 1 else 0.01
        if dlat == 0 or dlon == 0:
            ok = np.zeros(np.shape(qlat), dtype=bool)
            return np.zeros(np.shape(qlat), dtype=np.int32), np.zeros(
                np.shape(qlat), dtype=np.int32
            ), ok
        j_f = (qlat - float(lat[0])) / dlat
        i_f = (qlon - float(lon[0])) / dlon
    else:
        j_f, i_f = frac
    j = np.rint(j_f).astype(np.int32)
    i = np.rint(i_f).astype(np.int32)
    ok = (j >= 0) & (j < lat.size) & (i >= 0) & (i < lon.size)
    return j, i, ok


def sample_nearest_many(
    lat: np.ndarray,
    lon: np.ndarray,
    qlat: np.ndarray,
    qlon: np.ndarray,
    *arrays: np.ndarray,
) -> List[np.ndarray]:
    j, i, ok = _nearest_indexers(lat, lon, qlat, qlon)
    outs: List[np.ndarray] = []
    for arr in arrays:
        out = np.full(qlat.shape, np.nan, dtype=np.float32)
        if np.any(ok):
            out[ok] = arr[j[ok], i[ok]]
        outs.append(out)
    return outs


def sample_masked_bilinear(
    dbz: np.ndarray,
    lat: np.ndarray,
    lon: np.ndarray,
    qlat: np.ndarray,
    qlon: np.ndarray,
    category: Optional[np.ndarray] = None,
) -> Tuple[np.ndarray, np.ndarray]:
    """Sample dBZ without letting echo bleed into clear air.

    The nearest source cell is the footprint. If that cell is no-echo or
    missing, the query stays that category and NaN — a 60 dBZ neighbor
    cannot paint it. If the nearest cell is valid, dBZ is a bilinear blend
    of the surrounding *valid* samples only. Invalid corners are left out
    of the average, so the edge of a storm keeps its own dBZ instead of
    fading toward zero, and no new precip appears outside the echo mask.
    """
    src = np.asarray(dbz)
    qlat_a = np.asarray(qlat)
    qlon_a = np.asarray(qlon)
    if category is None:
        cat = np.where(np.isfinite(src), CAT_VALID, CAT_NO_ECHO).astype(np.uint8)
    else:
        cat = np.asarray(category)
        if cat.shape != src.shape:
            raise ValueError("category shape must match dbz")
    out_dbz = np.full(qlat_a.shape, np.nan, dtype=np.float32)
    out_cat = np.full(qlat_a.shape, CAT_MISSING, dtype=np.uint8)
    frac = _grid_fractional(lat, lon, qlat_a, qlon_a)
    if frac is None or src.size == 0:
        return out_dbz, out_cat
    j_f, i_f = frac
    j_n, i_n, in_grid = _nearest_indexers(lat, lon, qlat_a, qlon_a)
    if not np.any(in_grid):
        return out_dbz, out_cat
    out_cat[in_grid] = cat[j_n[in_grid], i_n[in_grid]]
    echo = in_grid & (out_cat == CAT_VALID)
    if not np.any(echo):
        return out_dbz, out_cat

    ny, nx = src.shape
    j0 = np.floor(j_f).astype(np.int32)
    i0 = np.floor(i_f).astype(np.int32)
    tj = (j_f - j0).astype(np.float32)
    ti = (i_f - i0).astype(np.float32)
    weights = (
        (j0, i0, (1.0 - tj) * (1.0 - ti)),
        (j0, i0 + 1, (1.0 - tj) * ti),
        (j0 + 1, i0, tj * (1.0 - ti)),
        (j0 + 1, i0 + 1, tj * ti),
    )
    num = np.zeros(qlat_a.shape, dtype=np.float32)
    den = np.zeros(qlat_a.shape, dtype=np.float32)
    for jj, ii, w in weights:
        inside = (jj >= 0) & (jj < ny) & (ii >= 0) & (ii < nx)
        jc = np.clip(jj, 0, ny - 1)
        ic = np.clip(ii, 0, nx - 1)
        vals = src[jc, ic]
        good = inside & (cat[jc, ic] == CAT_VALID) & np.isfinite(vals)
        num += np.where(good, vals.astype(np.float32) * w, np.float32(0.0))
        den += np.where(good, w, np.float32(0.0))
    use = echo & (den > 1e-6)
    out_dbz[use] = num[use] / den[use]
    fallback = echo & ~use
    if np.any(fallback):
        out_dbz[fallback] = src[j_n[fallback], i_n[fallback]]
    return out_dbz, out_cat


def _query_lonlat(z: int, x: int, y: int, tile_size: int):
    xs, ys = tile_pixel_centers(z, x, y, tile_size)
    gx = np.array(xs, dtype=np.float64)
    gy = np.array(ys, dtype=np.float64)
    n = 1 << z
    lon = gx / n * 360.0 - 180.0
    lat_rad = np.arctan(np.sinh(np.pi * (1.0 - 2.0 * gy / n)))
    lat = np.degrees(lat_rad)
    return np.meshgrid(lon, lat)


def render_tile(
    frame: ReflectivityFrame,
    palette: Palette,
    z: int,
    x: int,
    y: int,
    tile_size: int = 512,
    sample_mode: str = SAMPLE_NEAREST,
) -> Tuple[Image.Image, bool]:
    """Return (PNG image, has_echo).

    ``nearest`` (composite) copies one MRMS cell into every pixel of that
    cell. ``masked-bilinear`` (RALA) keeps the same nearest-cell footprint
    and only interpolates dBZ inside echo, so the grid steps inside a storm
    soften without a halo in clear air.
    """
    qlon, qlat = _query_lonlat(z, x, y, tile_size)
    if sample_mode == SAMPLE_MASKED_BILINEAR:
        sampled, sampled_cat = sample_masked_bilinear(
            frame.dbz, frame.lat, frame.lon, qlat, qlon, frame.category
        )
        rgba = palette.colorize(sampled, category=sampled_cat)
    elif sample_mode == SAMPLE_NEAREST:
        if frame.category is not None:
            sampled, sampled_cat_f = sample_nearest_many(
                frame.lat,
                frame.lon,
                qlat,
                qlon,
                frame.dbz,
                frame.category.astype(np.float32),
            )
            sampled_cat = np.full(sampled.shape, CAT_MISSING, dtype=np.uint8)
            finite_cat = np.isfinite(sampled_cat_f)
            sampled_cat[finite_cat] = np.rint(sampled_cat_f[finite_cat]).astype(np.uint8)
            rgba = palette.colorize(sampled, category=sampled_cat)
        else:
            sampled = sample_nearest(frame.dbz, frame.lat, frame.lon, qlat, qlon)
            rgba = palette.colorize(sampled)
    else:
        raise ValueError(
            f"Unknown sample_mode {sample_mode!r}. "
            f"Use {SAMPLE_NEAREST!r} or {SAMPLE_MASKED_BILINEAR!r}."
        )
    has_echo = bool(np.any(rgba[..., 3] > 0))
    return Image.fromarray(rgba), has_echo


def _axis_window(axis: np.ndarray, lo: float, hi: float, pad: int = 1) -> slice:
    """Inclusive slice of a monotonic lat or lon axis overlapping [lo, hi]."""
    if axis.size == 0:
        return slice(0, 0)
    increasing = bool(axis[-1] >= axis[0])
    ordered = axis if increasing else axis[::-1]
    i0 = int(np.searchsorted(ordered, lo, side="left"))
    i1 = int(np.searchsorted(ordered, hi, side="right"))
    if not increasing:
        n = int(axis.size)
        i0, i1 = n - i1, n - i0
    i0 = max(0, i0 - pad)
    i1 = min(int(axis.size), i1 + pad)
    if i1 <= i0:
        return slice(0, 0)
    return slice(i0, i1)


def tile_has_echo(frame: ReflectivityFrame, z: int, x: int, y: int) -> bool:
    """True if the physical grid has any valid reflectivity inside the XYZ tile."""
    bounds = tile_bounds(z, x, y)
    rows = _axis_window(frame.lat, bounds.south, bounds.north)
    cols = _axis_window(frame.lon, bounds.west, bounds.east)
    if rows.start == rows.stop or cols.start == cols.stop:
        return False
    if frame.category is not None:
        return bool(np.any(frame.category[rows, cols] == CAT_VALID))
    return bool(np.any(np.isfinite(frame.dbz[rows, cols])))


def write_tiles(
    frame: ReflectivityFrame,
    palette: Palette,
    out_dir: Path,
    bbox: BBox,
    min_zoom: int,
    max_zoom: int,
    tile_size: int = 512,
    skip_empty: bool = True,
    sample_mode: str = SAMPLE_NEAREST,
) -> Dict:
    """Write `{z}/{x}/{y}.png` under out_dir. Returns tile stats."""
    out_dir.mkdir(parents=True, exist_ok=True)
    written = 0
    skipped = 0
    paths: List[str] = []
    for z, x, y in iter_tiles(bbox, min_zoom, max_zoom):
        if skip_empty and not tile_has_echo(frame, z, x, y):
            skipped += 1
            continue
        image, has_echo = render_tile(
            frame, palette, z, x, y, tile_size, sample_mode=sample_mode
        )
        if skip_empty and not has_echo:
            skipped += 1
            continue
        dest = out_dir / str(z) / str(x) / f"{y}.png"
        dest.parent.mkdir(parents=True, exist_ok=True)
        image.save(dest, format="PNG", optimize=True)
        written += 1
        paths.append(f"{z}/{x}/{y}.png")
    stats = {
        "written": written,
        "skipped_empty": skipped,
        "tile_size": tile_size,
        "min_zoom": min_zoom,
        "max_zoom": max_zoom,
    }
    log.info(
        "Wrote %d tiles (%d empty skipped) → %s",
        written,
        skipped,
        out_dir,
    )
    return stats


def write_colorbar(palette: Palette, path: Path) -> None:
    """Write the Clean ramp from the display cutoff through 75 dBZ."""
    path.parent.mkdir(parents=True, exist_ok=True)
    palette.colorbar(dbz_min=palette.min_dbz, dbz_max=75).save(path, format="PNG")
