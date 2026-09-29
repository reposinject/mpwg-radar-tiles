"""Numerical dBZ interpolation candidates for RALA.

The cooker calls ``sample_bilinear_peak_hold`` only when
``MPWG_RALA_DBZ_INTERP=bilinear_peak_hold``. The default is off, and that
path is the p3l splat. This module does not change ``SPATIAL_REVISION`` or
the p3k palette.

Production paint order (unchanged by this module):

    decode_grib2
      classify_dbz → float32 dBZ + category (valid / no-echo / missing)
      lat/lon axes stay float64
    cook → apply_mode(clean) for RALA
      no dBZ floor, no despeckle, no grid smooth (the grid is still native)
    write_tiles → render_tile(sample_mode="masked-splat")
      Web Mercator pixel centers
      sample_masked_splat          ← numerical dBZ (p3l seam) + category + edge
      palette.colorize             ← p3k 0.1 dBZ LUT  (RGBA only after this)
      alpha *= edge_scale          ← clear-air inset, still not an RGB blend

p3l already blends dBZ, but only outside an exact core of 0.28 cell
(``tiles._DETAIL_CORE``). Inside that core every sample is the source cell,
so a z9 tile still carries a flat native-cell plateau. Mapbox linear overzoom
softens the seam; it does not remove the plateau. That is the block James
measured at Dickinson (visible pitch / native cell ≈ 1).

A bounded interpolator replaces the **dBZ** coming out of ``sample_masked_splat``
and must run **before** ``palette.colorize``. It does not blend RGBA, does not
change the category mask, and does not change ``SPATIAL_REVISION`` or the p3k
palette. With the review flag off, ``render_tile`` never calls into this module.

Candidates
----------
bilinear-masked
    Existing ``sample_masked_bilinear``. Exact at cell centers. NO-ECHO and
    missing are not samples. No flat core, so a stepped field ramps across
    the cell. Off-center tile pixels of a sharp peak cool toward neighbors.

bicubic-clipped
    Catmull-Rom (Keys a=-0.5) on the 4×4 around the query, only when every
    tap is valid echo. Otherwise masked bilinear. The result is clipped to
    the min/max of the 2×2 cell centers so the cubic cannot invent a hotter
    peak or a colder hole. Exact at cell centers. Same off-center cooling
    as any interpolator that is not pinned.

bilinear-peak-hold
    Masked bilinear on ordinary cells. Where the nearest cell is a
    plateau-aware local maximum, keep the p3l dBZ instead (exact core and
    the existing smoothstep seam). Sharp cores stay on today's footprint.
    The rest of the field loses the flat plateau. Optional ``hold_min_dbz``
    also keeps p3l on every cell at or above that value (magenta sensitivity
    only; not the default).

A limited-radius Gaussian is not a candidate. It is not interpolating, so a
cell center becomes a weighted average of its neighbors. That is the p3h
failure (a 68 dBZ cell in 30 dBZ rain came back at 62.6).
"""

from __future__ import annotations

from typing import Optional, Tuple

import numpy as np
from PIL import Image

from mpwg_radar.grib import ReflectivityFrame
from mpwg_radar.palette import Palette
from mpwg_radar.products import CAT_NO_ECHO, CAT_VALID
from mpwg_radar.tiles import (
    _grid_fractional,
    _nearest_indexers,
    _query_lonlat,
    sample_masked_bilinear,
    sample_masked_splat,
)

# Names a caller can pass to sample_dbz_candidate / sample_and_paint.
# "p3l" is the production seam, included so an A/B can paint it through the
# same LUT and the same clear-air edge as the candidates.
PRODUCTION_NAME = "p3l"
CANDIDATE_BILINEAR = "bilinear-masked"
CANDIDATE_BICUBIC = "bicubic-clipped"
CANDIDATE_PEAK_HOLD = "bilinear-peak-hold"
CANDIDATE_IDS = (CANDIDATE_BILINEAR, CANDIDATE_BICUBIC, CANDIDATE_PEAK_HOLD)

