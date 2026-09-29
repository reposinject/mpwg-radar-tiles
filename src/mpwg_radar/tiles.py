"""Render 512×512 XYZ PNG tiles from a physical dBZ grid."""

from __future__ import annotations

import json
import logging
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

import numpy as np
from PIL import Image

from mpwg_radar.config import RALA_DBZ_INTERP_BILINEAR_PEAK_HOLD
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


# p3l spatial stamp. Same one adjacent-cell seam as p3i (replaces the p3h
# multi-kernel blur stack). p3i used _DETAIL_CORE=0.12 and left a ~0.55-cell
# 10–90% face ramp across ~75% of each half-cell — soft enough that continuous
# p3k colorize + Mapbox linear reads as blob soup. p3l raises the exact core
# to 0.28 so the seam is narrower (~0.31-cell 10–90; blend area ~44% of the
# half-cell) while the shared face is still 50/50 and cell centers stay exact
# (spatial_peak_loss=0). No quantize, no blur restore, no fake sharpen.
# Do not put the p3h kernels back.
SPATIAL_REVISION = "p3l"
# |offset from the cell center|, in MRMS cells, inside which dBZ is the
# source cell exactly. Past this, a smoothstep reaches a 50/50 blend at the
# shared face with a valid neighbor. 0.28 keeps a wider exact core than p3i's
# 0.12 so internal faces stay tighter without returning nearest-cell Lego.
_DETAIL_CORE = np.float32(0.28)
# Alpha inset toward a non-echo neighbor (no-echo, missing, or off the
# mosaic). Starts here and reaches 0 at the cell edge. Echo neighbors do
# not fade, so internal structure is not an alpha blur.
_EDGE_FADE_START = np.float32(0.20)


def _smoothstep01(t: np.ndarray) -> np.ndarray:
    clipped = np.clip(np.asarray(t, dtype=np.float32), 0.0, 1.0)
    return (clipped * clipped * (np.float32(3.0) - np.float32(2.0) * clipped)).astype(
        np.float32
    )


