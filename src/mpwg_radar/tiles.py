"""Render 512×512 XYZ PNG tiles from a physical dBZ grid."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

import numpy as np
from PIL import Image

from mpwg_radar.geo import BBox, iter_tiles, tile_pixel_centers
from mpwg_radar.grib import ReflectivityFrame
from mpwg_radar.palette import Palette

log = logging.getLogger(__name__)


def sample_nearest(
    dbz: np.ndarray,
    lat: np.ndarray,
    lon: np.ndarray,
    qlat: np.ndarray,
    qlon: np.ndarray,
) -> np.ndarray:
    """Nearest-neighbor sample of a regular lat/lon dBZ grid."""
    dlat = float(lat[1] - lat[0]) if lat.size > 1 else -0.01
    dlon = float(lon[1] - lon[0]) if lon.size > 1 else 0.01
    if dlat == 0 or dlon == 0:
        return np.full(qlat.shape, np.nan, dtype=np.float32)
    j = np.rint((qlat - float(lat[0])) / dlat).astype(np.int32)
    i = np.rint((qlon - float(lon[0])) / dlon).astype(np.int32)
    out = np.full(qlat.shape, np.nan, dtype=np.float32)
    ok = (j >= 0) & (j < lat.size) & (i >= 0) & (i < lon.size)
    if not np.any(ok):
        return out
    out[ok] = dbz[j[ok], i[ok]]
    return out


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
    sampled = sample_nearest(frame.dbz, frame.lat, frame.lon, qlat, qlon)
    rgba = palette.colorize(sampled)
    has_echo = bool(np.any(rgba[..., 3] > 0))
    return Image.fromarray(rgba), has_echo


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
    path.parent.mkdir(parents=True, exist_ok=True)
    palette.colorbar().save(path, format="PNG")