# z9 CSS pixels per native 0.01° cell at Dickinson (~46.88°N), from the
# p3n measurement. Half a pixel is the farthest a tile-pixel center can sit
# from a cell center. E–W is the worse axis (~0.137 cell).
Z9_PX_PER_CELL_NS = 5.32
Z9_PX_PER_CELL_EW = 3.65
Z9_HALF_PIXEL_NS = 0.5 / Z9_PX_PER_CELL_NS
Z9_HALF_PIXEL_EW = 0.5 / Z9_PX_PER_CELL_EW


def local_peak_mask(
    dbz: np.ndarray,
    category: Optional[np.ndarray] = None,
    hold_min_dbz: Optional[float] = None,
) -> np.ndarray:
    """Plateau-aware local maxima on the source grid.

    A valid cell is held when its dBZ is greater than or equal to every
    8-neighbor. Invalid neighbors are ignored (they do not block a peak and
    they are not a value to beat). Flat ties are included. On a perfectly
    flat neighborhood bilinear already matches p3l, so holding those cells
    does not repaint them.

    ``hold_min_dbz`` additionally holds every valid cell at or above that
    threshold. Used only for the magenta-shoulder sensitivity.
    """
    src = np.asarray(dbz, dtype=np.float32)
    if category is None:
        valid = np.isfinite(src)
    else:
        cat = np.asarray(category)
        if cat.shape != src.shape:
            raise ValueError("category shape must match dbz")
        valid = cat == CAT_VALID
    valid = valid & np.isfinite(src)
    filled = np.where(valid, src, np.float32(-1e30))
    padded = np.pad(filled, 1, mode="constant", constant_values=np.float32(-1e30))
    not_below = np.ones(src.shape, dtype=bool)
    height, width = src.shape
    for dj in (-1, 0, 1):
        for di in (-1, 0, 1):
            if dj == 0 and di == 0:
                continue
            window = padded[1 + dj : 1 + dj + height, 1 + di : 1 + di + width]
            not_below &= filled >= window
    hold = valid & not_below
    if hold_min_dbz is not None:
        hold = hold | (valid & (src >= np.float32(hold_min_dbz)))
    return hold


