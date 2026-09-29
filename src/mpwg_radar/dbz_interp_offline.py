"""Numerical dBZ interpolation candidates for RALA.

The cooker calls into this module only when ``MPWG_RALA_DBZ_INTERP`` is
``bilinear_peak_hold`` (A) or ``tight_peak_hold`` (B). The default is off,
and that path is the p3l splat. This module does not change
``SPATIAL_REVISION`` or the p3k palette.

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

bilinear-peak-hold (flag A, ``bilinear_peak_hold``)
    Masked bilinear on ordinary cells. Where the nearest cell is a
    plateau-aware local maximum, keep the p3l dBZ instead (exact core and
    the existing smoothstep seam). Sharp cores stay on today's footprint.
    The rest of the field loses the flat plateau. A 1D step's 10–90% width
    is 0.80 cell, which is the whole center-to-center ramp. Optional
    ``hold_min_dbz`` also keeps p3l on every cell at or above that value
    (magenta sensitivity only; not the default).

tight-peak-hold (flag B, ``tight_peak_hold``)
    Same peak-hold and the same valid corners as A. The bilinear fraction
    is remapped with power 2 so neighbor weight grows more slowly near a
    cell center and the 10–90% width of a 1D step is 0.50 cell. Still a
    convex combination: no overshoot, exact at cell centers, NO-ECHO is
    not a sample. Not a sharpen mask and not a zoom-dependent kernel.

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
from mpwg_radar.products import CAT_MISSING, CAT_NO_ECHO, CAT_VALID
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
CANDIDATE_TIGHT = "tight-peak-hold"
# Final-harness only. Not cooker flags. C keeps a cubic when it stays inside
# the 2×2 cell centers and otherwise uses bilinear, so a clip cannot stamp a
# flat shelf. D is successive monotone cubic Hermite (Fritsch–Carlson slopes).
CANDIDATE_BOUNDED_CUBIC = "bounded-cubic"
CANDIDATE_MONOTONE = "monotone-pchip"
CANDIDATE_IDS = (
    CANDIDATE_BILINEAR,
    CANDIDATE_BICUBIC,
    CANDIDATE_PEAK_HOLD,
    CANDIDATE_TIGHT,
    CANDIDATE_BOUNDED_CUBIC,
    CANDIDATE_MONOTONE,
)
# Power on the bilinear fraction for B. k=1 is plain bilinear (10–90% width
# 0.80 cell). k=2 is 0.50 cell. k=3 is 0.35 cell and the near-flat shelf on
# a 15 dBZ step grows toward p3l's 0.28 core, which is the block James
# already rejected. k=2 is the measured middle. It is not a function of zoom:
# one z9 raster cannot get narrower in screen pixels as the map overzooms.
TIGHT_BIAS_POWER = 2.0

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


def _gather4(src, cat, j0, i0):
    """4×4 taps at (j0-1..j0+2, i0-1..i0+2) plus a validity mask.

    NO-ECHO and missing are not numeric taps. Out-of-grid taps are invalid.
    """
    shape = np.asarray(j0).shape
    vals = np.zeros((4, 4) + shape, dtype=np.float64)
    good = np.zeros((4, 4) + shape, dtype=bool)
    ny, nx = src.shape
    for aj, dj in enumerate((-1, 0, 1, 2)):
        for ai, di in enumerate((-1, 0, 1, 2)):
            jj = j0 + np.int32(dj)
            ii = i0 + np.int32(di)
            inside = (jj >= 0) & (jj < ny) & (ii >= 0) & (ii < nx)
            jc = np.clip(jj, 0, ny - 1)
            ic = np.clip(ii, 0, nx - 1)
            sample = src[jc, ic]
            ok = inside & (cat[jc, ic] == CAT_VALID) & np.isfinite(sample)
            vals[aj, ai] = np.where(ok, sample.astype(np.float64), 0.0)
            good[aj, ai] = ok
    return vals, good


def _hermite(y0, y1, m0, m1, t):
    """Cubic Hermite on t in [0, 1]. Slopes are dy/dt over that one cell."""
    t = np.asarray(t, dtype=np.float64)
    t2 = t * t
    t3 = t2 * t
    h00 = 2.0 * t3 - 3.0 * t2 + 1.0
    h10 = t3 - 2.0 * t2 + t
    h01 = -2.0 * t3 + 3.0 * t2
    h11 = t3 - t2
    return h00 * y0 + h10 * m0 + h01 * y1 + h11 * m1


def _fc_limit(m0, m1, secant, ok):
    """Fritsch–Carlson slope cap. The segment then stays between its endpoints."""
    m0 = np.array(m0, dtype=np.float64, copy=True)
    m1 = np.array(m1, dtype=np.float64, copy=True)
    d = np.asarray(secant, dtype=np.float64)
    flat = ok & (np.abs(d) < 1e-8)
    live = ok & ~flat
    m0 = np.where(flat, 0.0, m0)
    m1 = np.where(flat, 0.0, m1)
    alpha = np.divide(m0, d, out=np.zeros(d.shape, dtype=np.float64), where=live)
    beta = np.divide(m1, d, out=np.zeros(d.shape, dtype=np.float64), where=live)
    alpha = np.maximum(alpha, 0.0)
    beta = np.maximum(beta, 0.0)
    norm = alpha * alpha + beta * beta
    scale = np.ones(d.shape, dtype=np.float64)
    hot = live & (norm > 9.0)
    scale = np.where(hot, 3.0 / np.sqrt(np.maximum(norm, 1e-30)), 1.0)
    m0 = np.where(live, alpha * scale * d, np.where(flat, 0.0, m0))
    m1 = np.where(live, beta * scale * d, np.where(flat, 0.0, m1))
    m0 = np.where(ok, m0, 0.0)
    m1 = np.where(ok, m1, 0.0)
    return m0, m1


def _node_slope(d_left, d_right, left_ok, right_ok):
    """Harmonic-mean slope, zero at a sign change or a flat neighbor."""
    both = left_ok & right_ok & (d_left * d_right > 0.0)
    denom = d_left + d_right
    harm = np.divide(
        2.0 * d_left * d_right,
        denom,
        out=np.zeros(np.broadcast(d_left, d_right).shape, dtype=np.float64),
        where=both & (np.abs(denom) > 1e-12),
    )
    only_l = left_ok & ~right_ok
    only_r = right_ok & ~left_ok
    slope = np.where(only_l, d_left, 0.0)
    slope = np.where(only_r, d_right, slope)
    slope = np.where(both, harm, slope)
    return slope.astype(np.float64)


def _pchip1(ym1, y0, y1, y2, gm1, g0, g1, g2, t):
    """1D monotone cubic on the segment from y0 to y1. Invalid ends → NaN."""
    ok = g0 & g1
    d_left = y0 - ym1
    d_mid = y1 - y0
    d_right = y2 - y1
    left_ok = gm1 & g0
    mid_ok = g0 & g1
    right_ok = g1 & g2
    m0 = _node_slope(d_left, d_mid, left_ok, mid_ok)
    m1 = _node_slope(d_mid, d_right, mid_ok, right_ok)
    m0, m1 = _fc_limit(m0, m1, d_mid, mid_ok)
    value = _hermite(y0, y1, m0, m1, t)
    return np.where(ok, value, np.nan)


def sample_bounded_cubic(
    dbz: np.ndarray,
    lat: np.ndarray,
    lon: np.ndarray,
    qlat: np.ndarray,
    qlon: np.ndarray,
    category: Optional[np.ndarray] = None,
) -> Tuple[np.ndarray, np.ndarray]:
    """Candidate C. Catmull-Rom where it stays inside the 2×2, else bilinear.

    A hard clip would flatten every overshoot onto the local max and stamp a
    plateau. This keeps the cubic only when it is already inside the min/max
    of the four surrounding valid cell centers, and leaves the masked bilinear
    sample in place otherwise. NO-ECHO is never a tap. Cell centers match the
    source because both kernels are interpolating. No value is written outside
    the local source range.
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
    inside_hull = all_good & (acc >= c_min - 1e-4) & (acc <= c_max + 1e-4)
    use = inside_hull & (out_cat == CAT_VALID) & np.isfinite(out_dbz)
    if np.any(use):
        out_dbz = np.array(out_dbz, dtype=np.float32, copy=True)
        out_dbz[use] = acc[use].astype(np.float32)
    return out_dbz, out_cat


