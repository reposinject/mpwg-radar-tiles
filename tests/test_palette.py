"""Tests for MPWG Clean palette: physical dBZ stays numeric; color is a later step."""

from __future__ import annotations

import numpy as np

from mpwg_radar.palette import load_palette

# James / Sep 2026 locked anchors (opaque). Interpolation uses actual dBZ.
ANCHORS = {
    15.0: (8, 119, 46, 255),
    20.0: (15, 163, 61, 255),
    25.0: (50, 201, 75, 255),
    30.0: (244, 242, 13, 255),
    35.0: (244, 194, 13, 255),
    40.0: (245, 138, 22, 255),
    45.0: (243, 74, 31, 255),
    50.0: (237, 23, 28, 255),
    55.0: (201, 20, 39, 255),
    60.0: (229, 42, 174, 255),
    65.0: (181, 42, 203, 255),
    70.0: (120, 40, 200, 255),
    75.0: (217, 182, 255, 255),
}


def _rgba(pal, dbz: float):
    return tuple(int(c) for c in pal.colorize(np.array([[dbz]], dtype=np.float32))[0, 0])


def test_below_display_cutoff_is_transparent():
    pal = load_palette()
    assert pal.min_dbz == 15.0
    assert _rgba(pal, 14.9) == (0, 0, 0, 0)
    assert _rgba(pal, 0.0) == (0, 0, 0, 0)
    assert _rgba(pal, 10.0) == (0, 0, 0, 0)


def test_colorize_does_not_discard_underlying_dbz():
    pal = load_palette()
    dbz = np.array([[12.0, 15.0, 31.0]], dtype=np.float32)
    orig = dbz.copy()
    rgba = pal.colorize(dbz)
    np.testing.assert_array_equal(dbz, orig)
    assert tuple(int(c) for c in rgba[0, 0]) == (0, 0, 0, 0)
    assert tuple(int(c) for c in rgba[0, 1]) == ANCHORS[15.0]


def test_clean_qc_still_separate_from_display_cutoff():
    pal = load_palette()
    from mpwg_radar.qc import threshold

    dbz = np.array([[9.9, 12.0, 15.0]], dtype=np.float32)
    kept = threshold(dbz, 10.0)
    assert np.isnan(kept[0, 0])
    assert kept[0, 1] == 12.0
    rgba = pal.colorize(kept)
    assert tuple(int(c) for c in rgba[0, 0]) == (0, 0, 0, 0)
    assert tuple(int(c) for c in rgba[0, 1]) == (0, 0, 0, 0)
    assert rgba[0, 2, 3] == 255


def test_nan_is_transparent():
    pal = load_palette()
    rgba = pal.colorize(np.array([[np.nan]], dtype=np.float32))
    assert rgba[0, 0, 3] == 0


def test_anchor_interpolation_15_30_31_35_50():
    pal = load_palette()
    assert _rgba(pal, 15.0) == ANCHORS[15.0]
    assert _rgba(pal, 30.0) == ANCHORS[30.0]
    assert _rgba(pal, 35.0) == ANCHORS[35.0]
    assert _rgba(pal, 50.0) == ANCHORS[50.0]

    # 31 dBZ must interpolate between 30 and 35, not snap to a 5 dBZ bucket.
    c30 = np.array(ANCHORS[30.0], dtype=np.float64)
    c35 = np.array(ANCHORS[35.0], dtype=np.float64)
    expected = np.clip(c30 + 0.2 * (c35 - c30), 0, 255).astype(np.uint8)
    got = np.array(_rgba(pal, 31.0), dtype=np.uint8)
    assert not np.array_equal(got, ANCHORS[30.0])
    assert not np.array_equal(got, ANCHORS[35.0])
    np.testing.assert_allclose(got, expected, atol=1)
    # Green channel is the channel that actually moves 30 → 35.
    assert ANCHORS[35.0][1] < got[1] < ANCHORS[30.0][1]


def test_locked_anchor_stops_match_json():
    pal = load_palette()
    for dbz, rgba in ANCHORS.items():
        assert _rgba(pal, dbz) == rgba
    assert _rgba(pal, 80.0) == ANCHORS[75.0]


def test_no_cyan_or_aqua_in_reflectivity_ramp():
    pal = load_palette()
    for dbz in np.linspace(15.0, 75.0, 121):
        r, g, b, a = _rgba(pal, float(dbz))
        assert a == 255
        # Classic NWS clear-air cyan/aqua: high G and B, low R.
        assert not (r < 90 and g > 140 and b > 140), (dbz, r, g, b)