def _cubic_weights(t: np.ndarray) -> Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Catmull-Rom weights for taps at offsets -1, 0, +1, +2. ``t`` is in [0, 1]."""
    t = np.asarray(t, dtype=np.float64)
    t2 = t * t
    t3 = t2 * t
    w0 = -0.5 * t3 + t2 - 0.5 * t
    w1 = 1.5 * t3 - 2.5 * t2 + 1.0
    w2 = -1.5 * t3 + 2.0 * t2 + 0.5 * t
    w3 = 0.5 * t3 - 0.5 * t2
    return w0, w1, w2, w3


def sample_bicubic_clipped(
    dbz: np.ndarray,
    lat: np.ndarray,
    lon: np.ndarray,
    qlat: np.ndarray,
    qlon: np.ndarray,
    category: Optional[np.ndarray] = None,
) -> Tuple[np.ndarray, np.ndarray]:
    """Catmull-Rom dBZ, clipped to the 2×2 cell range. Mask stays nearest-cell.

    Returns ``(dbz, category)``. Category is the nearest source cell, same
    footprint as masked bilinear / p3l. A query whose 4×4 is not entirely
    valid echo falls back to masked bilinear, so a hole cannot ring. The
    cubic result is clipped to the min and max of the four surrounding valid
    cell centers, so it cannot exceed the local source range.
    """
    src = np.asarray(dbz)
    out_dbz, out_cat, _edge = sample_masked_bilinear(
        src, lat, lon, qlat, qlon, category
    )
    frac = _grid_fractional(lat, lon, qlat, qlon)
    if frac is None or src.size == 0 or not np.any(out_cat == CAT_VALID):
        return out_dbz, out_cat
    if category is None:
        cat = np.where(np.isfinite(src), CAT_VALID, CAT_NO_ECHO).astype(np.uint8)
    else:
        cat = np.asarray(category)
    j_f, i_f = frac
    j0 = np.floor(j_f).astype(np.int32)
    i0 = np.floor(i_f).astype(np.int32)
    wy = _cubic_weights(j_f - j0)
    wx = _cubic_weights(i_f - i0)
    ny, nx = src.shape
    acc = np.zeros(np.asarray(qlat).shape, dtype=np.float64)
    all_good = np.ones(np.asarray(qlat).shape, dtype=bool)
    c_min = np.full(np.asarray(qlat).shape, np.inf, dtype=np.float64)
    c_max = np.full(np.asarray(qlat).shape, -np.inf, dtype=np.float64)
    for aj, wj in enumerate(wy):
        dj = aj - 1
        for ai, wi in enumerate(wx):
            di = ai - 1
            jj = j0 + np.int32(dj)
            ii = i0 + np.int32(di)
            inside = (jj >= 0) & (jj < ny) & (ii >= 0) & (ii < nx)
            jc = np.clip(jj, 0, ny - 1)
            ic = np.clip(ii, 0, nx - 1)
            vals = src[jc, ic]
            good = inside & (cat[jc, ic] == CAT_VALID) & np.isfinite(vals)
            all_good &= good
            acc = acc + np.where(good, vals.astype(np.float64) * (wj * wi), 0.0)
            if dj in (0, 1) and di in (0, 1):
                finite_vals = vals.astype(np.float64)
                c_min = np.where(good, np.minimum(c_min, finite_vals), c_min)
                c_max = np.where(good, np.maximum(c_max, finite_vals), c_max)
    use = all_good & (out_cat == CAT_VALID)
    if np.any(use):
        clipped = np.clip(acc, c_min, c_max).astype(np.float32)
        out_dbz = np.array(out_dbz, dtype=np.float32, copy=True)
        out_dbz[use] = clipped[use]
    return out_dbz, out_cat


def sample_bilinear_peak_hold(
    dbz: np.ndarray,
    lat: np.ndarray,
    lon: np.ndarray,
    qlat: np.ndarray,
    qlon: np.ndarray,
    category: Optional[np.ndarray] = None,
    hold_min_dbz: Optional[float] = None,
    peak_mask: Optional[np.ndarray] = None,
) -> Tuple[np.ndarray, np.ndarray]:
    """Bilinear dBZ, with the p3l seam kept on local-maximum cells.

    Returns ``(dbz, category)``. Category is the production nearest-cell
    footprint. Queries whose nearest cell is held get ``sample_masked_splat``
    (exact 0.28-cell core and the smoothstep face). Every other echo query
    gets masked bilinear. NO-ECHO and missing stay NaN.

    ``peak_mask`` may be precomputed with ``local_peak_mask`` for the same
    grid and the same ``hold_min_dbz``. A mask built for a different threshold
    is not checked.
    """
    src = np.asarray(dbz)
    splat_dbz, splat_cat, _edge = sample_masked_splat(
        src, lat, lon, qlat, qlon, category
    )
    bilin_dbz, _bilin_cat, _edge_b = sample_masked_bilinear(
        src, lat, lon, qlat, qlon, category
    )
    if peak_mask is None:
        peak_mask = local_peak_mask(src, category, hold_min_dbz=hold_min_dbz)
    elif peak_mask.shape != src.shape:
        raise ValueError("peak_mask shape must match dbz")
    frac = _grid_fractional(lat, lon, qlat, qlon)
    out = np.array(bilin_dbz, dtype=np.float32, copy=True)
    if frac is not None and src.size:
        _j_n, i_n, ok = _nearest_indexers(lat, lon, qlat, qlon)
        j_n = _j_n
        height, width = src.shape
        jn = np.clip(j_n, 0, height - 1)
        inn = np.clip(i_n, 0, width - 1)
        use_peak = ok & peak_mask[jn, inn] & (splat_cat == CAT_VALID)
        out[use_peak] = splat_dbz[use_peak]
    out[splat_cat != CAT_VALID] = np.float32(np.nan)
    return out, splat_cat


def sample_dbz_candidate(
    name: str,
    dbz: np.ndarray,
    lat: np.ndarray,
    lon: np.ndarray,
    qlat: np.ndarray,
    qlon: np.ndarray,
    category: Optional[np.ndarray] = None,
    hold_min_dbz: Optional[float] = None,
    peak_mask: Optional[np.ndarray] = None,
) -> Tuple[np.ndarray, np.ndarray]:
    """Numerical dBZ for one offline candidate. Does not colorize.

    ``name`` is one of ``CANDIDATE_IDS``. The returned category is the
    nearest-cell footprint. Clear air is NaN.
    """
    if name == CANDIDATE_BILINEAR:
        sampled, cat, _edge = sample_masked_bilinear(
            dbz, lat, lon, qlat, qlon, category
        )
        return sampled, cat
    if name == CANDIDATE_BICUBIC:
        return sample_bicubic_clipped(dbz, lat, lon, qlat, qlon, category)
    if name == CANDIDATE_PEAK_HOLD:
        return sample_bilinear_peak_hold(
            dbz,
            lat,
            lon,
            qlat,
            qlon,
            category,
            hold_min_dbz=hold_min_dbz,
            peak_mask=peak_mask,
        )
    raise ValueError(
        f"Unknown dBZ interp candidate {name!r}. Choose from {CANDIDATE_IDS}."
    )


def colorize_with_production_mask(
    palette: Palette,
    dbz_values: np.ndarray,
    category: np.ndarray,
    edge_scale: np.ndarray,
) -> np.ndarray:
    """p3k LUT, then the p3l clear-air alpha inset.

    Non-valid category samples are forced to NaN before the LUT so a
    candidate cannot paint NO-ECHO or missing. RGB is never blended here;
    the only spatial mix already happened in dBZ.
    """
    cat = np.asarray(category)
    values = np.array(dbz_values, dtype=np.float32, copy=True)
    values[cat != CAT_VALID] = np.float32(np.nan)
    rgba = palette.colorize(values, category=cat)
    edge = np.asarray(edge_scale, dtype=np.float32)
    rgba[..., 3] = np.clip(
        np.rint(rgba[..., 3].astype(np.float32) * edge), 0, 255
    ).astype(np.uint8)
    return rgba


def sample_and_paint(
    frame: ReflectivityFrame,
    palette: Palette,
    qlat: np.ndarray,
    qlon: np.ndarray,
    name: str,
    peak_mask: Optional[np.ndarray] = None,
    hold_min_dbz: Optional[float] = None,
) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Sample numerical dBZ, then the production LUT and clear-air edge.

    ``name="p3l"`` uses ``sample_masked_splat`` for dBZ. Any candidate name
    replaces that dBZ and still uses the splat category and edge scale, so
    the A/B differs only in the numbers that enter the LUT.

    Returns ``(rgba, sampled_dbz, category)``.
    """
    splat_dbz, splat_cat, edge = sample_masked_splat(
        frame.dbz, frame.lat, frame.lon, qlat, qlon, frame.category
    )
    if name == PRODUCTION_NAME:
        sampled = splat_dbz
    else:
        sampled, _cat = sample_dbz_candidate(
            name,
            frame.dbz,
            frame.lat,
            frame.lon,
            qlat,
            qlon,
            frame.category,
            hold_min_dbz=hold_min_dbz,
            peak_mask=peak_mask,
        )
        bad = (splat_cat == CAT_VALID) & ~np.isfinite(sampled)
        if np.any(bad):
            sampled = np.array(sampled, dtype=np.float32, copy=True)
            sampled[bad] = splat_dbz[bad]
    rgba = colorize_with_production_mask(palette, sampled, splat_cat, edge)
    return rgba, sampled, splat_cat


def paint_candidate_tile(
    frame: ReflectivityFrame,
    palette: Palette,
    z: int,
    x: int,
    y: int,
    name: str,
    tile_size: int = 512,
    peak_mask: Optional[np.ndarray] = None,
    hold_min_dbz: Optional[float] = None,
) -> Image.Image:
    """One 512 px tile through an offline candidate, or through p3l."""
    qlon, qlat = _query_lonlat(z, x, y, tile_size)
    rgba, _sampled, _cat = sample_and_paint(
        frame,
        palette,
        qlat,
        qlon,
        name,
        peak_mask=peak_mask,
        hold_min_dbz=hold_min_dbz,
    )
    return Image.fromarray(rgba)
