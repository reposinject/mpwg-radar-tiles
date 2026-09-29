"""Offline dBZ interp stays off the production cook path."""

from __future__ import annotations

from pathlib import Path

import numpy as np

from mpwg_radar.dbz_interp_offline import (
    CANDIDATE_BICUBIC,
    CANDIDATE_BILINEAR,
    CANDIDATE_IDS,
    CANDIDATE_PEAK_HOLD,
    PRODUCTION_NAME,
    Z9_HALF_PIXEL_EW,
    Z9_HALF_PIXEL_NS,
    paint_candidate_tile,
    sample_dbz_candidate,
)
from mpwg_radar.geo import latlon_to_global_xy
from mpwg_radar.palette import RALA_PALETTE_VERSION, load_palette
from mpwg_radar.products import CAT_MISSING, CAT_NO_ECHO, CAT_VALID, get_product
from mpwg_radar.tiles import SPATIAL_REVISION, _DETAIL_CORE, render_tile
from tests.test_rala_render import _frame


def _peak_grid():
    """68 dBZ cell in a field of 30, with a clear row and one missing cell."""
    lat = np.arange(34.10, 33.90, -0.01, dtype=np.float64)
    lon = np.arange(-101.50, -101.30, 0.01, dtype=np.float64)
    dbz = np.full((lat.size, lon.size), 30.0, dtype=np.float32)
    cat = np.full(dbz.shape, CAT_VALID, dtype=np.uint8)
    dbz[10, 10] = 68.0
    dbz[0, :] = np.nan
    cat[0, :] = CAT_NO_ECHO
    dbz[5, 2] = np.nan
    cat[5, 2] = CAT_MISSING
    return lat, lon, dbz, cat


def _at(lat, lon, j, i, dj, di):
    dlat = float(lat[1] - lat[0])
    dlon = float(lon[1] - lon[0])
    qlat = np.array([[float(lat[j]) + dj * dlat]], dtype=np.float64)
    qlon = np.array([[float(lon[i]) + di * dlon]], dtype=np.float64)
    return qlat, qlon


def test_production_modules_do_not_import_the_offline_interp():
    root = Path(__file__).resolve().parents[1] / "src" / "mpwg_radar"
    for name in (
        "cooker.py",
        "tiles.py",
        "products.py",
        "palette.py",
        "cli.py",
        "qc.py",
        "grib.py",
        "config.py",
    ):
        text = (root / name).read_text()
        assert "dbz_interp_offline" not in text, name
    assert SPATIAL_REVISION == "p3l"
    assert abs(float(_DETAIL_CORE) - 0.28) < 1e-6
    assert RALA_PALETTE_VERSION == "2026-09-rala-p3k"
    assert get_product("rala").sample_mode == "masked-splat"
    assert PRODUCTION_NAME == "p3l"
    assert "p3l" not in CANDIDATE_IDS


def test_candidates_keep_cell_centers_and_refuse_clear_air():
    lat, lon, dbz, cat = _peak_grid()
    qlat, qlon = _at(lat, lon, 10, 10, 0.0, 0.0)
    clear_lat, clear_lon = _at(lat, lon, 0, 10, 0.0, 0.0)
    miss_lat, miss_lon = _at(lat, lon, 5, 2, 0.0, 0.0)
    for name in CANDIDATE_IDS:
        sampled, got = sample_dbz_candidate(name, dbz, lat, lon, qlat, qlon, cat)
        assert got[0, 0] == CAT_VALID
        assert abs(float(sampled[0, 0]) - 68.0) < 1e-3
        sampled_c, cat_c = sample_dbz_candidate(
            name, dbz, lat, lon, clear_lat, clear_lon, cat
        )
        assert cat_c[0, 0] == CAT_NO_ECHO
        assert np.isnan(sampled_c[0, 0])
        sampled_m, cat_m = sample_dbz_candidate(
            name, dbz, lat, lon, miss_lat, miss_lon, cat
        )
        assert cat_m[0, 0] == CAT_MISSING
        assert np.isnan(sampled_m[0, 0])