def test_colorbar_starts_at_display_cutoff():
    pal = load_palette()
    img = pal.colorbar()
    arr = np.array(img)
    assert img.size == (512, 48)
    assert tuple(int(c) for c in arr[0, 0]) == ANCHORS[15.0]
    assert arr[0, -1, 3] == 255


# p3f calibration anchors. 2 and 10 stay the RadarScope taps. Mid and high
# bands are the p3e families with the remaining mud pulled out. Alpha is
# still the continuous ramp.
RALA_ANCHOR_RGB = {
    2.0: (28, 138, 48),
    10.0: (46, 158, 60),
    25.0: (0, 176, 0),
    32.0: (255, 246, 0),
    40.0: (255, 108, 0),
    48.0: (255, 40, 0),
    56.0: (176, 0, 0),
    65.0: (255, 0, 255),
}

# colorize() of 2026-09-rala-p3d at the same dBZ. Cores must beat these.
_P3D_RGB = {
    25.0: (65, 191, 71),
    40.0: (248, 218, 17),
    48.0: (244, 154, 33),
    56.0: (202, 25, 111),
    65.0: (225, 54, 217),
    70.0: (240, 155, 236),
}


def _chroma(rgb) -> int:
    return max(rgb[:3]) - min(rgb[:3])


def test_rala_piecewise_anchors_match_stops():
    from mpwg_radar.palette import RALA_PALETTE_VERSION, derive_rala_p3f_stops

    pal = load_palette("mpwg-rala-2026-09")
    assert pal.version == RALA_PALETTE_VERSION == "2026-09-rala-p3f"
    assert pal.min_dbz == -32.0
    derived = derive_rala_p3f_stops()
    assert [stop.dbz for stop in pal.stops] == [stop.dbz for stop in derived]
    assert [stop.rgba for stop in pal.stops] == [stop.rgba for stop in derived]
    for stop in pal.stops:
        assert _rgba(pal, stop.dbz) == stop.rgba
    for dbz, rgb in RALA_ANCHOR_RGB.items():
        assert _rgba(pal, dbz)[:3] == rgb
    assert _rgba(pal, 75.0) == (255, 255, 255, 255)
    assert _rgba(pal, 80.0) == (255, 255, 255, 255)
    assert _rgba(pal, -32.1) == (0, 0, 0, 0)
    # From 0 dBZ up, stops are at most 2.5 apart so a nearest-stop strip
    # cannot paint a whole decade one color. -32 is the wisp bookend.
    legend = [stop.dbz for stop in pal.stops]
    assert legend[0] == -32.0
    precip = [dbz for dbz in legend if dbz >= 0.0]
    assert max(b - a for a, b in zip(precip, precip[1:])) <= 2.5
    for dbz in RALA_ANCHOR_RGB:
        assert dbz in legend


def test_rala_ramps_are_piecewise_not_a_single_gradient():
    """Midpoints sit on the segment between neighboring anchors, not a global curve."""
    pal = load_palette("mpwg-rala-2026-09")
    stops = [(stop.dbz, stop.rgba) for stop in pal.stops]
    for (lo, clo), (hi, chi) in zip(stops, stops[1:]):
        mid = (lo + hi) / 2.0
        expected = np.clip(
            np.array(clo, dtype=np.float64) * 0.5 + np.array(chi, dtype=np.float64) * 0.5,
            0,
            255,
        )
        got = np.array(_rgba(pal, mid), dtype=np.float64)
        # 0.1 dBZ LUT can sit half a step off the exact midpoint on a steep segment.
        np.testing.assert_allclose(got, expected, atol=3.0)
        if hi - lo >= 2.0:
            assert tuple(int(c) for c in got) not in {clo, chi}


