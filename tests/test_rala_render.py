"""RALA Phase 2: faint-green weak echo, no despeckle, no clear-air bloom."""

from __future__ import annotations

from datetime import datetime, timezone

import numpy as np

from mpwg_radar.geo import latlon_to_global_xy, tile_bounds
from mpwg_radar.grib import ReflectivityFrame
from mpwg_radar.palette import load_palette
from mpwg_radar.products import CAT_NO_ECHO, CAT_VALID, RALA
from mpwg_radar.qc import apply_mode, edge_aware_smooth, mild_smooth
from mpwg_radar.tiles import render_tile, sample_masked_bilinear, sample_masked_splat


def _frame(dbz, lat, lon, category):
    return ReflectivityFrame(
        dbz=np.asarray(dbz, dtype=np.float32),
        lat=np.asarray(lat, dtype=np.float64),
        lon=np.asarray(lon, dtype=np.float64),
        valid_time=datetime(2026, 9, 21, 18, 0, tzinfo=timezone.utc),
        product="ReflectivityAtLowestAltitude",
        category=np.asarray(category, dtype=np.uint8),
    )


def test_clear_air_next_to_strong_echo_stays_empty_and_edge_does_not_fade():
    """Nearest cell is the footprint. Invalid neighbors are not averaged in."""
    lat = np.array([30.00, 29.99], dtype=np.float64)
    lon = np.array([-98.00, -97.99], dtype=np.float64)
    dbz = np.array([[60.0, np.nan], [np.nan, np.nan]], dtype=np.float32)
    cat = np.full(dbz.shape, CAT_NO_ECHO, dtype=np.uint8)
    cat[0, 0] = CAT_VALID

    # 40% of the way toward the clear-air neighbor, still inside the echo cell.
    qlat = np.array([[30.00]], dtype=np.float64)
    qlon = np.array([[-98.00 + 0.004]], dtype=np.float64)
    sampled, sampled_cat, edge = sample_masked_bilinear(dbz, lat, lon, qlat, qlon, cat)
    assert sampled_cat[0, 0] == CAT_VALID
    # Must stay at the echo cell's dBZ, not fade toward the empty neighbor.
    assert abs(float(sampled[0, 0]) - 60.0) < 1e-3
    # Still inside the cell, but near clear air: square rim fades, dBZ does not.
    assert 0.0 < float(edge[0, 0]) < 0.85

    center, center_cat, center_edge = sample_masked_bilinear(
        dbz, lat, lon, qlat, np.array([[-98.00]], dtype=np.float64), cat
    )
    assert abs(float(center[0, 0]) - 60.0) < 1e-3
    assert float(center_edge[0, 0]) > 0.95

    # Just inside the neighboring clear-air cell. Must not pick up the 60 dBZ.
    qlon_clear = np.array([[-98.00 + 0.006]], dtype=np.float64)
    sampled_clear, cat_clear, clear_edge = sample_masked_bilinear(
        dbz, lat, lon, qlat, qlon_clear, cat
    )
    assert cat_clear[0, 0] == CAT_NO_ECHO
    assert np.isnan(sampled_clear[0, 0])
    assert float(clear_edge[0, 0]) == 0.0

    pal = load_palette("mpwg-rala-2026-09")
    rgba = pal.colorize(sampled_clear, category=cat_clear)
    assert tuple(int(c) for c in rgba[0, 0]) == (0, 0, 0, 0)
    painted = pal.colorize(center, category=center_cat)
    assert painted[0, 0, 3] == 255
    # 64.7 dBZ is the magenta/pink core, not a dark-red plateau.
    hot = pal.colorize(np.array([[64.7]], dtype=np.float32))[0, 0]
    assert int(hot[0]) > 200 and int(hot[2]) > 150 and int(hot[1]) < 80
    assert int(hot[2]) > int(hot[1])


def test_echo_neighbors_interpolate_instead_of_posterizing():
    lat = np.array([30.00, 29.99], dtype=np.float64)
    lon = np.array([-98.00, -97.99], dtype=np.float64)
    dbz = np.array([[30.0, 50.0], [np.nan, np.nan]], dtype=np.float32)
    cat = np.full(dbz.shape, CAT_NO_ECHO, dtype=np.uint8)
    cat[0, :] = CAT_VALID
    # i_f = 0.2 → 0.8*30 + 0.2*50 = 34, still nearest to the 30 dBZ cell.
    qlat = np.array([[30.00]], dtype=np.float64)
    qlon = np.array([[-97.998]], dtype=np.float64)
    sampled, sampled_cat, edge = sample_masked_bilinear(dbz, lat, lon, qlat, qlon, cat)
    assert float(edge[0, 0]) > 0.95
    assert sampled_cat[0, 0] == CAT_VALID
    assert abs(float(sampled[0, 0]) - 34.0) < 1e-3
    pal = load_palette("mpwg-rala-2026-09")
    got = pal.colorize(sampled, category=sampled_cat)[0, 0]
    flat30 = pal.colorize(np.array([[30.0]], dtype=np.float32))[0, 0]
    flat50 = pal.colorize(np.array([[50.0]], dtype=np.float32))[0, 0]
    assert not np.array_equal(got, flat30)
    assert not np.array_equal(got, flat50)
    expect = pal.colorize(np.array([[34.0]], dtype=np.float32))[0, 0]
    np.testing.assert_array_equal(got, expect)