def sample_monotone_pchip(
    dbz: np.ndarray,
    lat: np.ndarray,
    lon: np.ndarray,
    qlat: np.ndarray,
    qlon: np.ndarray,
    category: Optional[np.ndarray] = None,
) -> Tuple[np.ndarray, np.ndarray]:
    """Candidate D. Successive monotone cubic Hermite on valid echo only.

    Slopes are Fritsch–Carlson: zero at a local extremum, harmonic mean on a
    monotone run, then capped so each 1D segment stays between its endpoints.
    Longitude is interpolated first on the four bracketing rows, then latitude
    through those four results. A local maximum is reached at its own cell
    center and the tangent there is flat, but the value is not held across a
    core. Where an endpoint is NO-ECHO or missing the sample falls back to
    masked bilinear. Category stays the nearest cell.
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
    tj = (j_f - j0).astype(np.float64)
    ti = (i_f - i0).astype(np.float64)
    vals, good = _gather4(src, cat, j0, i0)
    row_v = []
    row_g = []
    for aj in range(4):
        v = _pchip1(
            vals[aj, 0],
            vals[aj, 1],
            vals[aj, 2],
            vals[aj, 3],
            good[aj, 0],
            good[aj, 1],
            good[aj, 2],
            good[aj, 3],
            ti,
        )
        row_v.append(v)
        row_g.append(np.isfinite(v))
    mono = _pchip1(
        row_v[0],
        row_v[1],
        row_v[2],
        row_v[3],
        row_g[0],
        row_g[1],
        row_g[2],
        row_g[3],
        tj,
    )
    use = (out_cat == CAT_VALID) & np.isfinite(mono)
    if np.any(use):
        out_dbz = np.array(out_dbz, dtype=np.float32, copy=True)
        out_dbz[use] = mono[use].astype(np.float32)
    return out_dbz, out_cat


def _bias_unit(t: np.ndarray, power: float) -> np.ndarray:
    """Map a bilinear fraction into (0, 1) with less neighbor weight near 0 and 1.

    ``power == 1`` is the identity. ``power > 1`` stays on 0, 0.5, and 1, is
    monotonic, and pulls intermediate samples toward the nearer cell. The
    result is still in ``[0, 1]``, so a later convex combination cannot
    overshoot the valid corners.
    """
    arr = np.clip(np.asarray(t, dtype=np.float64), 0.0, 1.0)
    if power == 1.0:
        return arr.astype(np.float32)
    tp = np.power(arr, power)
    up = np.power(1.0 - arr, power)
    return (tp / np.maximum(tp + up, 1e-30)).astype(np.float32)


def sample_localized_bilinear(
    dbz: np.ndarray,
    lat: np.ndarray,
    lon: np.ndarray,
    qlat: np.ndarray,
    qlon: np.ndarray,
    category: Optional[np.ndarray] = None,
    power: float = TIGHT_BIAS_POWER,
) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Masked bilinear with a tighter fraction. Same corners, less neighbor pull.

    Returns ``(dbz, category, edge_scale)`` in the same shapes as
    ``sample_masked_bilinear``. Invalid corners are left out of the average.
    ``power == 1`` matches masked bilinear. Category is still the nearest cell.
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

    from mpwg_radar.tiles import _EDGE_CLEAR, _EDGE_SOLID

    ny, nx = src.shape
    j0 = np.floor(j_f).astype(np.int32)
    i0 = np.floor(i_f).astype(np.int32)
    tj = _bias_unit(j_f - j0, power)
    ti = _bias_unit(i_f - i0, power)
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


def width_10_90(values: np.ndarray) -> float:
    """Cell units from the 10% point to the 90% point along one center-to-center line.

    ``values`` is evenly spaced from cell center A (index 0) to cell center B
    (last index). Native nearest-cell sampling returns about ``1 / (n - 1)``
    because the step sits on one sample. Bilinear on a pure 1D step returns
    0.80. Returns NaN when the series never covers both levels.
    """
    y = np.asarray(values, dtype=np.float64).reshape(-1)
    if y.size < 2:
        return float("nan")
    start = float(y[0])
    end = float(y[-1])
    if not np.isfinite(start) or not np.isfinite(end) or abs(end - start) < 1e-3:
        return float("nan")
    frac = (y - start) / (end - start)
    x = np.linspace(0.0, 1.0, y.size)
    i10 = np.flatnonzero(frac >= 0.10)
    i90 = np.flatnonzero(frac >= 0.90)
    if i10.size == 0 or i90.size == 0:
        return float("nan")
    return float(x[i90[0]] - x[i10[0]])


def sample_bilinear_peak_hold(
    dbz: np.ndarray,
    lat: np.ndarray,
    lon: np.ndarray,
    qlat: np.ndarray,
    qlon: np.ndarray,
    category: Optional[np.ndarray] = None,
    hold_min_dbz: Optional[float] = None,
    peak_mask: Optional[np.ndarray] = None,
    weight_power: float = 1.0,
) -> Tuple[np.ndarray, np.ndarray]:
    """Bilinear dBZ, with the p3l seam kept on local-maximum cells.

    ``weight_power`` 1 is candidate A. ``TIGHT_BIAS_POWER`` is candidate B:
    the same hold, a narrower ramp on ordinary cells.

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
    if weight_power == 1.0:
        bilin_dbz, _bilin_cat, _edge_b = sample_masked_bilinear(
            src, lat, lon, qlat, qlon, category
        )
    else:
        bilin_dbz, _bilin_cat, _edge_b = sample_localized_bilinear(
            src, lat, lon, qlat, qlon, category, power=weight_power
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


def sample_tight_peak_hold(
    dbz: np.ndarray,
    lat: np.ndarray,
    lon: np.ndarray,
    qlat: np.ndarray,
    qlon: np.ndarray,
    category: Optional[np.ndarray] = None,
    hold_min_dbz: Optional[float] = None,
    peak_mask: Optional[np.ndarray] = None,
) -> Tuple[np.ndarray, np.ndarray]:
    """Candidate B. Peak-hold plus the tighter bounded bilinear."""
    return sample_bilinear_peak_hold(
        dbz,
        lat,
        lon,
        qlat,
        qlon,
        category,
        hold_min_dbz=hold_min_dbz,
        peak_mask=peak_mask,
        weight_power=TIGHT_BIAS_POWER,
    )


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
    if name == CANDIDATE_TIGHT:
        return sample_tight_peak_hold(
            dbz,
            lat,
            lon,
            qlat,
            qlon,
            category,
            hold_min_dbz=hold_min_dbz,
            peak_mask=peak_mask,
        )
    if name == CANDIDATE_BOUNDED_CUBIC:
        return sample_bounded_cubic(dbz, lat, lon, qlat, qlon, category)
    if name == CANDIDATE_MONOTONE:
        return sample_monotone_pchip(dbz, lat, lon, qlat, qlon, category)
    raise ValueError(
        f"Unknown dBZ interp candidate {name!r}. Choose from {CANDIDATE_IDS}."
    )


def sample_review_dbz(
    flag: str,
    dbz: np.ndarray,
    lat: np.ndarray,
    lon: np.ndarray,
    qlat: np.ndarray,
    qlon: np.ndarray,
    category: Optional[np.ndarray] = None,
    hold_min_dbz: Optional[float] = None,
    peak_mask: Optional[np.ndarray] = None,
) -> Tuple[np.ndarray, np.ndarray]:
    """Numerical dBZ for one ``MPWG_RALA_DBZ_INTERP`` review token."""
    key = (flag or "").strip().lower().replace("-", "_")
    if key == "bilinear_peak_hold":
        name = CANDIDATE_PEAK_HOLD
    elif key == "tight_peak_hold":
        name = CANDIDATE_TIGHT
    else:
        raise ValueError(
            f"Unknown review sampler {flag!r}. "
            "Use bilinear_peak_hold or tight_peak_hold."
        )
    return sample_dbz_candidate(
        name,
        dbz,
        lat,
        lon,
        qlat,
        qlon,
        category,
        hold_min_dbz=hold_min_dbz,
        peak_mask=peak_mask,
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
