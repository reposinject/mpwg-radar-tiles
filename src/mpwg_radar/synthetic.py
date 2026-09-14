"""Synthetic Central Texas reflectivity for tests and offline smoke runs."""

from __future__ import annotations

from datetime import datetime, timezone

import numpy as np

from mpwg_radar.geo import CENTRAL_TEXAS
from mpwg_radar.grib import ReflectivityFrame, mask_fill


def synthetic_central_texas(
    valid_time: datetime | None = None,
    seed: int = 202609,
) -> ReflectivityFrame:
    """0.01° grid over Central Texas with a squall + isolated speckle."""
    rng = np.random.default_rng(seed)
    bbox = CENTRAL_TEXAS
    d = 0.01
    lat = np.arange(bbox.north, bbox.south - d * 0.5, -d, dtype=np.float32)
    lon = np.arange(bbox.west, bbox.east + d * 0.5, d, dtype=np.float32)
    yy, xx = np.meshgrid(lat, lon, indexing="ij")

    dbz = np.full(yy.shape, np.nan, dtype=np.float32)

    # West–east squall south of Austin, light green → orange cores.
    band = np.exp(-((yy - 30.05) ** 2) / (2 * 0.18**2))
    along = 0.55 + 0.45 * np.sin((xx + 98.4) * 6.0)
    dbz_band = 18.0 + 28.0 * band * along
    dbz_band = np.where(band > 0.12, dbz_band, np.nan)

    # Intense core near San Marcos / I-35 (dark red / magenta).
    core = np.exp(-((yy - 29.88) ** 2 + (xx + 97.94) ** 2) / (2 * 0.08**2))
    dbz_core = np.where(core > 0.15, 48.0 + 28.0 * core, np.nan)

    # Austin metro light shower (barely tinted → light green).
    shower = np.exp(-((yy - 30.27) ** 2 + (xx + 97.74) ** 2) / (2 * 0.22**2))
    dbz_shower = np.where(shower > 0.25, 8.0 + 22.0 * shower, np.nan)

    # Faint returns (Standard/All). Clean mode drops these (<10 dBZ).
    faint = np.exp(-((yy - 31.15) ** 2 + (xx + 97.45) ** 2) / (2 * 0.16**2))
    dbz_faint = np.where(faint > 0.35, 6.0 + 5.0 * faint, np.nan)

    stacked = np.stack(
        [
            np.nan_to_num(dbz_band, nan=-999),
            np.nan_to_num(dbz_core, nan=-999),
            np.nan_to_num(dbz_shower, nan=-999),
            np.nan_to_num(dbz_faint, nan=-999),
        ]
    )
    dbz = np.max(stacked, axis=0)
    dbz[dbz < 0] = np.nan

    # Isolated 1-pixel speckles that Clean mode should remove.
    for _ in range(12):
        j = int(rng.integers(5, lat.size - 5))
        i = int(rng.integers(5, lon.size - 5))
        if not np.isfinite(dbz[j, i]):
            dbz[j, i] = 22.0

    return ReflectivityFrame(
        dbz=mask_fill(dbz),
        lat=lat,
        lon=lon,
        valid_time=valid_time or datetime(2026, 9, 14, 17, 12, tzinfo=timezone.utc),
        product="SyntheticReflectivity",
        source="synthetic",
    )