def test_sharp_peak_half_pixel_stays_hot_only_when_held():
    """Worst-case z9 pixel center at Dickinson is ~0.137 cell off the source.

    Bilinear cools a 68-in-30 peak well below 65. Clipped bicubic stays
    inside the local range and cools less, but a 65 dBZ peak in the same
    rain drops under 65. Peak-hold stays on the p3l value inside the
    0.28-cell core.
    """
    lat, lon, dbz, cat = _peak_grid()
    qlat, qlon = _at(lat, lon, 10, 10, Z9_HALF_PIXEL_NS, Z9_HALF_PIXEL_EW)
    bilinear, _ = sample_dbz_candidate(
        CANDIDATE_BILINEAR, dbz, lat, lon, qlat, qlon, cat
    )
    cubic, _ = sample_dbz_candidate(
        CANDIDATE_BICUBIC, dbz, lat, lon, qlat, qlon, cat
    )
    held, _ = sample_dbz_candidate(
        CANDIDATE_PEAK_HOLD, dbz, lat, lon, qlat, qlon, cat
    )
    assert float(bilinear[0, 0]) < 62.0
    # Clipped to the local source range, cooler than the cell, not pinned.
    assert 30.0 <= float(cubic[0, 0]) <= 68.0 + 1e-3
    assert float(cubic[0, 0]) < 67.0
    assert abs(float(held[0, 0]) - 68.0) < 1e-3

    # A cell that only just clears magenta, in the same cold rain, drops
    # below 65 for both interpolators. Peak-hold keeps the source dBZ.
    dbz65 = np.array(dbz, copy=True)
    dbz65[10, 10] = 65.0
    bilin65, _ = sample_dbz_candidate(
        CANDIDATE_BILINEAR, dbz65, lat, lon, qlat, qlon, cat
    )
    cubic65, _ = sample_dbz_candidate(
        CANDIDATE_BICUBIC, dbz65, lat, lon, qlat, qlon, cat
    )
    held65, _ = sample_dbz_candidate(
        CANDIDATE_PEAK_HOLD, dbz65, lat, lon, qlat, qlon, cat
    )
    assert float(bilin65[0, 0]) < 65.0
    assert float(cubic65[0, 0]) < 65.0
    assert abs(float(held65[0, 0]) - 65.0) < 1e-3


def test_ordinary_slope_cell_is_bilinear_not_a_plateau():
    lat = np.arange(34.10, 33.90, -0.01, dtype=np.float64)
    lon = np.arange(-101.50, -101.30, 0.01, dtype=np.float64)
    dbz = np.full((lat.size, lon.size), 30.0, dtype=np.float32)
    cat = np.full(dbz.shape, CAT_VALID, dtype=np.uint8)
    dbz[8, 8:13] = np.array([10, 20, 30, 40, 50], dtype=np.float32)
    # +0.20 cell from the 30 toward the 40. Inside the p3l core, outside a
    # pure center sample. The 30 cell is not a local maximum.
    qlat, qlon = _at(lat, lon, 8, 10, 0.0, 0.20)
    bilinear, _ = sample_dbz_candidate(
        CANDIDATE_BILINEAR, dbz, lat, lon, qlat, qlon, cat
    )
    held, _ = sample_dbz_candidate(
        CANDIDATE_PEAK_HOLD, dbz, lat, lon, qlat, qlon, cat
    )
    assert abs(float(bilinear[0, 0]) - 32.0) < 0.15
    assert abs(float(held[0, 0]) - float(bilinear[0, 0])) < 1e-3
    # The local max at the top of the ramp still holds the source value
    # inside the core (50, with a colder west neighbor and 30s around).
    qlat_p, qlon_p = _at(lat, lon, 8, 12, 0.0, -0.20)
    peak, _ = sample_dbz_candidate(
        CANDIDATE_PEAK_HOLD, dbz, lat, lon, qlat_p, qlon_p, cat
    )
    assert abs(float(peak[0, 0]) - 50.0) < 1e-3


def test_bicubic_cannot_leave_the_local_source_range():
    lat = np.arange(34.20, 33.80, -0.01, dtype=np.float64)
    lon = np.arange(-101.60, -101.20, 0.01, dtype=np.float64)
    dbz = np.zeros((lat.size, lon.size), dtype=np.float32)
    cat = np.full(dbz.shape, CAT_VALID, dtype=np.uint8)
    dbz[20, 20] = 100.0
    offsets = np.linspace(-0.45, 0.45, 7)
    du, dv = np.meshgrid(offsets, offsets)
    dlat = float(lat[1] - lat[0])
    dlon = float(lon[1] - lon[0])
    qlat = float(lat[20]) + dv * dlat
    qlon = float(lon[20]) + du * dlon
    sampled, got = sample_dbz_candidate(
        CANDIDATE_BICUBIC, dbz, lat, lon, qlat, qlon, cat
    )
    assert np.all(got == CAT_VALID)
    finite = sampled[np.isfinite(sampled)]
    assert finite.size
    assert float(np.min(finite)) >= -1e-3
    assert float(np.max(finite)) <= 100.0 + 1e-3
    center = sample_dbz_candidate(
        CANDIDATE_BICUBIC,
        dbz,
        lat,
        lon,
        np.array([[float(lat[20])]]),
        np.array([[float(lon[20])]]),
        cat,
    )[0]
    assert abs(float(center[0, 0]) - 100.0) < 1e-3


def test_p3l_paint_matches_production_render_tile():
    lat, lon, dbz, cat = _peak_grid()
    frame = _frame(dbz, lat, lon, cat)
    pal = load_palette("mpwg-rala-2026-09")
    assert pal.version == "2026-09-rala-p3k"
    gx, gy = latlon_to_global_xy(float(lon[10]), float(lat[10]), 9)
    image = paint_candidate_tile(frame, pal, 9, int(gx), int(gy), PRODUCTION_NAME)
    production, _echo = render_tile(
        frame, pal, 9, int(gx), int(gy), sample_mode="masked-splat"
    )
    assert np.array_equal(np.asarray(image), np.asarray(production))
