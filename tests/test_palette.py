"""Tests for MPWG Clean palette: physical dBZ stays numeric; color is a later step."""

from __future__ import annotations

import numpy as np

from mpwg_radar.palette import load_palette


def test_below_first_stop_is_transparent():
    pal = load_palette()
    rgba = pal.colorize(np.array([[4.9]], dtype=np.float32))
    assert tuple(int(c) for c in rgba[0, 0]) == (0, 0, 0, 0)


def test_clean_qc_makes_sub_10_transparent():
    pal = load_palette()
    from mpwg_radar.qc import threshold

    dbz = threshold(np.array([[9.9, 10.0]], dtype=np.float32), 10.0)
    rgba = pal.colorize(dbz)
    assert tuple(int(c) for c in rgba[0, 0]) == (0, 0, 0, 0)
    assert rgba[0, 1, 3] > 0


def test_nan_is_transparent():
    pal = load_palette()
    rgba = pal.colorize(np.array([[np.nan]], dtype=np.float32))
    assert rgba[0, 0, 3] == 0


def test_barely_tinted_10_15():
    pal = load_palette()
    a10 = pal.colorize(np.array([[10.0]], dtype=np.float32))[0, 0]
    a14 = pal.colorize(np.array([[14.0]], dtype=np.float32))[0, 0]
    assert a10[1] > a10[0]  # green channel dominates
    assert a10[3] < 100  # barely there
    assert a14[3] > a10[3]


def test_light_and_medium_green():
    pal = load_palette()
    light = pal.colorize(np.array([[20.0]], dtype=np.float32))[0, 0]
    medium = pal.colorize(np.array([[30.0]], dtype=np.float32))[0, 0]
    assert light[1] > 180 or light[1] > light[0]
    assert medium[1] > medium[0]
    assert medium[3] >= light[3]


def test_yellow_green_to_yellow():
    pal = load_palette()
    yg = pal.colorize(np.array([[40.0]], dtype=np.float32))[0, 0]
    y = pal.colorize(np.array([[45.0]], dtype=np.float32))[0, 0]
    assert yg[0] > 140 and yg[1] > 180
    assert y[0] > 200 and y[1] > 180


def test_orange_then_red():
    pal = load_palette()
    orange = pal.colorize(np.array([[50.0]], dtype=np.float32))[0, 0]
    red = pal.colorize(np.array([[60.0]], dtype=np.float32))[0, 0]
    assert orange[0] > 200 and orange[1] > 100
    assert red[0] > red[1] and red[1] < 120


def test_magenta_reserved_for_cores():
    pal = load_palette()
    core = pal.colorize(np.array([[75.0]], dtype=np.float32))[0, 0]
    assert core[0] > 120 and core[2] > 80  # red + blue = magenta family
    assert core[3] == 255


def test_lighter_than_classic_heavy_green():
    pal = load_palette()
    medium = pal.colorize(np.array([[32.0]], dtype=np.float32))[0, 0]
    # Classic NWS medium/dark green is near (0, 144, 0). Ours is lighter.
    assert int(medium[1]) >= 150
