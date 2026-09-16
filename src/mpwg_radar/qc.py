"""QC / cleanup on the physical dBZ grid (before colorization).

This is not the Clean display cutoff. Tiles hide reflectivity below 15 dBZ
in the palette (display-only); the float32 dBZ crop is written before this
module runs. Clean mode still drops sub-10 dBZ clutter from the *mode* grid
used for despeckle/smooth.

Modes
-----
clean     default: drop < ~10 dBZ clutter, despeckle, mild 3×3 smooth
standard  scaffold: hide < ~5 dBZ, no despeckle/smooth
all       scaffold: hide fill only (still masks -99/-999 no-coverage)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict

import numpy as np

from mpwg_radar.grib import ReflectivityFrame, mask_fill


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


def apply_mode(frame: ReflectivityFrame, mode: str) -> ReflectivityFrame:
    spec = MODES[mode]
    dbz = mask_fill(frame.dbz)
    dbz = threshold(dbz, spec.min_dbz)
    if spec.despeckle:
        dbz = remove_small_components(dbz, spec.min_component)
        dbz = despike_isolated(dbz)
    if spec.smooth:
        dbz = mild_smooth(dbz)
        dbz = threshold(dbz, spec.min_dbz)
    return ReflectivityFrame(
        dbz=dbz,
        lat=frame.lat,
        lon=frame.lon,
        valid_time=frame.valid_time,
        product=frame.product,
        source=frame.source,
    )


def threshold(dbz: np.ndarray, min_dbz: float) -> np.ndarray:
    out = dbz.astype(np.float32, copy=True)
    out[np.isfinite(out) & (out < min_dbz)] = np.nan
    return out


def despike_isolated(dbz: np.ndarray) -> np.ndarray:
    """Drop echo pixels that have fewer than two echoing 8-neighbors."""
    valid = np.isfinite(dbz)
    if not np.any(valid):
        return dbz
    neighbors = _neighbor_count(valid)
    keep = valid & (neighbors >= 2)
    out = dbz.copy()
    out[~keep] = np.nan
    return out


def remove_small_components(dbz: np.ndarray, min_size: int) -> np.ndarray:
    """Remove 4-connected echo regions smaller than min_size pixels."""
    if min_size <= 1:
        return dbz
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
    out[~keep] = np.nan
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
