"""Render 512×512 XYZ PNG tiles from a physical dBZ grid."""

from __future__ import annotations

import json
import logging
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

import numpy as np
from PIL import Image

from mpwg_radar.geo import BBox, iter_tiles, tile_bounds
from mpwg_radar.grib import ReflectivityFrame
from mpwg_radar.palette import Palette
from mpwg_radar.products import (
    CAT_MISSING,
    CAT_NO_ECHO,
    CAT_VALID,
    SAMPLE_MASKED_BILINEAR,
    SAMPLE_MASKED_SPLAT,
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


# Valid-corner weight at which a pixel sitting on the echo/clear boundary
# fades to transparent, and the weight at which the interior stays solid.
# 1.0 is deep inside echo. ~0.5 is the shared edge with a no-echo cell.
# Fading only inside the echo cell rounds square corners without painting
# the clear-air neighbor.
_EDGE_CLEAR = np.float32(0.50)
_EDGE_SOLID = np.float32(0.84)


def sample_masked_bilinear(
    dbz: np.ndarray,
    lat: np.ndarray,
    lon: np.ndarray,
    qlat: np.ndarray,
    qlon: np.ndarray,
    category: Optional[np.ndarray] = None,
) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Sample dBZ without letting echo bleed into clear air.

    Returns ``(dbz, category, edge_scale)``. The nearest source cell is the
    footprint. If that cell is no-echo or missing, the query stays that
    category, NaN, and ``edge_scale`` 0 — a 60 dBZ neighbor cannot paint it.
    If the nearest cell is valid, dBZ is a bilinear blend of the surrounding
    *valid* samples only. Invalid corners are left out of the average, so
    the edge of a storm keeps its own dBZ instead of fading toward zero.
    ``edge_scale`` is 1 in the interior and falls to 0 at the boundary with
    clear air, which knocks the square corners off the mask without ever
    writing into a clear-air cell.
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
    edge_scale = np.zeros(qlat_a.shape, dtype=np.float32)
    frac = _grid_fractional(lat, lon, qlat_a, qlon_a)
    if frac is None or src.size == 0:
        return out_dbz, out_cat, edge_scale
    j_f, i_f = frac
    j_n, i_n, in_grid = _nearest_indexers(lat, lon, qlat_a, qlon_a)
    if not np.any(in_grid):
        return out_dbz, out_cat, edge_scale
    out_cat[in_grid] = cat[j_n[in_grid], i_n[in_grid]]
    echo = in_grid & (out_cat == CAT_VALID)
    if not np.any(echo):
        return out_dbz, out_cat, edge_scale

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
    mult = np.clip((den - _EDGE_CLEAR) / (_EDGE_SOLID - _EDGE_CLEAR), 0.0, 1.0)
    edge_scale[echo] = mult[echo].astype(np.float32)
    return out_dbz, out_cat, edge_scale


# Occupancy blur in MRMS-cell units. The visible outline is an iso-line of
# this blur, drawn inside the echo mask. Sigma is about half a cell, so the
# fade lives in the outer part of the boundary cell: the cell center stays
# opaque, the square rim does not, and a storm does not grow a multi-cell
# halo. A half-plane edge still sits near occupancy 0.5. Clear air is never
# painted.
_OCC_SIGMA = 0.52
_OCC_LO = np.float32(0.56)
_OCC_HI = np.float32(0.78)
# Narrow disc for a one-cell return. Weight is gone well inside the cell, so
# it cannot repaint the inset rim the occupancy contour just removed. It stays
# on only while occupancy is still below the multi-cell iso-line, which is
# an isolated cell, not the edge of a storm.
_DISC_SIGMA = 0.46
_DISC_LO = np.float32(0.82)
_DISC_HI = np.float32(0.97)
_DISC_KEEP_LO = np.float32(0.60)
_DISC_KEEP_HI = np.float32(0.70)
# dBZ is a local resample of valid cells only. Neighbors grade across the
# shared face; a cell center stays near its own value. NO-ECHO is not a
# sample, so 25 dBZ beside clear air does not become 18→12→6. A light peak
# pull keeps a hot cell in its own color family when that local blend is
# cooler. This is not a wide blur of the finished RGBA raster.
_COLOR_SIGMA = 0.35
_PEAK_SIGMA = 0.32
_PEAK_MIX = np.float32(0.45)
_CORE_RISE = np.float32(10.0)
_SPLAT_RADIUS = 3


def _splat_echo(
    src: np.ndarray,
    cat: np.ndarray,
    j_f: np.ndarray,
    i_f: np.ndarray,
    j_n: np.ndarray,
    i_n: np.ndarray,
    echo: np.ndarray,
    radius: int,
) -> Tuple[np.ndarray, np.ndarray]:
    """Masked-splat dBZ and edge scale for echo pixels only.

    The weights are the same grid-index Gaussians as a full-tile pass:
    nearest cell is the footprint, taps outside the mosaic count as clear
    air, and a clear-air pixel is never written. Clear pixels are skipped
    instead of allocating a 512×512 temporary on every kernel tap — that
    full-tile loop was most of a ~10 minute CONUS cook.
    """
    out_dbz = np.full(echo.shape, np.nan, dtype=np.float32)
    edge_scale = np.zeros(echo.shape, dtype=np.float32)
    ys, xs = np.nonzero(echo)
    if ys.size == 0:
        return out_dbz, edge_scale

    inv_occ = np.float32(1.0 / (2.0 * _OCC_SIGMA * _OCC_SIGMA))
    inv_disc = np.float32(1.0 / (2.0 * _DISC_SIGMA * _DISC_SIGMA))
    inv_color = np.float32(1.0 / (2.0 * _COLOR_SIGMA * _COLOR_SIGMA))
    inv_peak = np.float32(1.0 / (2.0 * _PEAK_SIGMA * _PEAK_SIGMA))

    jn = j_n[ys, xs].astype(np.int32, copy=False)
    inn = i_n[ys, xs].astype(np.int32, copy=False)
    # Neighbor gathers stay inside the tile's window. The full CONUS grid
    # is a view here, not a copy.
    j0 = max(0, int(jn.min()) - radius)
    j1 = min(int(src.shape[0]), int(jn.max()) + radius + 1)
    i0 = max(0, int(inn.min()) - radius)
    i1 = min(int(src.shape[1]), int(inn.max()) + radius + 1)
    src_c = np.asarray(src, dtype=np.float32)[j0:j1, i0:i1]
    cat_c = cat[j0:j1, i0:i1]
    ny, nx = src_c.shape
    jn = jn - np.int32(j0)
    inn = inn - np.int32(i0)
    j_f_e = j_f[ys, xs] - j0
    i_f_e = i_f[ys, xs] - i0
    n = int(ys.size)

    mass_echo = np.zeros(n, dtype=np.float32)
    mass_all = np.zeros(n, dtype=np.float32)
    disc_mass = np.zeros(n, dtype=np.float32)
    num = np.zeros(n, dtype=np.float32)
    den = np.zeros(n, dtype=np.float32)
    offs = np.arange(-radius, radius + 1, dtype=np.float64)
    # dj = j_f - (j_n + dy), matching the full-tile cast to float32.
    dj = (j_f_e[None, :] - (jn.astype(np.float64)[None, :] + offs[:, None])).astype(
        np.float32
    )
    di = (i_f_e[None, :] - (inn.astype(np.float64)[None, :] + offs[:, None])).astype(
        np.float32
    )
    d2_j = dj * dj
    d2_i = di * di
    wy_o = np.exp(-d2_j * inv_occ).astype(np.float32)
    wy_d = np.exp(-d2_j * inv_disc).astype(np.float32)
    wy_c = np.exp(-d2_j * inv_color).astype(np.float32)
    wx_o = np.exp(-d2_i * inv_occ).astype(np.float32)
    wx_d = np.exp(-d2_i * inv_disc).astype(np.float32)
    wx_c = np.exp(-d2_i * inv_color).astype(np.float32)

    dxs = np.arange(-radius, radius + 1, dtype=np.int32)
    zero = np.float32(0.0)
    for ky, dy in enumerate(range(-radius, radius + 1)):
        jj = jn + np.int32(dy)
        ii = inn[None, :] + dxs[:, None]
        inside = (
            ((jj >= 0) & (jj < ny))[None, :]
            & (ii >= 0)
            & (ii < nx)
        )
        jc = np.clip(jj, 0, ny - 1)
        ic = np.clip(ii, 0, nx - 1)
        vals = src_c[jc[None, :], ic]
        good = inside & (cat_c[jc[None, :], ic] == CAT_VALID) & np.isfinite(vals)
        wo = wy_o[ky] * wx_o
        wd = wy_d[ky] * wx_d
        wc = wy_c[ky] * wx_c
        mass_all += np.sum(wo, axis=0, dtype=np.float32)
        mass_echo += np.sum(np.where(good, wo, zero), axis=0, dtype=np.float32)
        disc_mass += np.sum(np.where(good, wd, zero), axis=0, dtype=np.float32)
        weighted = np.where(good, wc * vals.astype(np.float32), zero)
        num += np.sum(weighted, axis=0, dtype=np.float32)
        den += np.sum(np.where(good, wc, zero), axis=0, dtype=np.float32)

    occ = mass_echo / np.maximum(mass_all, np.float32(1e-6))
    blob = np.clip((occ - _OCC_LO) / (_OCC_HI - _OCC_LO), 0.0, 1.0)
    disc = np.clip((disc_mass - _DISC_LO) / (_DISC_HI - _DISC_LO), 0.0, 1.0)
    disc_keep = np.clip(
        (_DISC_KEEP_HI - occ) / (_DISC_KEEP_HI - _DISC_KEEP_LO), 0.0, 1.0
    )
    # The disc covers an isolated cell the contour has not reached. On a
    # storm edge the contour already owns the rim, and the disc stays off.
    edge_scale[ys, xs] = np.maximum(blob, disc * disc_keep).astype(np.float32)

    use = den > np.float32(1e-6)
    color = np.zeros(n, dtype=np.float32)
    color[use] = num[use] / den[use]
    own_d2 = dj[radius] ** 2 + di[radius] ** 2
    own = np.exp(-own_d2 * inv_peak).astype(np.float32)
    nearest_val = src_c[jn, inn].astype(np.float32)
    hotter = np.clip((nearest_val - color) / _CORE_RISE, 0.0, 1.0)
    mix = (_PEAK_MIX * own * hotter).astype(np.float32)
    pulled = (color * (np.float32(1.0) - mix) + nearest_val * mix).astype(np.float32)
    picked = np.full(n, np.nan, dtype=np.float32)
    picked[use] = pulled[use]
    out_dbz[ys, xs] = picked
    return out_dbz, edge_scale


def sample_masked_splat(
    dbz: np.ndarray,
    lat: np.ndarray,
    lon: np.ndarray,
    qlat: np.ndarray,
    qlon: np.ndarray,
    category: Optional[np.ndarray] = None,
    radius: int = _SPLAT_RADIUS,
) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Contour echo inside the mask. Never paint clear air.

    The nearest source cell is the footprint. A query whose nearest cell is
    no-echo or missing stays empty, even if the smoothed outline would have
    reached it. Inside echo, alpha follows a short occupancy contour so the
    square rim is inset without fading the rest of the cell. A lone valid
    cell is kept as a small disc. dBZ is a narrow blend of nearby echo
    cells, with a light pull back toward a hotter peak cell.
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
    edge_scale = np.zeros(qlat_a.shape, dtype=np.float32)
    frac = _grid_fractional(lat, lon, qlat_a, qlon_a)
    if frac is None or src.size == 0:
        return out_dbz, out_cat, edge_scale
    j_f, i_f = frac
    j_n, i_n, in_grid = _nearest_indexers(lat, lon, qlat_a, qlon_a)
    if not np.any(in_grid):
        return out_dbz, out_cat, edge_scale
    out_cat[in_grid] = cat[j_n[in_grid], i_n[in_grid]]
    echo = in_grid & (out_cat == CAT_VALID)
    if not np.any(echo):
        return out_dbz, out_cat, edge_scale

    # Kernel taps past the grid count as clear air, so the mosaic edge does
    # not brighten just because the window is truncated. That rule lives in
    # _splat_echo; this call does not change the footprint or the contour.
    splat_dbz, splat_edge = _splat_echo(
        src, cat, j_f, i_f, j_n, i_n, echo, radius
    )
    out_dbz[echo] = splat_dbz[echo]
    edge_scale[echo] = splat_edge[echo]
    return out_dbz, out_cat, edge_scale


def _query_lonlat(z: int, x: int, y: int, tile_size: int):
    # Same pixel centers as tile_pixel_centers, without a Python loop per pixel.
    step = 1.0 / tile_size
    idx = np.arange(tile_size, dtype=np.float64) + 0.5
    gx = x + idx * step
    gy = y + idx * step
    n = float(1 << z)
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
    cell. ``masked-splat`` (RALA) and ``masked-bilinear`` keep that nearest
    cell as the footprint and only blend inside echo, so a clear-air neighbor
    stays empty. Splat insets the square rim with a short contour and blends
    dBZ only across the shared face of neighboring echo cells; a lone echo
    cell stays a small disc.
    """
    qlon, qlat = _query_lonlat(z, x, y, tile_size)
    if sample_mode in (SAMPLE_MASKED_BILINEAR, SAMPLE_MASKED_SPLAT):
        sampler = (
            sample_masked_splat
            if sample_mode == SAMPLE_MASKED_SPLAT
            else sample_masked_bilinear
        )
        sampled, sampled_cat, edge_scale = sampler(
            frame.dbz, frame.lat, frame.lon, qlat, qlon, frame.category
        )
        rgba = palette.colorize(sampled, category=sampled_cat)
        # Palette alpha (wispy low dBZ) times the in-mask stamp. Clear-air
        # queries have edge_scale 0, so they cannot pick up a neighbor's color.
        faded = rgba[..., 3].astype(np.float32) * edge_scale
        rgba[..., 3] = np.clip(np.rint(faded), 0, 255).astype(np.uint8)
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
            f"Use {SAMPLE_NEAREST!r}, {SAMPLE_MASKED_BILINEAR!r}, or {SAMPLE_MASKED_SPLAT!r}."
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


# optimize=True tries every PNG filter. On a noisy 512×512 RGBA tile that
# was ~0.37s for a ~2% smaller file — several minutes of a CONUS frame.
# Level 6 is the usual zlib default and stays within a few percent of that size.
_PNG_COMPRESS_LEVEL = 6


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
    workers: int = 1,
) -> Dict:
    """Write `{z}/{x}/{y}.png` under out_dir. Returns tile stats.

    ``workers`` > 1 renders echo tiles on a thread pool. Numpy releases the
    GIL inside the splat, so two workers fit a 2 vCPU host. The frame grid
    is read-only and is not copied per worker.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    skipped = 0
    jobs: List[Tuple[int, int, int]] = []
    # An empty parent tile has no echo in any child. Mark it and skip the
    # descendants instead of scanning each of them.
    empty_parents: set[Tuple[int, int, int]] = set()
    for z, x, y in iter_tiles(bbox, min_zoom, max_zoom):
        if skip_empty:
            parent = (z - 1, x // 2, y // 2)
            if z > min_zoom and parent in empty_parents:
                skipped += 1
                continue
            if not tile_has_echo(frame, z, x, y):
                empty_parents.add((z, x, y))
                skipped += 1
                continue
        jobs.append((z, x, y))

    def _one(item: Tuple[int, int, int]) -> Optional[str]:
        z, x, y = item
        image, has_echo = render_tile(
            frame, palette, z, x, y, tile_size, sample_mode=sample_mode
        )
        if skip_empty and not has_echo:
            return None
        dest = out_dir / str(z) / str(x) / f"{y}.png"
        dest.parent.mkdir(parents=True, exist_ok=True)
        image.save(
            dest,
            format="PNG",
            optimize=False,
            compress_level=_PNG_COMPRESS_LEVEL,
        )
        return f"{z}/{x}/{y}.png"

    paths: List[str] = []
    worker_n = max(1, int(workers))
    if worker_n == 1 or len(jobs) <= 1:
        rendered = (_one(item) for item in jobs)
    else:
        pool = ThreadPoolExecutor(
            max_workers=min(worker_n, len(jobs)),
            thread_name_prefix="rala-tile",
        )
        rendered = pool.map(_one, jobs)
    try:
        for rel in rendered:
            if rel is None:
                skipped += 1
            else:
                paths.append(rel)
    finally:
        if worker_n > 1 and len(jobs) > 1:
            pool.shutdown(wait=True)
    written = len(paths)
    stats = {
        "written": written,
        "skipped_empty": skipped,
        "tile_size": tile_size,
        "min_zoom": min_zoom,
        "max_zoom": max_zoom,
        "workers": worker_n if len(jobs) > 1 else 1,
    }
    log.info(
        "Wrote %d tiles (%d empty skipped, workers=%d) → %s",
        written,
        skipped,
        stats["workers"],
        out_dir,
    )
    return stats


def write_colorbar(palette: Palette, path: Path) -> None:
    """Write the ramp from the display cutoff through the top stop (at least 75 dBZ)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    top = 75.0
    if palette.stops:
        top = max(75.0, float(palette.stops[-1].dbz))
    palette.colorbar(dbz_min=palette.min_dbz, dbz_max=top).save(path, format="PNG")