def _splat_echo(
    src: np.ndarray,
    cat: np.ndarray,
    j_f: np.ndarray,
    i_f: np.ndarray,
    j_n: np.ndarray,
    i_n: np.ndarray,
    echo: np.ndarray,
) -> Tuple[np.ndarray, np.ndarray]:
    """One detail-preserving resample for echo pixels only.

    The nearest source cell is the footprint. dBZ inside ``_DETAIL_CORE`` of
    that cell's center is the source value, so a narrow core and a local
    maximum are not averaged down. Across the shared face with another valid
    cell, a smoothstep seam hides the hard stair. No-echo, missing, and
    out-of-mosaic neighbors are not samples: dBZ does not step toward them,
    and alpha fades only inside the echo cell so the square rim comes off
    without painting clear air. Clear pixels are not visited.
    """
    out_dbz = np.full(echo.shape, np.nan, dtype=np.float32)
    edge_scale = np.zeros(echo.shape, dtype=np.float32)
    ys, xs = np.nonzero(echo)
    if ys.size == 0:
        return out_dbz, edge_scale

    jn_full = j_n[ys, xs].astype(np.int32, copy=False)
    in_full = i_n[ys, xs].astype(np.int32, copy=False)
    # Offset from the nearest cell center, in cell units, before the crop.
    du = (i_f[ys, xs] - in_full.astype(np.float64)).astype(np.float32)
    dv = (j_f[ys, xs] - jn_full.astype(np.float64)).astype(np.float32)
    # The 3×3 around these cells. The full grid stays a view.
    j0 = max(0, int(jn_full.min()) - 1)
    j1 = min(int(src.shape[0]), int(jn_full.max()) + 2)
    i0 = max(0, int(in_full.min()) - 1)
    i1 = min(int(src.shape[1]), int(in_full.max()) + 2)
    src_c = np.asarray(src, dtype=np.float32)[j0:j1, i0:i1]
    cat_c = cat[j0:j1, i0:i1]
    ny, nx = src_c.shape
    jn = jn_full - np.int32(j0)
    inn = in_full - np.int32(i0)

    def _at(jj: np.ndarray, ii: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
        inside = (jj >= 0) & (jj < ny) & (ii >= 0) & (ii < nx)
        jc = np.clip(jj, 0, ny - 1)
        ic = np.clip(ii, 0, nx - 1)
        vals = src_c[jc, ic]
        good = inside & (cat_c[jc, ic] == CAT_VALID) & np.isfinite(vals)
        return vals.astype(np.float32, copy=False), good

    own_v, own_g = _at(jn, inn)
    one = np.int32(1)
    east_v, east_g = _at(jn, inn + one)
    west_v, west_g = _at(jn, inn - one)
    south_v, south_g = _at(jn + one, inn)
    north_v, north_g = _at(jn - one, inn)

    su = np.sign(du).astype(np.int32)
    sv = np.sign(dv).astype(np.int32)
    h_v = np.where(su > 0, east_v, west_v).astype(np.float32)
    h_g = np.where(su > 0, east_g, west_g) & (su != 0)
    v_v = np.where(sv > 0, south_v, north_v).astype(np.float32)
    v_g = np.where(sv > 0, south_g, north_g) & (sv != 0)
    d_v, d_g = _at(jn + sv, inn + su)

    span = np.float32(0.5) - _DETAIL_CORE
    tx = _smoothstep01((np.abs(du) - _DETAIL_CORE) / span)
    ty = _smoothstep01((np.abs(dv) - _DETAIL_CORE) / span)
    half = np.float32(0.5)
    zero = np.float32(0.0)
    wx = np.where(h_g, half * tx, zero).astype(np.float32)
    wy = np.where(v_g, half * ty, zero).astype(np.float32)
    w_h = wx * (np.float32(1.0) - wy)
    w_v = wy * (np.float32(1.0) - wx)
    # Diagonal only when both orthogonal neighbors are real echo. A clear
    # corner does not lend its (absent) dBZ, and a diagonal-only touch does
    # not bleed around a no-echo cell.
    d_ok = d_g & h_g & v_g
    w_d = np.where(d_ok, wx * wy, zero).astype(np.float32)
    w_own = np.float32(1.0) - w_h - w_v - w_d
    # Zero the unused taps. A NaN neighbor times a zero weight is still NaN.
    h_safe = np.where(h_g, h_v, zero)
    v_safe = np.where(v_g, v_v, zero)
    d_safe = np.where(d_ok, d_v, zero)
    own_safe = np.where(own_g, own_v, zero)
    color = (w_own * own_safe + w_h * h_safe + w_v * v_safe + w_d * d_safe).astype(
        np.float32
    )
    picked = np.full(ys.shape, np.nan, dtype=np.float32)
    picked[own_g] = color[own_g]
    out_dbz[ys, xs] = picked

    fade_span = np.float32(0.5) - _EDGE_FADE_START

    def _side(delta: np.ndarray, neighbor_clear: np.ndarray) -> np.ndarray:
        fade = np.float32(1.0) - _smoothstep01((delta - _EDGE_FADE_START) / fade_span)
        return np.where((delta > 0) & neighbor_clear, fade, np.float32(1.0)).astype(
            np.float32
        )

    edge = (
        _side(du, ~east_g)
        * _side(-du, ~west_g)
        * _side(dv, ~south_g)
        * _side(-dv, ~north_g)
    )
    edge_scale[ys, xs] = edge.astype(np.float32)
    return out_dbz, edge_scale


def sample_masked_splat(
    dbz: np.ndarray,
    lat: np.ndarray,
    lon: np.ndarray,
    qlat: np.ndarray,
    qlon: np.ndarray,
    category: Optional[np.ndarray] = None,
    radius: int = 1,
) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """One mask-clipped seam. Never paint clear air.

    The nearest source cell is the footprint. A query whose nearest cell is
    no-echo or missing stays that category, with NaN dBZ and edge scale 0,
    even next to a core. Inside echo, dBZ is the source cell until the outer
    seam, where two valid cells meet. Alpha insets only the rim that faces
    clear air, missing data, or the mosaic edge. ``radius`` is unused: p3l
    does not run a wide kernel. It stays so older callers still import.
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

    # Off-grid taps count as clear air inside _splat_echo, so the mosaic
    # edge does not brighten from a truncated window. `radius` is ignored.
    del radius
    splat_dbz, splat_edge = _splat_echo(src, cat, j_f, i_f, j_n, i_n, echo)
    out_dbz[echo] = splat_dbz[echo]
    edge_scale[echo] = splat_edge[echo]
    return out_dbz, out_cat, edge_scale


def spatial_peak_loss(
    dbz: np.ndarray,
    lat: np.ndarray,
    lon: np.ndarray,
    category: Optional[np.ndarray] = None,
) -> Dict[str, object]:
    """Raw vs post-spatial dBZ at source cell centers.

    The raw value is the decoded cell. The post value is
    ``sample_masked_splat`` at that cell's lat/lon. Loss is raw minus post:
    positive means the spatial stage cooled the cell. A local maximum is a
    valid cell strictly hotter than every valid 8-neighbor (an isolated cell
    counts). p3l is exact at those centers. The retired p3h color Gaussian
    was not: a 68 dBZ cell in 30 dBZ rain lost 5.4 dBZ at its own center.
    """
    src = np.asarray(dbz, dtype=np.float32)
    if category is None:
        valid = np.isfinite(src)
        cat = np.where(valid, CAT_VALID, CAT_NO_ECHO).astype(np.uint8)
    else:
        cat = np.asarray(category)
        valid = cat == CAT_VALID
    empty = {
        "spatial_revision": SPATIAL_REVISION,
        "cells": 0.0,
        "field_max_raw": float("nan"),
        "field_max_post": float("nan"),
        "field_max_loss": float("nan"),
        "center_loss_max": float("nan"),
        "center_abs_max": float("nan"),
        "local_max_count": 0.0,
        "local_max_raw": float("nan"),
        "local_max_post": float("nan"),
        "local_max_loss": float("nan"),
        "worst_local_max_loss": float("nan"),
    }
    if not np.any(valid):
        return empty
    qlon, qlat = np.meshgrid(np.asarray(lon, dtype=np.float64), np.asarray(lat, dtype=np.float64))
    post, _post_cat, _edge = sample_masked_splat(src, lat, lon, qlat, qlon, cat)
    raw = src[valid]
    got = post[valid]
    delta = raw - got
    field_max = float(np.max(raw))
    at_max = valid & np.isfinite(src) & (src >= field_max - np.float32(1e-4))
    loss_at_field_max = float(np.max(src[at_max] - post[at_max]))

    # Strict local maxima. Invalid neighbors are -inf so they do not block a peak.
    filled = np.where(valid & np.isfinite(src), src, np.float32(-1e30))
    padded = np.pad(filled, 1, mode="constant", constant_values=np.float32(-1e30))
    hotter = np.ones(src.shape, dtype=bool)
    height, width = src.shape
    for dj in (-1, 0, 1):
        for di in (-1, 0, 1):
            if dj == 0 and di == 0:
                continue
            window = padded[1 + dj : 1 + dj + height, 1 + di : 1 + di + width]
            hotter &= filled > window
    peaks = valid & hotter
    report = {
        "spatial_revision": SPATIAL_REVISION,
        "cells": float(int(valid.sum())),
        "field_max_raw": field_max,
        "field_max_post": float(np.max(got)),
        "field_max_loss": loss_at_field_max,
        "center_loss_max": float(np.max(delta)),
        "center_abs_max": float(np.max(np.abs(delta))),
        "local_max_count": float(int(peaks.sum())),
    }
    if np.any(peaks):
        peak_raw = src[peaks]
        peak_post = post[peaks]
        peak_loss = peak_raw - peak_post
        hottest = int(np.argmax(peak_raw))
        report["local_max_raw"] = float(peak_raw[hottest])
        report["local_max_post"] = float(peak_post[hottest])
        report["local_max_loss"] = float(peak_loss[hottest])
        report["worst_local_max_loss"] = float(np.max(peak_loss))
    else:
        report["local_max_raw"] = float("nan")
        report["local_max_post"] = float("nan")
        report["local_max_loss"] = float("nan")
        report["worst_local_max_loss"] = float("nan")
    return report


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
    dbz_interp: str = "",
    peak_mask: Optional[np.ndarray] = None,
) -> Tuple[Image.Image, bool]:
    """Return (PNG image, has_echo).

    ``nearest`` (composite) copies one MRMS cell into every pixel of that
    cell. ``masked-bilinear`` is the older in-mask blend and is not the RALA
    path. ``masked-splat`` (RALA, p3l) keeps the nearest cell as the footprint
    and runs one seam: cell centers stay on the source dBZ, the shared face
    of two echo cells blends, and clear air is never a sample or a paint target.

    ``dbz_interp`` defaults to off. ``bilinear_peak_hold`` replaces the splat
    dBZ with bilinear-on-dBZ plus the p3l seam on local maxima, then the same
    p3k LUT and the same splat category and clear-air alpha. Omitting it, or
    passing ``""``, is the production splat with no extra sampling.
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
        if (
            sample_mode == SAMPLE_MASKED_SPLAT
            and dbz_interp == RALA_DBZ_INTERP_BILINEAR_PEAK_HOLD
        ):
            # Category and edge_scale stay on the splat above. Only dBZ changes,
            # and only before colorize. Local import: the review sampler is not
            # on the default call path.
            from mpwg_radar.dbz_interp_offline import sample_bilinear_peak_hold

            held, _held_cat = sample_bilinear_peak_hold(
                frame.dbz,
                frame.lat,
                frame.lon,
                qlat,
                qlon,
                frame.category,
                peak_mask=peak_mask,
            )
            held = np.array(held, dtype=np.float32, copy=True)
            held[sampled_cat != CAT_VALID] = np.float32(np.nan)
            sampled = held
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
    dbz_interp: str = "",
) -> Dict:
    """Write `{z}/{x}/{y}.png` under out_dir. Returns tile stats.

    ``workers`` > 1 renders echo tiles on a thread pool. Numpy releases the
    GIL inside the splat, so two workers fit a 2 vCPU host. The frame grid
    is read-only and is not copied per worker.

    ``dbz_interp=""`` is production. ``bilinear_peak_hold`` is the review
    sampler and applies only to ``masked-splat`` (RALA).
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    peak_mask = None
    if (
        dbz_interp == RALA_DBZ_INTERP_BILINEAR_PEAK_HOLD
        and sample_mode == SAMPLE_MASKED_SPLAT
    ):
        from mpwg_radar.dbz_interp_offline import local_peak_mask

        peak_mask = local_peak_mask(frame.dbz, frame.category)
        log.warning(
            "EXPERIMENTAL RALA dBZ interp %s is ON for this tile write. "
            "Spatial stamp stays p3l. Do not deploy these tiles to production.",
            dbz_interp,
        )
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
            frame,
            palette,
            z,
            x,
            y,
            tile_size,
            sample_mode=sample_mode,
            dbz_interp=dbz_interp,
            peak_mask=peak_mask,
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