def test_rala_anchor_feel_and_no_cyan():
    pal = load_palette("mpwg-rala-2026-09")
    # 2 dBZ is the subtle tap: greener than the bookend, partial alpha.
    r2, g2, b2, a2 = _rgba(pal, 2.0)
    assert 100 < a2 < 220
    assert g2 > 100 and g2 > r2 and g2 > b2 and b2 < 80
    # 0.9 and the other weak taps stay green wisps, not neon and not deleted.
    r09, g09, b09, a09 = _rgba(pal, 0.9)
    assert 0 < a09 < 255 and g09 > r09 and g09 > b09 and g09 > 100
    for dbz in (0.1, 5.0, 10.0, 12.2, 17.2, 20.0):
        r, g, b, a = _rgba(pal, dbz)
        assert a > 0 and g > r and g > b and b < 100 and g < 200, (dbz, r, g, b)
    # 10 is still subtle green, not neon, and not yet opaque. No cutoff at 10.
    r10, g10, b10, a10 = _rgba(pal, 10.0)
    assert a2 < a10 < 220 and g10 > r10 + 40 and r10 < 80 and b10 < 90
    # 20–30 deep greens: opaque from 20, G leads, purer than the weak tap.
    r20, g20, b20, a20 = _rgba(pal, 20.0)
    assert a20 == 255 and g20 > r20 and g20 > b20 and g20 < 170 and b20 < 40
    r25, g25, b25, a25 = _rgba(pal, 25.0)
    assert a25 == 255 and 150 <= g25 <= 200 and r25 < 20 and b25 < 20 and g25 > r25 + 100
    assert g25 > g10
    r22, g22, b22, a22 = _rgba(pal, 22.8)
    assert a22 == 255 and g22 > r22 and g22 > b22 and b22 < 90
    for dbz in (25.7, 27.2, 30.0):
        r, g, b, a = _rgba(pal, dbz)
        assert a == 255 and g > r and g > b and b < 40, (dbz, r, g, b)
    # 30–40 bright yellow → strong gold. Fully opaque.
    r32, g32, b32, a32 = _rgba(pal, 32.0)
    assert a32 == 255 and r32 > 240 and g32 > 230 and b32 < 10
    r40, g40, b40, a40 = _rgba(pal, 40.0)
    assert a40 == 255 and r40 > 240 and 80 < g40 < 140 and b40 < 10
    # 40–50 gold → heavy orange.
    r44, g44, b44, _ = _rgba(pal, 44.0)
    assert r44 > 240 and g44 > g40 * 0.4 and g44 < g40 and b44 < 20
    r48, g48, b48, a48 = _rgba(pal, 48.0)
    assert a48 == 255 and r48 > 240 and 20 < g48 < 70 and b48 < 10 and g48 < g40
    # 50–60 vivid red → dark red core. 56 stays pure red, darker than 50.
    r50, g50, b50, a50 = _rgba(pal, 50.0)
    assert a50 == 255 and r50 > 240 and g50 < 20 and b50 < 20
    r56, g56, b56, a56 = _rgba(pal, 56.0)
    assert a56 == 255 and 150 < r56 < r50 and g56 < 10 and b56 < 20 and r56 > b56 + 100
    r60, g60, b60, a60 = _rgba(pal, 60.0)
    assert a60 == 255 and r60 > 150 and g60 < 15 and b60 < 80 and r60 > b60 + 80
    # 60–65+ pink → magenta → extreme. 65 is hot; 70 has not washed out.
    r62, g62, b62, _ = _rgba(pal, 62.5)
    assert r62 > 180 and b62 > 100 and g62 < 40 and b62 > g62
    r65, g65, b65, a65 = _rgba(pal, 65.0)
    assert a65 == 255 and r65 > 220 and b65 > 200 and g65 < 40 and b65 > g65
    # 64.7 is inside that magenta core (the spatial render check reads it).
    hot = _rgba(pal, 64.7)
    assert hot[0] > 200 and hot[2] > 150 and hot[1] < 80 and hot[2] > hot[1]
    r70, g70, b70, _ = _rgba(pal, 70.0)
    assert r70 > 230 and b70 > 200 and g70 < 120 and b70 > g70
    assert _rgba(pal, 75.0)[:3] == (255, 255, 255)
    # Valid low dBZ stays a green wisp. Below the display floor is clear.
    _r5, g5, _b5, a5 = _rgba(pal, 5.0)
    assert 0 < a5 < 255 and g5 > _r5 and g5 > _b5
    rm, gm, bm, am = _rgba(pal, -5.0)
    assert 0 < am < a2 and gm > rm and gm > bm and gm < 140
    for dbz in np.linspace(-32.0, 78.0, 221):
        r, g, b, a = _rgba(pal, float(dbz))
        assert a > 0
        assert not (r < 90 and g > 140 and b > 140), (dbz, r, g, b)
    # Legend hex matches the stop table the app draws.
    for stop in pal.stops:
        assert stop.hex.upper() == "#{:02X}{:02X}{:02X}".format(*stop.rgba[:3])


# colorize() of 2026-09-rala-p3e at the bands James still called muted.
_P3E_RGB = {
    2.0: (28, 138, 48),
    10.0: (46, 158, 60),
    20.0: (50, 182, 58),
    25.0: (36, 208, 50),
    30.0: (116, 216, 40),
    32.0: (232, 220, 12),
    40.0: (252, 176, 4),
    48.0: (252, 108, 16),
    50.0: (228, 28, 18),
    56.0: (196, 10, 24),
    60.0: (192, 6, 36),
    65.0: (246, 18, 232),
    70.0: (255, 72, 248),
}