def test_isolated_weak_cell_survives_rala_clean_and_paints():
    dbz = np.full((9, 9), np.nan, dtype=np.float32)
    dbz[4, 4] = -5.0
    cat = np.full(dbz.shape, CAT_NO_ECHO, dtype=np.uint8)
    cat[4, 4] = CAT_VALID
    lat = np.linspace(31.0, 30.92, 9)
    lon = np.linspace(-98.0, -97.92, 9)
    frame = _frame(dbz, lat, lon, cat)
    kept = apply_mode(
        frame,
        "clean",
        apply_dbz_floor=RALA.apply_dbz_floor,
        apply_despeckle=RALA.apply_despeckle,
        edge_aware=RALA.edge_aware_smooth,
        apply_grid_smooth=RALA.apply_grid_smooth,
    )
    assert kept.category[4, 4] == CAT_VALID
    assert abs(float(kept.dbz[4, 4]) - (-5.0)) < 1e-3
    assert kept.category[4, 5] == CAT_NO_ECHO
    assert np.isnan(kept.dbz[4, 5])

    dropped = apply_mode(frame, "clean")
    assert np.isnan(dropped.dbz[4, 4])

    pal = load_palette(RALA.palette_id)
    rgba = pal.colorize(kept.dbz, category=kept.category)
    assert 0 < rgba[4, 4, 3] < 255  # wispy, not an opaque dark block
    assert rgba[4, 5, 3] == 0
    r, g, b, _a = (int(c) for c in rgba[4, 4])
    assert g > r and g > b
    assert not (r < 90 and g > 140 and b > 140)
    anchor15 = pal.colorize(np.array([[15.0]], dtype=np.float32))[0, 0]
    assert not np.array_equal(rgba[4, 4], anchor15)


def test_splat_rounds_squares_without_painting_clear_air():
    lat = np.array([30.00, 29.99], dtype=np.float64)
    lon = np.array([-98.00, -97.99], dtype=np.float64)
    dbz = np.array([[62.0, np.nan], [np.nan, np.nan]], dtype=np.float32)
    cat = np.full(dbz.shape, CAT_NO_ECHO, dtype=np.uint8)
    cat[0, 0] = CAT_VALID
    qlat = np.array([[30.00]], dtype=np.float64)

    center, center_cat, center_s = sample_masked_splat(
        dbz, lat, lon, qlat, np.array([[-98.00]]), cat
    )
    assert center_cat[0, 0] == CAT_VALID
    assert abs(float(center[0, 0]) - 62.0) < 1.0
    assert float(center_s[0, 0]) > 0.9

    # Voronoi corner of the echo cell: still the echo cell, but not a square.
    corner_lon = -98.00 + 0.0049
    corner_lat = 30.00 - 0.0049
    corner, corner_cat, corner_s = sample_masked_splat(
        dbz, lat, lon, np.array([[corner_lat]]), np.array([[corner_lon]]), cat
    )
    assert corner_cat[0, 0] == CAT_VALID
    assert float(corner_s[0, 0]) < 0.35

    clear, clear_cat, clear_s = sample_masked_splat(
        dbz, lat, lon, qlat, np.array([[-98.00 + 0.006]]), cat
    )
    assert clear_cat[0, 0] == CAT_NO_ECHO
    assert np.isnan(clear[0, 0])
    assert float(clear_s[0, 0]) == 0.0


