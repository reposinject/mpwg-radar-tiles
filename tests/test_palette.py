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


# Sample RGB locked from the Phase 3 RadarScope taps. Alpha is the p3c ramp.
RALA_SAMPLE_RGB = {
    2.0: (28, 138, 48),
    10.3: (46, 158, 60),
    24.8: (62, 190, 72),
    31.7: (154, 212, 46),
    39.7: (248, 220, 16),
    48.3: (244, 152, 34),
    56.4: (200, 24, 120),
    64.7: (224, 48, 216),
}


def test_rala_piecewise_anchors_match_radarscope_stops():
    from mpwg_radar.palette import RALA_PALETTE_VERSION, derive_rala_p3c_stops

    pal = load_palette("mpwg-rala-2026-09")
    assert pal.version == RALA_PALETTE_VERSION == "2026-09-rala-p3d"
    assert pal.min_dbz == -32.0
    derived = derive_rala_p3c_stops()
    assert [stop.dbz for stop in pal.stops] == [stop.dbz for stop in derived]
    assert [stop.rgba for stop in pal.stops] == [stop.rgba for stop in derived]
    for stop in pal.stops:
        assert _rgba(pal, stop.dbz) == stop.rgba
    for dbz, rgb in RALA_SAMPLE_RGB.items():
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
    for dbz in RALA_SAMPLE_RGB:
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
    # 2 dBZ is crisp but quiet: greener than the bookend, partial alpha.
    r2, g2, b2, a2 = _rgba(pal, 2.0)
    assert 100 < a2 < 220
    assert g2 > 100 and g2 > r2 and g2 > b2 and b2 < 80
    # 0.9 and the other weak taps stay green wisps, not neon and not deleted.
    r09, g09, b09, a09 = _rgba(pal, 0.9)
    assert 0 < a09 < 255 and g09 > r09 and g09 > b09 and g09 > 100
    for dbz in (12.2, 17.2, 22.8, 25.7, 25.8, 26.2, 27.2):
        r, g, b, _a = _rgba(pal, dbz)
        assert g > r and g > b and b < 100, (dbz, r, g, b)
    # 10.3 is weak green, not neon, and not yet opaque. No cutoff at 10.
    r10, g10, b10, a10 = _rgba(pal, 10.3)
    assert a2 < a10 < 255 and g10 > r10 + 40 and g10 < 190 and b10 < 90 and r10 < 80
    # 24.8 medium green, still green rather than yellow.
    r25, g25, b25, a25 = _rgba(pal, 24.8)
    assert a25 == 255 and g25 > 150 and r25 < 90 and b25 < 100 and g25 > r25 + 80
    # 31.7 strong green with yellow arriving (R has climbed, G still leads).
    r32, g32, b32, _a32 = _rgba(pal, 31.7)
    assert g32 > 180 and r32 > 100 and g32 > r32 and b32 < 80
    for dbz in (32.7, 33.2):
        r, g, b, _a = _rgba(pal, dbz)
        assert g > 180 and g >= r and b < 60, (dbz, r, g, b)
    # 39–41 is yellow. 36.9 is still climbing out of yellow-green, not a cliff.
    r36, g36, b36, a36 = _rgba(pal, 36.9)
    assert a36 == 255 and g36 > 180 and b36 < 50 and r36 < 240
    for dbz in (39.4, 39.7, 40.5, 40.7):
        r, g, b, a = _rgba(pal, dbz)
        assert a == 255 and r > 230 and g >= 200 and b < 50, (dbz, r, g, b)
    r44, g44, b44, _ = _rgba(pal, 44.1)
    assert r44 > 230 and g44 >= 165 and b44 < 50
    for dbz in (46.6, 47.7, 48.3):
        r, g, b, _a = _rgba(pal, dbz)
        assert r > 220 and g >= 145 and b < 60, (dbz, r, g, b)
    # Red is the hue arc between orange (48.3) and magenta (56.4), not a 2 dBZ cliff.
    r50, g50, b50, a50 = _rgba(pal, 50.0)
    assert a50 == 255 and r50 > 200 and g50 > 60  # still orange, not slammed to red
    r52, g52, b52, _a52 = _rgba(pal, 52.5)
    assert r52 > 190 and g52 < 50 and b52 < 50  # clean red on the hue arc
    r56, g56, b56, _ = _rgba(pal, 56.4)
    assert r56 > 160 and b56 > 80 and g56 < 50 and b56 > g56
    # 61 is magenta, not the old purple detour (blue above red).
    r61, g61, b61, _ = _rgba(pal, 61.2)
    assert r61 > 180 and b61 > 120 and g61 < 60 and r61 > b61
    r65, g65, b65, a65 = _rgba(pal, 64.7)
    assert a65 == 255 and r65 > 200 and b65 > 150 and g65 < 80 and b65 > g65
    for dbz in (65.5, 70.2, 74.8, 78.0):
        r, g, b, a = _rgba(pal, dbz)
        assert a == 255 and r > 180, (dbz, r, g, b)
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
