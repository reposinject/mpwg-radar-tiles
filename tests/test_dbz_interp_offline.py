"""Offline dBZ interp stays off the production cook path."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from mpwg_radar.config import (
    RALA_DBZ_INTERP_BILINEAR_PEAK_HOLD,
    RALA_DBZ_INTERP_MONOTONE_PCHIP,
    RALA_DBZ_INTERP_TIGHT_PEAK_HOLD,
    load_config,
    normalize_rala_dbz_interp,
)
from mpwg_radar.dbz_interp_offline import (
    CANDIDATE_BICUBIC,
    CANDIDATE_BILINEAR,
    CANDIDATE_IDS,
    CANDIDATE_BOUNDED_CUBIC,
    CANDIDATE_MONOTONE,
    CANDIDATE_PEAK_HOLD,
    CANDIDATE_TIGHT,
    PRODUCTION_NAME,
    TIGHT_BIAS_POWER,
    Z9_HALF_PIXEL_EW,
    Z9_HALF_PIXEL_NS,
    paint_candidate_tile,
    sample_bilinear_peak_hold,
    sample_dbz_candidate,
    sample_localized_bilinear,
    sample_monotone_pchip,
    sample_review_dbz,
    width_10_90,
)
from mpwg_radar.geo import latlon_to_global_xy
from mpwg_radar.palette import RALA_PALETTE_VERSION, load_palette
from mpwg_radar.products import CAT_MISSING, CAT_NO_ECHO, CAT_VALID, get_product
from mpwg_radar.tiles import (
    SPATIAL_REVISION,
    _DETAIL_CORE,
    render_tile,
    sample_masked_bilinear,
    sample_masked_splat,
)
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


def test_production_stamps_stay_p3l_and_the_flag_defaults_off():
    """The review sampler is opt-in. Stamps and the default call stay p3l."""
    assert SPATIAL_REVISION == "p3l"
    assert abs(float(_DETAIL_CORE) - 0.28) < 1e-6
    assert RALA_PALETTE_VERSION == "2026-09-rala-p3k"
    assert get_product("rala").sample_mode == "masked-splat"
    assert PRODUCTION_NAME == "p3l"
    assert "p3l" not in CANDIDATE_IDS
    assert normalize_rala_dbz_interp(None) == ""
    assert normalize_rala_dbz_interp("") == ""
    assert normalize_rala_dbz_interp("off") == ""
    assert normalize_rala_dbz_interp("p3l") == ""
    assert normalize_rala_dbz_interp("bilinear_peak_hold") == RALA_DBZ_INTERP_BILINEAR_PEAK_HOLD
    assert normalize_rala_dbz_interp("bilinear-peak-hold") == RALA_DBZ_INTERP_BILINEAR_PEAK_HOLD
    assert normalize_rala_dbz_interp("tight_peak_hold") == RALA_DBZ_INTERP_TIGHT_PEAK_HOLD
    assert normalize_rala_dbz_interp("tight-peak-hold") == RALA_DBZ_INTERP_TIGHT_PEAK_HOLD
    assert normalize_rala_dbz_interp("monotone_pchip") == RALA_DBZ_INTERP_MONOTONE_PCHIP
    assert normalize_rala_dbz_interp("monotone-pchip") == RALA_DBZ_INTERP_MONOTONE_PCHIP
    assert TIGHT_BIAS_POWER == 2.0
    import inspect

    assert inspect.signature(render_tile).parameters["dbz_interp"].default == ""


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
    tight65, _ = sample_dbz_candidate(
        CANDIDATE_TIGHT, dbz65, lat, lon, qlat, qlon, cat
    )
    assert float(bilin65[0, 0]) < 65.0
    assert float(cubic65[0, 0]) < 65.0
    assert abs(float(held65[0, 0]) - 65.0) < 1e-3
    assert abs(float(tight65[0, 0]) - 65.0) < 1e-3


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


def test_flag_off_matches_p3l_numerical_field_and_paint(monkeypatch):
    """Omitting the flag is the production splat, not a second implementation."""
    lat, lon, dbz, cat = _peak_grid()
    frame = _frame(dbz, lat, lon, cat)
    pal = load_palette("mpwg-rala-2026-09")
    gx, gy = latlon_to_global_xy(float(lon[10]), float(lat[10]), 9)
    z, x, y = 9, int(gx), int(gy)
    qlon, qlat = _query_for_tile(z, x, y)
    captured = {}

    def _capture(src, src_lat, src_lon, query_lat, query_lon, category=None):
        out = sample_masked_splat(src, src_lat, src_lon, query_lat, query_lon, category)
        captured["out"] = out
        return out

    def _refuse_review(*_args, **_kwargs):
        raise AssertionError("flag off must not call sample_bilinear_peak_hold")

    monkeypatch.setattr("mpwg_radar.tiles.sample_masked_splat", _capture)
    monkeypatch.setattr(
        "mpwg_radar.dbz_interp_offline.sample_bilinear_peak_hold", _refuse_review
    )
    default_img, _echo = render_tile(frame, pal, z, x, y, sample_mode="masked-splat")
    splat, splat_cat, _edge = captured["out"]
    direct, direct_cat, _direct_edge = sample_masked_splat(dbz, lat, lon, qlat, qlon, cat)
    assert np.allclose(splat, direct, rtol=0, atol=1e-5, equal_nan=True)
    assert np.array_equal(splat_cat, direct_cat)
    explicit_off, _echo_off = render_tile(
        frame, pal, z, x, y, sample_mode="masked-splat", dbz_interp=""
    )
    assert np.array_equal(np.asarray(default_img), np.asarray(explicit_off))
    # A slope sample inside the p3l core stays on the source cell when the flag is off.
    qlat_s, qlon_s = _at(lat, lon, 10, 10, 0.0, 0.20)
    held_off, _cat_off, _edge_off = sample_masked_splat(dbz, lat, lon, qlat_s, qlon_s, cat)
    assert abs(float(held_off[0, 0]) - 68.0) < 1e-3


def _query_for_tile(z, x, y, tile_size=512):
    from mpwg_radar.tiles import _query_lonlat

    return _query_lonlat(z, x, y, tile_size)


def test_flag_on_keeps_peaks_and_does_not_paint_clear_air():
    """bilinear_peak_hold on the same 68-in-30 frame used for the offline A/B."""
    lat, lon, dbz, cat = _peak_grid()
    frame = _frame(dbz, lat, lon, cat)
    pal = load_palette("mpwg-rala-2026-09")
    assert pal.version == "2026-09-rala-p3k"
    qlat, qlon = _at(lat, lon, 10, 10, 0.0, 0.0)
    center, center_cat = sample_bilinear_peak_hold(dbz, lat, lon, qlat, qlon, cat)
    assert center_cat[0, 0] == CAT_VALID
    assert abs(float(center[0, 0]) - 68.0) < 1e-3
    half_lat, half_lon = _at(lat, lon, 10, 10, Z9_HALF_PIXEL_NS, Z9_HALF_PIXEL_EW)
    half, _half_cat = sample_bilinear_peak_hold(dbz, lat, lon, half_lat, half_lon, cat)
    assert abs(float(half[0, 0]) - 68.0) < 1e-3
    clear_lat, clear_lon = _at(lat, lon, 0, 10, 0.0, 0.0)
    clear, clear_cat = sample_bilinear_peak_hold(dbz, lat, lon, clear_lat, clear_lon, cat)
    assert clear_cat[0, 0] == CAT_NO_ECHO
    assert np.isnan(clear[0, 0])
    miss_lat, miss_lon = _at(lat, lon, 5, 2, 0.0, 0.0)
    missing, miss_cat = sample_bilinear_peak_hold(dbz, lat, lon, miss_lat, miss_lon, cat)
    assert miss_cat[0, 0] == CAT_MISSING
    assert np.isnan(missing[0, 0])

    gx, gy = latlon_to_global_xy(float(lon[10]), float(lat[10]), 9)
    flagged = render_tile(
        frame,
        pal,
        9,
        int(gx),
        int(gy),
        sample_mode="masked-splat",
        dbz_interp=RALA_DBZ_INTERP_BILINEAR_PEAK_HOLD,
    )
    offline = paint_candidate_tile(frame, pal, 9, int(gx), int(gy), CANDIDATE_PEAK_HOLD)
    assert np.array_equal(np.asarray(flagged[0]), np.asarray(offline))
    # Clear-air pixels in that tile stay transparent. The flag does not invent echo.
    rgba = np.asarray(flagged[0])
    production, _echo = render_tile(frame, pal, 9, int(gx), int(gy), sample_mode="masked-splat")
    prod = np.asarray(production)
    clear_px = prod[..., 3] == 0
    assert np.all(rgba[clear_px, 3] == 0)


def _line_between(lat, lon, j0, i0, j1, i1, n=401):
    qlat = np.linspace(float(lat[j0]), float(lat[j1]), n, dtype=np.float64)[:, None]
    qlon = np.linspace(float(lon[i0]), float(lon[i1]), n, dtype=np.float64)[:, None]
    return qlat, qlon


def test_tight_ramp_is_narrower_and_keeps_the_real_shoulder():
    """B is a shorter 10–90% ramp than A, still exact at centers, no overshoot.

    The 68 cell is a local max, so both A and B hold it. The east neighbor is
    ordinary 30 dBZ. B must not lift that neighbor's interior the way plain
    bilinear does, and the shared face stays between the two source values.
    """
    lat = np.arange(34.10, 33.90, -0.01, dtype=np.float64)
    lon = np.arange(-101.50, -101.30, 0.01, dtype=np.float64)
    dbz = np.full((lat.size, lon.size), 30.0, dtype=np.float32)
    cat = np.full(dbz.shape, CAT_VALID, dtype=np.uint8)
    dbz[8, 8:13] = np.array([10, 20, 30, 40, 50], dtype=np.float32)
    qlat, qlon = _line_between(lat, lon, 8, 10, 8, 11)
    wide, wide_cat = sample_dbz_candidate(
        CANDIDATE_PEAK_HOLD, dbz, lat, lon, qlat, qlon, cat
    )
    tight, tight_cat = sample_dbz_candidate(
        CANDIDATE_TIGHT, dbz, lat, lon, qlat, qlon, cat
    )
    assert np.all(wide_cat == CAT_VALID)
    assert np.all(tight_cat == CAT_VALID)
    wide_w = width_10_90(wide)
    tight_w = width_10_90(tight)
    assert 0.75 <= wide_w <= 0.85
    assert 0.45 <= tight_w <= 0.55
    assert tight_w < wide_w - 0.20
    assert abs(float(tight[0, 0]) - 30.0) < 1e-3
    assert abs(float(tight[-1, 0]) - 40.0) < 1e-3
    assert float(np.min(tight)) >= 30.0 - 1e-3
    assert float(np.max(tight)) <= 40.0 + 1e-3

    same_power, _, _ = sample_localized_bilinear(
        dbz, lat, lon, qlat, qlon, cat, power=1.0
    )
    plain, _, _ = sample_masked_bilinear(dbz, lat, lon, qlat, qlon, cat)
    assert np.allclose(same_power, plain, rtol=0, atol=1e-5, equal_nan=True)

    # Shoulder of the 68-in-30 peak: 0.20 cell inside the 30, toward the peak.
    lat, lon, dbz, cat = _peak_grid()
    q_shoulder = _at(lat, lon, 10, 11, 0.0, -0.20)
    a_sh, _ = sample_dbz_candidate(CANDIDATE_PEAK_HOLD, dbz, lat, lon, *q_shoulder, cat)
    b_sh, _ = sample_dbz_candidate(CANDIDATE_TIGHT, dbz, lat, lon, *q_shoulder, cat)
    assert 30.0 < float(b_sh[0, 0]) < float(a_sh[0, 0]) < 68.0
    assert float(a_sh[0, 0]) - float(b_sh[0, 0]) > 3.0
    # Just inside the neighbor, the face is still a blend of 68 and 30.
    q_face = _at(lat, lon, 10, 10, 0.0, 0.52)
    a_face, _ = sample_dbz_candidate(CANDIDATE_PEAK_HOLD, dbz, lat, lon, *q_face, cat)
    b_face, _ = sample_dbz_candidate(CANDIDATE_TIGHT, dbz, lat, lon, *q_face, cat)
    for value in (float(a_face[0, 0]), float(b_face[0, 0])):
        assert 30.0 < value < 68.0
    half_lat, half_lon = _at(lat, lon, 10, 10, Z9_HALF_PIXEL_NS, Z9_HALF_PIXEL_EW)
    half_b, half_cat = sample_dbz_candidate(
        CANDIDATE_TIGHT, dbz, lat, lon, half_lat, half_lon, cat
    )
    assert half_cat[0, 0] == CAT_VALID
    assert abs(float(half_b[0, 0]) - 68.0) < 1e-3


def test_flag_tight_matches_candidate_and_leaves_clear_air(monkeypatch):
    lat, lon, dbz, cat = _peak_grid()
    frame = _frame(dbz, lat, lon, cat)
    pal = load_palette("mpwg-rala-2026-09")
    gx, gy = latlon_to_global_xy(float(lon[10]), float(lat[10]), 9)
    z, x, y = 9, int(gx), int(gy)
    flagged = render_tile(
        frame,
        pal,
        z,
        x,
        y,
        sample_mode="masked-splat",
        dbz_interp=RALA_DBZ_INTERP_TIGHT_PEAK_HOLD,
    )
    offline = paint_candidate_tile(frame, pal, z, x, y, CANDIDATE_TIGHT)
    assert np.array_equal(np.asarray(flagged[0]), np.asarray(offline))
    production, _echo = render_tile(frame, pal, z, x, y, sample_mode="masked-splat")
    prod = np.asarray(production)
    rgba = np.asarray(flagged[0])
    assert np.all(rgba[prod[..., 3] == 0, 3] == 0)
    monkeypatch.setattr("mpwg_radar.config.load_dotenv", lambda path=None: None)
    monkeypatch.setenv("MPWG_PRODUCT", "rala")
    monkeypatch.setenv("MPWG_RALA_DBZ_INTERP", "tight_peak_hold")
    assert load_config().rala_dbz_interp == "tight_peak_hold"


def test_bounded_cubic_and_monotone_hit_centers_without_a_flat_stamp():
    """C and D are harness samplers. Centers match. They do not hold a p3l core."""
    lat, lon, dbz, cat = _peak_grid()
    q_center = _at(lat, lon, 10, 10, 0.0, 0.0)
    q_off = _at(lat, lon, 10, 10, 0.0, 0.14)
    q_clear = _at(lat, lon, 0, 10, 0.0, 0.0)
    q_miss = _at(lat, lon, 5, 2, 0.0, 0.0)
    held, _ = sample_dbz_candidate(CANDIDATE_PEAK_HOLD, dbz, lat, lon, *q_off, cat)
    assert abs(float(held[0, 0]) - 68.0) < 1e-3
    for name in (CANDIDATE_BOUNDED_CUBIC, CANDIDATE_MONOTONE):
        center, center_cat = sample_dbz_candidate(name, dbz, lat, lon, *q_center, cat)
        assert center_cat[0, 0] == CAT_VALID
        assert abs(float(center[0, 0]) - 68.0) < 1e-3
        off, _ = sample_dbz_candidate(name, dbz, lat, lon, *q_off, cat)
        # Inside the p3l core, A is still the source. C and D have already left it.
        assert float(off[0, 0]) < 67.5
        assert float(off[0, 0]) > 60.0
        clear, clear_cat = sample_dbz_candidate(name, dbz, lat, lon, *q_clear, cat)
        assert clear_cat[0, 0] == CAT_NO_ECHO
        assert np.isnan(clear[0, 0])
        missing, miss_cat = sample_dbz_candidate(name, dbz, lat, lon, *q_miss, cat)
        assert miss_cat[0, 0] == CAT_MISSING
        assert np.isnan(missing[0, 0])
        qlat, qlon = _line_between(lat, lon, 10, 10, 10, 11)
        line, line_cat = sample_dbz_candidate(name, dbz, lat, lon, qlat, qlon, cat)
        assert np.all(line_cat == CAT_VALID)
        assert float(np.nanmin(line)) >= 30.0 - 1e-2
        assert float(np.nanmax(line)) <= 68.0 + 1e-2
    dbz65 = np.array(dbz, copy=True)
    dbz65[10, 10] = 65.0
    for name in (CANDIDATE_BOUNDED_CUBIC, CANDIDATE_MONOTONE):
        center, _ = sample_dbz_candidate(name, dbz65, lat, lon, *q_center, cat)
        assert abs(float(center[0, 0]) - 65.0) < 1e-3


def test_rala_dbz_interp_env_defaults_off(monkeypatch):
    monkeypatch.setattr("mpwg_radar.config.load_dotenv", lambda path=None: None)
    monkeypatch.delenv("MPWG_RALA_DBZ_INTERP", raising=False)
    monkeypatch.setenv("MPWG_PRODUCT", "rala")
    cfg = load_config()
    assert cfg.rala_dbz_interp == ""
    assert cfg.product.sample_mode == "masked-splat"
    monkeypatch.setenv("MPWG_RALA_DBZ_INTERP", "bilinear_peak_hold")
    enabled = load_config()
    assert enabled.rala_dbz_interp == "bilinear_peak_hold"
    monkeypatch.setenv("MPWG_RALA_DBZ_INTERP", "gaussian")
    try:
        load_config()
    except ValueError as exc:
        assert "bilinear_peak_hold" in str(exc)
    else:
        raise AssertionError("unknown interp value must fail the cook config")


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


def test_review_cook_keeps_p3l_stamp_and_marks_the_flag(tmp_path: Path):
    from mpwg_radar.config import CookerConfig
    from mpwg_radar.cooker import cook
    from mpwg_radar.geo import CENTRAL_TEXAS

    cfg = CookerConfig(
        bbox=CENTRAL_TEXAS,
        region_name="central-texas",
        modes=["clean"],
        min_zoom=6,
        max_zoom=6,
        tile_size=512,
        skip_empty_tiles=True,
        keep_dbz=False,
        data_dir=tmp_path / "data",
        output_dir=tmp_path,
        upload=False,
        product_id="rala",
        rala_dbz_interp="bilinear_peak_hold",
    )
    cook(cfg, source="synthetic", upload=False)
    frame = json.loads(
        (tmp_path / "radar" / "rala" / "clean" / "latest" / "frame.json").read_text()
    )
    assert frame["mode_spec"]["spatial"] == "p3l"
    assert frame["mode_spec"]["sample"] == "masked-splat"
    assert frame["mode_spec"]["dbz_interp"] == "bilinear_peak_hold"
    assert frame["palette"]["version"] == "2026-09-rala-p3k"
    assert frame["max_zoom"] == 6


def test_monotone_pchip_review_flag_uses_the_harness_sampler(monkeypatch):
    """D on the cooker flag is sample_monotone_pchip, with no peak-hold core."""
    lat, lon, dbz, cat = _peak_grid()
    q_center = _at(lat, lon, 10, 10, 0.0, 0.0)
    q_off = _at(lat, lon, 10, 10, 0.0, 0.14)
    q_clear = _at(lat, lon, 0, 10, 0.0, 0.0)
    calls = {"n": 0}
    real = sample_monotone_pchip

    def _wrapped(*args, **kwargs):
        calls["n"] += 1
        return real(*args, **kwargs)

    monkeypatch.setattr(
        "mpwg_radar.dbz_interp_offline.sample_monotone_pchip", _wrapped
    )
    flagged, flagged_cat = sample_review_dbz(
        "monotone_pchip", dbz, lat, lon, *q_center, cat
    )
    direct, direct_cat = real(dbz, lat, lon, *q_center, cat)
    assert calls["n"] >= 1
    assert np.allclose(flagged, direct, equal_nan=True)
    assert np.array_equal(flagged_cat, direct_cat)
    assert abs(float(flagged[0, 0]) - 68.0) < 1e-3
    off, _ = sample_review_dbz("monotone-pchip", dbz, lat, lon, *q_off, cat)
    assert float(off[0, 0]) < 67.5
    held, _ = sample_dbz_candidate(CANDIDATE_PEAK_HOLD, dbz, lat, lon, *q_off, cat)
    assert abs(float(held[0, 0]) - 68.0) < 1e-3
    clear, clear_cat = sample_review_dbz(
        RALA_DBZ_INTERP_MONOTONE_PCHIP, dbz, lat, lon, *q_clear, cat
    )
    assert clear_cat[0, 0] == CAT_NO_ECHO
    assert np.isnan(clear[0, 0])
    frame = _frame(dbz, lat, lon, cat)
    pal = load_palette("mpwg-rala-2026-09")
    gx, gy = latlon_to_global_xy(float(lon[10]), float(lat[10]), 9)
    z, x, y = 9, int(gx), int(gy)
    image, _echo = render_tile(
        frame,
        pal,
        z,
        x,
        y,
        sample_mode="masked-splat",
        dbz_interp=RALA_DBZ_INTERP_MONOTONE_PCHIP,
    )
    production, _ = render_tile(frame, pal, z, x, y, sample_mode="masked-splat")
    rgba = np.asarray(image)
    prod = np.asarray(production)
    assert np.all(rgba[prod[..., 3] == 0, 3] == 0)
    assert calls["n"] >= 2


def test_monotone_pchip_cook_lands_on_the_review_prefix(tmp_path: Path):
    """D tiles are not written onto the consumer rala/clean/latest tree."""
    from mpwg_radar.config import CookerConfig
    from mpwg_radar.cooker import cook
    from mpwg_radar.geo import CENTRAL_TEXAS

    cfg = CookerConfig(
        bbox=CENTRAL_TEXAS,
        region_name="central-texas",
        modes=["clean"],
        min_zoom=6,
        max_zoom=6,
        tile_size=512,
        skip_empty_tiles=True,
        keep_dbz=False,
        data_dir=tmp_path / "data",
        output_dir=tmp_path,
        upload=False,
        product_id="rala",
        rala_dbz_interp="monotone_pchip",
    )
    cook(cfg, source="synthetic", upload=False)
    review = (
        tmp_path
        / "radar"
        / "rala-review"
        / "monotone_pchip"
        / "clean"
        / "latest"
        / "frame.json"
    )
    frame = json.loads(review.read_text())
    assert frame["mode_spec"]["spatial"] == "p3l"
    assert frame["mode_spec"]["sample"] == "masked-splat"
    assert frame["mode_spec"]["dbz_interp"] == "monotone_pchip"
    assert frame["palette"]["version"] == "2026-09-rala-p3k"
    assert frame["tile_url_template"] == (
        "rala-review/monotone_pchip/clean/latest/{z}/{x}/{y}.png"
    )
    assert not (
        tmp_path / "radar" / "rala" / "clean" / "latest" / "frame.json"
    ).exists()
    manifest = json.loads((tmp_path / "radar" / "manifest.json").read_text())
    rala = manifest["products"]["rala"]
    assert rala["latest"] == "rala/clean/latest/{z}/{x}/{y}.png"
    assert rala["review"]["label"] == "NOT PRODUCTION"
    assert rala["review"]["dbz_interp"] == "monotone_pchip"
    assert rala["review"]["latest"] == (
        "rala-review/monotone_pchip/clean/latest/{z}/{x}/{y}.png"
    )
    assert rala["review"]["consumer_latest"] == "rala/clean/latest/{z}/{x}/{y}.png"
    assert isinstance(rala["review"]["frames"], list)
    assert not (tmp_path / "status-rala.json").exists()
    assert (tmp_path / "status-rala-review-monotone_pchip.json").is_file()


def test_review_frames_match_the_clean_frame_shape(tmp_path: Path):
    """products.rala.review.frames lists recent D frames; consumer latest stays."""
    from datetime import datetime, timedelta, timezone

    from mpwg_radar.config import CookerConfig
    from mpwg_radar.cooker import _product_for_publish, _write_manifest
    from mpwg_radar.geo import CENTRAL_TEXAS
    from mpwg_radar.grib import ReflectivityFrame, frame_id_for

    now = datetime.now(timezone.utc).replace(microsecond=0)
    times = [now - timedelta(minutes=4), now - timedelta(minutes=2), now]
    cfg = CookerConfig(
        bbox=CENTRAL_TEXAS,
        region_name="central-texas",
        modes=["clean"],
        min_zoom=6,
        max_zoom=6,
        output_dir=tmp_path,
        data_dir=tmp_path / "data",
        upload=False,
        product_id="rala",
        rala_dbz_interp="monotone_pchip",
        rala_review_frames=8,
    )
    product = _product_for_publish(cfg)
    radar = tmp_path / "radar"
    mode_dir = product.mode_dir(radar, "clean")
    # Older than the 30-minute review window. Must not enter the loop.
    stale = now - timedelta(hours=2)
    for when in list(times) + [stale]:
        fid = frame_id_for(when)
        dest = mode_dir / fid
        dest.mkdir(parents=True)
        (dest / "frame.json").write_text(
            json.dumps({"id": fid, "valid_time": when.isoformat(), "mode": "clean"})
        )
    # A consumer frame that a D manifest rewrite must not retarget.
    consumer = {
        "products": {
            "rala": {
                "latest": "rala/clean/latest/{z}/{x}/{y}.png",
                "latest_frame": "20260929T000000Z",
                "modes": {
                    "clean": {
                        "latest": "rala/clean/latest/{z}/{x}/{y}.png",
                        "latest_frame": "20260929T000000Z",
                        "frames": [
                            {
                                "id": "20260929T000000Z",
                                "valid_time": "2026-09-29T00:00:00+00:00",
                                "tiles": "rala/clean/20260929T000000Z/{z}/{x}/{y}.png",
                            }
                        ],
                    }
                },
            }
        }
    }
    (radar / "manifest.json").write_text(json.dumps(consumer))
    dbz = np.zeros((2, 2), dtype=np.float32)
    frame = ReflectivityFrame(
        dbz=dbz,
        lat=np.array([30.0, 30.01], dtype=np.float64),
        lon=np.array([-97.0, -96.99], dtype=np.float64),
        valid_time=now,
        product="ReflectivityAtLowestAltitude",
        category=np.ones((2, 2), dtype=np.uint8),
    )
    palette = load_palette("mpwg-rala-2026-09")
    manifest = _write_manifest(
        cfg,
        palette,
        radar,
        frame,
        [{"id": frame.frame_id, "mode": "clean"}],
        product,
        promoted=True,
    )
    review = manifest["products"]["rala"]["review"]
    assert review["label"] == "NOT PRODUCTION"
    assert review["latest_frame"]
    assert review["frame_json"].endswith("/latest/frame.json")
    assert review["consumer_latest"] == "rala/clean/latest/{z}/{x}/{y}.png"
    frames = review["frames"]
    assert [item["id"] for item in frames] == [frame_id_for(when) for when in reversed(times)]
    for item in frames:
        assert set(item) >= {"id", "valid_time", "tiles", "frame"}
        assert item["tiles"] == (
            f"rala-review/monotone_pchip/clean/{item['id']}/{{z}}/{{x}}/{{y}}.png"
        )
        assert item["frame"] == (
            f"rala-review/monotone_pchip/clean/{item['id']}/frame.json"
        )
    rala = manifest["products"]["rala"]
    assert rala["latest"] == "rala/clean/latest/{z}/{x}/{y}.png"
    assert rala["latest_frame"] == "20260929T000000Z"
    assert rala["modes"]["clean"]["frames"][0]["tiles"].startswith("rala/clean/")
    assert frame_id_for(stale) not in [item["id"] for item in frames]


def test_d_review_loop_cooks_newest_n_and_stops(monkeypatch, tmp_path: Path):
    from datetime import datetime, timedelta, timezone

    from mpwg_radar.config import CookerConfig
    from mpwg_radar.cooker import _cook_d_review_loop, _d_review_cook_limit
    from mpwg_radar.ingest import MrmsScan

    assert _d_review_cook_limit(CookerConfig(product_id="rala", rala_review_frames=8)) == 0
    limited = CookerConfig(
        product_id="rala",
        rala_dbz_interp="monotone_pchip",
        rala_review_frames=6,
    )
    assert _d_review_cook_limit(limited) == 6
    assert _d_review_cook_limit(
        CookerConfig(
            product_id="rala",
            rala_dbz_interp="monotone_pchip",
            rala_review_frames=40,
        )
    ) == 12

    now = datetime.now(timezone.utc).replace(microsecond=0)
    scans = [
        MrmsScan(valid_time=now - timedelta(minutes=2 * i), key=f"obj/{i}.grib2.gz")
        for i in range(10)
    ]
    monkeypatch.setattr(
        "mpwg_radar.cooker.list_recent_s3_scans", lambda *_a, **_k: scans
    )
    monkeypatch.setattr(
        "mpwg_radar.cooker.download_s3_key",
        lambda _cfg, key, _dest: Path(key),
    )
    seen = []

    def _fake_cook(_cfg, **kwargs):
        seen.append(str(kwargs.get("grib_path")))
        assert kwargs.get("archive") is False
        return {"frame_id": f"f{len(seen)}", "valid_time": now.isoformat(), "uploaded": 0}

    monkeypatch.setattr("mpwg_radar.cooker.cook", _fake_cook)
    cfg = CookerConfig(
        product_id="rala",
        rala_dbz_interp="monotone_pchip",
        rala_review_frames=6,
        output_dir=tmp_path,
        data_dir=tmp_path / "data",
        upload=False,
        modes=["clean"],
    )
    result = _cook_d_review_loop(cfg, False, limit=6)
    assert len(seen) == 6
    assert seen[0].endswith("obj/0.grib2.gz")
    assert result["archive_cooked"] == 6
    assert result["review_frames"] == 6
