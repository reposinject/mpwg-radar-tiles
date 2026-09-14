from __future__ import annotations

from mpwg_radar.geo import CENTRAL_TEXAS, count_tiles, latlon_to_global_xy, tiles_for_bbox, tile_bounds


def test_austin_inside_central_texas():
    assert CENTRAL_TEXAS.west < -97.74 < CENTRAL_TEXAS.east
    assert CENTRAL_TEXAS.south < 30.27 < CENTRAL_TEXAS.north


def test_tile_roundtrip_austin_z8():
    z = 8
    x, y = latlon_to_global_xy(-97.743, 30.267, z)
    xi, yi = int(x), int(y)
    bounds = tile_bounds(z, xi, yi)
    assert bounds.west <= -97.743 <= bounds.east
    assert bounds.south <= 30.267 <= bounds.north


def test_central_texas_is_a_handful_of_tiles():
    # Production zooms 6–9 should stay tiny vs CONUS.
    n = count_tiles(CENTRAL_TEXAS, 6, 9)
    assert 10 < n < 400
    z8 = tiles_for_bbox(CENTRAL_TEXAS, 8)
    assert 4 <= len(z8) <= 80
