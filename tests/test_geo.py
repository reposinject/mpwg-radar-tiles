from __future__ import annotations

from mpwg_radar.geo import (
    CENTRAL_TEXAS,
    CONUS,
    count_tiles,
    latlon_to_global_xy,
    parse_bbox,
    tiles_by_zoom,
    tiles_for_bbox,
    tile_bounds,
)


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
    n = count_tiles(CENTRAL_TEXAS, 6, 9)
    assert n == 80
    z8 = tiles_for_bbox(CENTRAL_TEXAS, 8)
    assert 4 <= len(z8) <= 80


def test_conus_covers_lower_48_not_ak_hi():
    cities = {
        "seattle": (-122.33, 47.61),
        "los_angeles": (-118.24, 34.05),
        "denver": (-104.99, 39.74),
        "chicago": (-87.63, 41.88),
        "new_york": (-74.01, 40.71),
        "miami": (-80.19, 25.76),
        "austin": (-97.74, 30.27),
        "boston": (-71.06, 42.36),
    }
    for lon, lat in cities.values():
        assert CONUS.west < lon < CONUS.east
        assert CONUS.south < lat < CONUS.north
    # Honolulu / Anchorage are outside the NOAA MRMS CONUS mosaic.
    assert not (CONUS.west < -157.86 < CONUS.east and CONUS.south < 21.31 < CONUS.north)
    assert not (CONUS.west < -149.90 < CONUS.east and CONUS.south < 61.22 < CONUS.north)


def test_conus_tile_counts_match_documented_band():
    by_zoom = tiles_by_zoom(CONUS, 5, 9)
    assert by_zoom == {5: 35, 6: 126, 7: 442, 8: 1734, 9: 6600}
    assert count_tiles(CONUS, 6, 8) == 2302
    assert count_tiles(CONUS, 6, 9) == 8902
    assert count_tiles(CONUS, 5, 8) == 2337


def test_parse_bbox_west_south_east_north():
    box = parse_bbox("-125,24,-66.5,49.5", name="l48")
    assert box.west == -125.0
    assert box.south == 24.0
    assert box.east == -66.5
    assert box.north == 49.5
    assert box.name == "l48"
