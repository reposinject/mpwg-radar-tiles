"""QC / cleanup on the physical dBZ grid (before colorization).

This is not the Clean display cutoff. Tiles hide reflectivity below the
palette display_min (15 dBZ for composite Clean; -32 for RALA valid returns).
The float32 dBZ crop is written before this module runs.

Composite Clean still drops sub-10 dBZ clutter from the *mode* grid used
for despeckle/smooth. RALA disables that dBZ floor (James: no 10/15/20
cutoff) and skips despeckle so isolated valid cells survive. RALA smooth
is edge-aware: echo cells blend only with similar echo neighbors, so a
core is not smeared into light rain and clear air is never filled.

Modes
-----
clean     default: composite drops < ~10 dBZ clutter, despeckle, mild 3×3
standard  scaffold: hide < ~5 dBZ, no despeckle/smooth
all       scaffold: hide fill only (still masks MRMS no-echo / no-coverage)
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Dict, Optional

import numpy as np

from mpwg_radar.grib import ReflectivityFrame, classify_dbz
from mpwg_radar.products import CAT_NO_ECHO, CAT_VALID


@dataclass(frozen=True)
class ModeSpec:
    name: str
    min_dbz: float
    despeckle: bool
    min_component: int
    smooth: bool
    description: str


MODES: Dict[str, ModeSpec] = {
    "clean": ModeSpec(
        name="clean",
        min_dbz=10.0,
        despeckle=True,
        min_component=8,
        smooth=True,
        description="mode-grid clutter <~10 dBZ dropped, QC/despeckle, mild smooth (display cutoff is 15 dBZ in the palette)",
    ),
    "standard": ModeSpec(
        name="standard",
        min_dbz=5.0,
        despeckle=False,
        min_component=0,
        smooth=False,
        description="≥~5 dBZ, no extra cleanup (scaffold)",
    ),
    "all": ModeSpec(
        name="all",
        min_dbz=-10.0,
        despeckle=False,
        min_component=0,
        smooth=False,
        description="all finite reflectivity except MRMS fill (scaffold)",
    ),
}


def apply_mode(
    frame: ReflectivityFrame,
    mode: str,
    *,
    min_dbz: Optional[float] = None,
    apply_dbz_floor: bool = True,
    apply_despeckle: bool = True,
    edge_aware: bool = False,
) -> ReflectivityFrame:
    spec = MODES[mode]
    if frame.category is None:
        dbz, cat = classify_dbz(frame.dbz)
    else:
        dbz = frame.dbz.astype(np.float32, copy=True)
        cat = frame.category.copy()
    cutoff = spec.min_dbz if min_dbz is None else min_dbz
    if apply_dbz_floor:
        dbz, cat = threshold(dbz, cutoff, category=cat)
    if spec.despeckle and apply_despeckle:
        dbz, cat = remove_small_components(dbz, spec.min_component, category=cat)
        dbz, cat = despike_isolated(dbz, category=cat)
    if spec.smooth and edge_aware:
        dbz = edge_aware_smooth(dbz)
    elif spec.smooth:
        dbz = mild_smooth(dbz)
        if apply_dbz_floor:
            dbz, cat = threshold(dbz, cutoff, category=cat)
    return ReflectivityFrame(
        dbz=dbz,
        lat=frame.lat,
        lon=frame.lon,
        valid_time=frame.valid_time,
        product=frame.product,
        source=frame.source,
        category=cat,
    )


def threshold(
    dbz: np.ndarray,
    min_dbz: float,
    category: Optional[np.ndarray] = None,
):
    """Hide finite values below min_dbz as no-echo (not missing/no-coverage)."""
    out = dbz.astype(np.float32, copy=True)
    hide = np.isfinite(out) & (out < min_dbz)
    out[hide] = np.nan
    if category is None:
        return out
    cat = category.copy()
    # Clutter/floor hides remain "had coverage" — do not promote to missing.
    cat[hide & (cat == CAT_VALID)] = CAT_NO_ECHO
    return out, cat


def despike_isolated(
    dbz: np.ndarray, category: Optional[np.ndarray] = None
):
    """Drop echo pixels that have fewer than two echoing 8-neighbors."""
    valid = np.isfinite(dbz)
    if not np.any(valid):
        if category is None:
            return dbz
        return dbz, category
    neighbors = _neighbor_count(valid)
    keep = valid & (neighbors >= 2)
    out = dbz.copy()
    dropped = valid & ~keep
    out[dropped] = np.nan
    if category is None:
        return out
    cat = category.copy()
    cat[dropped & (cat == CAT_VALID)] = CAT_NO_ECHO
    return out, cat


def remove_small_components(
    dbz: np.ndarray, min_size: int, category: Optional[np.ndarray] = None
):
    """Remove 4-connected echo regions smaller than min_size pixels."""
    if min_size <= 1:
        if category is None:
            return dbz
        return dbz, category
    valid = np.isfinite(dbz)
    h, w = valid.shape
    seen = np.zeros((h, w), dtype=np.uint8)
    keep = np.zeros((h, w), dtype=bool)
    for j in range(h):
        for i in range(w):
            if not valid[j, i] or seen[j, i]:
                continue
            stack = [(j, i)]
            component = []
            seen[j, i] = 1
            while stack:
                y, x = stack.pop()
                component.append((y, x))
                for dy, dx in ((0, 1), (0, -1), (1, 0), (-1, 0)):
                    ny, nx = y + dy, x + dx
                    if 0 <= ny < h and 0 <= nx < w and valid[ny, nx] and not seen[ny, nx]:
                        seen[ny, nx] = 1
                        stack.append((ny, nx))
            if len(component) >= min_size:
                for y, x in component:
                    keep[y, x] = True
    out = dbz.copy()
    dropped = valid & ~keep
    out[dropped] = np.nan
    if category is None:
        return out
    cat = category.copy()
    cat[dropped & (cat == CAT_VALID)] = CAT_NO_ECHO
    return out, cat


def edge_aware_smooth(
    dbz: np.ndarray,
    space_sigma: float = 1.35,
    value_sigma: float = 6.5,
    radius: int = 3,
) -> np.ndarray:
    """Blend echo cells with similar neighbors. Never writes into clear air.

    Spatial weight falls off over about one MRMS cell. Value weight ignores
    neighbors more than ~10 dBZ away, so a magenta core stays a core instead
    of being averaged down into the surrounding rain. An isolated valid cell
    has no similar neighbor and is unchanged. NaN / no-echo cells stay NaN.
    """
    valid = np.isfinite(dbz)
    if not np.any(valid):
        return dbz
    src = np.where(valid, dbz, np.float32(0.0)).astype(np.float32)
    mask = valid.astype(np.float32)
    padded_src = np.pad(src, radius, mode="constant", constant_values=0)
    padded_mask = np.pad(mask, radius, mode="constant", constant_values=0)
    h, w = src.shape
    num = np.zeros((h, w), dtype=np.float32)
    den = np.zeros((h, w), dtype=np.float32)
    inv_s = 1.0 / (2.0 * space_sigma * space_sigma)
    inv_v = np.float32(1.0 / (2.0 * value_sigma * value_sigma))
    for dy in range(-radius, radius + 1):
        for dx in range(-radius, radius + 1):
            spatial = np.float32(math.exp(-(dy * dy + dx * dx) * inv_s))
            y0 = radius + dy
            x0 = radius + dx
            window = padded_src[y0 : y0 + h, x0 : x0 + w]
            m = padded_mask[y0 : y0 + h, x0 : x0 + w]
            diff = window - src
            vw = np.exp(-(diff * diff) * inv_v).astype(np.float32)
            weight = spatial * vw * m
            num += weight * window
            den += weight
    out = dbz.astype(np.float32, copy=True)
    ok = valid & (den > 1e-6)
    out[ok] = num[ok] / den[ok]
    return out


def mild_smooth(dbz: np.ndarray) -> np.ndarray:
    """3×3 weighted nan-mean that does not bloom into empty cells."""
    valid = np.isfinite(dbz)
    if not np.any(valid):
        return dbz
    weights = np.array([[1, 2, 1], [2, 4, 2], [1, 2, 1]], dtype=np.float32)
    filled = np.where(valid, dbz, 0.0).astype(np.float32)
    mask = valid.astype(np.float32)
    num = _convolve3(filled, weights)
    den = _convolve3(mask, weights)
    out = dbz.copy()
    ok = valid & (den > 0)
    out[ok] = num[ok] / den[ok]
    return out


def _convolve3(arr: np.ndarray, kernel: np.ndarray) -> np.ndarray:
    padded = np.pad(arr, 1, mode="constant", constant_values=0)
    acc = np.zeros_like(arr, dtype=np.float32)
    for dy in range(3):
        for dx in range(3):
            acc += kernel[dy, dx] * padded[dy : dy + arr.shape[0], dx : dx + arr.shape[1]]
    return acc


def _neighbor_count(mask: np.ndarray) -> np.ndarray:
    padded = np.pad(mask.astype(np.uint8), 1, mode="constant")
    h, w = mask.shape
    total = np.zeros((h, w), dtype=np.int16)
    for dy in (-1, 0, 1):
        for dx in (-1, 0, 1):
            if dy == 0 and dx == 0:
                continue
            total += padded[1 + dy : 1 + dy + h, 1 + dx : 1 + dx + w]
    return total