def _rgb_dist(a, b) -> float:
    return float(np.linalg.norm(np.array(a[:3], dtype=np.float64) - np.array(b[:3], dtype=np.float64)))


def test_rala_p3f_cores_pop_more_than_p3e_and_stay_continuous():
    """Mid/high anchors are richer than p3e. Weak taps and the 0.1 dBZ LUT stay."""
    pal = load_palette("mpwg-rala-2026-09")
    assert _chroma(_rgba(pal, 25.0)) >= _chroma(_P3D_RGB[25.0]) + 30
    assert _rgba(pal, 40.0)[1] <= _P3D_RGB[40.0][1] - 30
    assert _chroma(_rgba(pal, 48.0)) > _chroma(_P3D_RGB[48.0])
    assert _rgba(pal, 56.0)[2] < 50 and _P3D_RGB[56.0][2] > 100
    assert _chroma(_rgba(pal, 65.0)) >= _chroma(_P3D_RGB[65.0]) + 40
    assert _chroma(_rgba(pal, 70.0)) >= _chroma(_P3D_RGB[70.0]) + 70
    # Weak echoes stay on the RadarScope taps. Through 10 dBZ nothing moves.
    assert _rgba(pal, 2.0)[:3] == _P3E_RGB[2.0]
    assert _rgba(pal, 10.0)[:3] == _P3E_RGB[10.0]
    assert max(abs(a - b) for a, b in zip(_rgba(pal, 10.0)[:3], (45, 157, 60))) <= 2
    for dbz in (25.0, 32.0, 40.0, 48.0, 50.0, 65.0, 70.0):
        assert _chroma(_rgba(pal, dbz)) > _chroma(_P3E_RGB[dbz]), dbz
    # Gold and orange leave the pale yellow. 50 is pure red; 56 is a darker pure core.
    assert _rgba(pal, 40.0)[1] <= _P3E_RGB[40.0][1] - 40
    assert _rgba(pal, 48.0)[1] <= _P3E_RGB[48.0][1] - 40
    assert _rgba(pal, 50.0)[:3] == (255, 0, 0)
    assert _rgba(pal, 56.0)[1] == 0 and _rgba(pal, 56.0)[2] == 0
    assert _rgba(pal, 56.0)[0] < _rgba(pal, 50.0)[0] - 40
    assert _rgb_dist(_rgba(pal, 50.0), _rgba(pal, 56.0)) > _rgb_dist(_P3E_RGB[50.0], _P3E_RGB[56.0])
    assert _rgb_dist(_rgba(pal, 56.0), _rgba(pal, 60.0)) >= _rgb_dist(
        _P3E_RGB[56.0], _P3E_RGB[60.0]
    ) + 20
    # Weak taps keep the quiet alpha. The storm body is opaque from 20 up.
    assert _rgba(pal, 2.0)[3] == 127
    assert _rgba(pal, 10.0)[3] == 165
    assert _rgba(pal, 10.0)[3] < _rgba(pal, 15.0)[3] < _rgba(pal, 20.0)[3] == 255
    for dbz in (25.0, 32.0, 40.0, 48.0, 56.0, 65.0):
        assert _rgba(pal, dbz)[3] == 255
    # 10–20 stays a deep subdued green, not a second neon.
    assert _rgba(pal, 20.0)[1] < 160
    assert _chroma(_rgba(pal, 20.0)) <= _chroma(_P3E_RGB[20.0]) + 15
    assert _chroma(_rgba(pal, 25.0)) >= _chroma(_rgba(pal, 20.0)) + 30

    prev = None
    for step_i in range(-320, 751):
        dbz = step_i / 10.0
        cur = _rgba(pal, dbz)
        if prev is not None:
            jump = max(abs(cur[channel] - prev[channel]) for channel in range(4))
            assert jump <= 12, (dbz, prev, cur)
        prev = cur
    for lo, hi in ((0.1, 10.0), (10.0, 20.0), (20.0, 30.0), (30.0, 40.0), (40.0, 50.0), (50.0, 60.0), (60.0, 65.0)):
        dist = float(np.linalg.norm(np.array(_rgba(pal, lo)[:3]) - np.array(_rgba(pal, hi)[:3])))
        assert dist >= 15.0, (lo, hi, dist)


def test_rala_colorbar_is_the_production_lut():
    pal = load_palette("mpwg-rala-2026-09")
    width = 640
    img = np.array(pal.colorbar(width=width, height=3, dbz_min=0.0, dbz_max=75.0))
    ramp = np.linspace(0.0, 75.0, width, dtype=np.float32)
    direct = pal.colorize(ramp[np.newaxis, :])
    np.testing.assert_array_equal(img[1], direct[0])