def test_splat_contour_hides_the_square_edge_and_keeps_a_hot_core():
    """A filled region's voronoi edge is transparent. The core stays hot."""
    lat = np.arange(30.20, 29.80, -0.01, dtype=np.float64)
    lon = np.arange(-98.20, -97.80, 0.01, dtype=np.float64)
    dbz = np.full((lat.size, lon.size), 42.0, dtype=np.float32)
    # Southern half is clear air, so the boundary is a long straight stair.
    split = lat.size // 2
    dbz[split:] = np.nan
    cat = np.full(dbz.shape, CAT_NO_ECHO, dtype=np.uint8)
    cat[np.isfinite(dbz)] = CAT_VALID
    edge_row = split - 1
    edge_lat = float(lat[edge_row])
    # Just inside the echo cell, against the clear neighbor: not a square rim.
    rim_lat = edge_lat - 0.0049
    rim, rim_cat, rim_s = sample_masked_splat(
        dbz, lat, lon, np.array([[rim_lat]]), np.array([[float(lon[lon.size // 2])]]), cat
    )
    assert rim_cat[0, 0] == CAT_VALID
    assert float(rim_s[0, 0]) < 0.2
    # Deeper in that same edge cell the contour is already solid.
    inner, inner_cat, inner_s = sample_masked_splat(
        dbz, lat, lon, np.array([[edge_lat + 0.002]]), np.array([[float(lon[lon.size // 2])]]), cat
    )
    assert inner_cat[0, 0] == CAT_VALID
    assert float(inner_s[0, 0]) > 0.85
    # The clear-air neighbor stays empty.
    clear_lat = edge_lat - 0.006
    clear, clear_cat, clear_s = sample_masked_splat(
        dbz, lat, lon, np.array([[clear_lat]]), np.array([[float(lon[lon.size // 2])]]), cat
    )
    assert clear_cat[0, 0] == CAT_NO_ECHO
    assert np.isnan(clear[0, 0])
    assert float(clear_s[0, 0]) == 0.0

    # 68 dBZ core in 42 dBZ rain stays in the magenta range, not averaged to ~50.
    dbz[8, 8] = 68.0
    core, core_cat, _core_s = sample_masked_splat(
        dbz, lat, lon, np.array([[float(lat[8])]]), np.array([[float(lon[8])]]), cat
    )
    assert core_cat[0, 0] == CAT_VALID
    assert float(core[0, 0]) >= 60.0


def test_edge_aware_smooth_keeps_cores_and_clear_air():
    dbz = np.full((9, 9), np.nan, dtype=np.float32)
    dbz[3:6, 3:6] = 22.0
    dbz[4, 4] = 68.0
    out = edge_aware_smooth(dbz)
    assert abs(float(out[4, 4]) - 68.0) < 1.5
    assert np.isnan(out[4, 7])
    assert np.isnan(out[0, 0])
    # Similar neighbors do blend, so a flat step is no longer a pure plateau edge.
    stepped = np.full((7, 7), np.nan, dtype=np.float32)
    stepped[:, :3] = 30.0
    stepped[:, 3:] = 34.0
    blended = edge_aware_smooth(stepped)
    assert blended[3, 2] != np.float32(30.0) or blended[3, 3] != np.float32(34.0)
    assert np.nanmax(blended) < 40
    assert np.nanmin(blended) > 25


def test_mild_smooth_does_not_invent_echo():
    dbz = np.full((5, 5), np.nan, dtype=np.float32)
    dbz[2, 2] = 55.0
    out = mild_smooth(dbz)
    assert abs(float(out[2, 2]) - 55.0) < 1e-3
    assert np.isnan(out[2, 3])
    assert np.isnan(out[1, 2])


def test_rala_tile_footprint_matches_nearest_and_interior_is_smoother():
    z = 8
    gx, gy = latlon_to_global_xy(-97.9, 30.05, z)
    x, y = int(gx), int(gy)
    bounds = tile_bounds(z, x, y)
    d = 0.01
    lat = np.arange(bounds.north + 0.02, bounds.south - 0.03, -d, dtype=np.float64)
    lon = np.arange(bounds.west - 0.02, bounds.east + 0.03, d, dtype=np.float64)
    dbz = np.full((lat.size, lon.size), np.nan, dtype=np.float32)
    cat = np.full(dbz.shape, CAT_NO_ECHO, dtype=np.uint8)
    row0 = lat.size // 2
    rows = slice(row0 - 2, row0 + 3)
    inside = np.where((lon >= bounds.west) & (lon <= bounds.east))[0]
    ramp_cols = inside[2:-2]
    assert ramp_cols.size > 8
    for k, col in enumerate(ramp_cols):
        dbz[rows, col] = 20.0 if k % 2 == 0 else 45.0
    cat[np.isfinite(dbz)] = CAT_VALID
    # Lone weak return, several cells away from the stripe.
    dbz[2, 2] = -4.0
    cat[2, 2] = CAT_VALID
    frame = _frame(dbz, lat, lon, cat)
    pal = load_palette(RALA.palette_id)

    nearest, _ = render_tile(frame, pal, z, x, y, sample_mode="nearest")
    smooth, _ = render_tile(frame, pal, z, x, y, sample_mode="masked-splat")
    default, _ = render_tile(frame, pal, z, x, y)
    near_px = np.array(nearest)
    smooth_px = np.array(smooth)
    np.testing.assert_array_equal(np.array(default), near_px)
    # Anti-bloom: nothing paints where nearest-neighbor is clear air.
    near_on = near_px[..., 3] > 0
    smooth_on = smooth_px[..., 3] > 0
    assert not np.any(smooth_on & ~near_on)
    assert near_px[..., 3].min() == 0
    assert smooth_px[..., 3].max() == 255
    # Square rims inside the echo mask lose full opacity. Clear air stays 0.
    rim = (near_px[..., 3] == 255) & (smooth_px[..., 3] > 0) & (smooth_px[..., 3] < 255)
    assert int(rim.sum()) > 20

    def unique_opaque(arr: np.ndarray) -> int:
        opaque = arr[arr[..., 3] > 0][:, :3]
        return int(np.unique(opaque, axis=0).shape[0])

    # Alternating 20/45 cells are two flat colors nearest, a gradient when blended.
    assert unique_opaque(near_px) <= 4
    assert unique_opaque(smooth_px) >= 10
