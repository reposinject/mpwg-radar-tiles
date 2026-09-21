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
from mpwg_radar.products import CAT_MISSING, CAT_VALID

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


def _nearest_indexers(
    lat: np.ndarray,
    lon: np.ndarray,
    qlat: np.ndarray,
    qlon: np.ndarray,
):
    dlat = float(lat[1] - lat[0]) if lat.size > 1 else -0.01
    dlon = float(lon[1] - lon[0]) if lon.size > 1 else 0.01
    if dlat == 0 or dlon == 0:
        ok = np.zeros(qlat.shape, dtype=bool)
        return np.zeros(qlat.shape, dtype=np.int32), np.zeros(
            qlat.shape, dtype=np.int32
        ), ok
    j = np.rint((qlat - float(lat[0])) / dlat).astype(np.int32)
    i = np.rint((qlon - float(lon[0])) / dlon).astype(np.int32)
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


def render_tile(
    frame: ReflectivityFrame,
    palette: Palette,
    z: int,
    x: int,
    y: int,
    tile_size: int = 512,
) -> Tuple[Image.Image, bool]:
    """Return (PNG image, has_echo)."""
    xs, ys = tile_pixel_centers(z, x, y, tile_size)
    # mesh of pixel centers in global tile coords → lon/lat
    gx = np.array(xs, dtype=np.float64)
    gy = np.array(ys, dtype=np.float64)
    n = 1 << z
    lon = gx / n * 360.0 - 180.0
    lat_rad = np.arctan(np.sinh(np.pi * (1.0 - 2.0 * gy / n)))
    lat = np.degrees(lat_rad)
    qlon, qlat = np.meshgrid(lon, lat)
    if frame.category is not None:
        sampled, sampled_cat_f = sample_nearest_many(
            frame.lat, frame.lon, qlat, qlon, frame.dbz, frame.category.astype(np.float32)
        )
        sampled_cat = np.full(sampled.shape, CAT_MISSING, dtype=np.uint8)
        finite_cat = np.isfinite(sampled_cat_f)
        sampled_cat[finite_cat] = np.rint(sampled_cat_f[finite_cat]).astype(np.uint8)
        rgba = palette.colorize(sampled, category=sampled_cat)
    else:
        sampled = sample_nearest(frame.dbz, frame.lat, frame.lon, qlat, qlon)
        rgba = palette.colorize(sampled)
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
        image, has_echo = render_tile(frame, palette, z, x, y, tile_size)
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
