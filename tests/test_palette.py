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


# James Phase 3 calibration anchors (piecewise, version 2026-09-rala-p3a).
RALA_ANCHORS = {
    -32.0: (12, 31, 10, 120),
    2.0: (20, 56, 18, 168),
    10.3: (31, 122, 44, 230),
    24.8: (46, 174, 60, 255),
    31.7: (142, 210, 58, 255),
    39.7: (240, 196, 14, 255),
    48.3: (240, 120, 18, 255),
    56.4: (224, 24, 28, 255),
    64.7: (226, 50, 180, 255),
    75.0: (244, 182, 236, 255),
}


def test_rala_piecewise_anchors_match_radarscope_stops():
    pal = load_palette("mpwg-rala-2026-09")
    assert pal.version == "2026-09-rala-p3a"
    assert pal.min_dbz == -32.0
    assert [stop.dbz for stop in pal.stops] == list(RALA_ANCHORS)
    for dbz, rgba in RALA_ANCHORS.items():
        assert _rgba(pal, dbz) == rgba
    assert _rgba(pal, 80.0) == RALA_ANCHORS[75.0]
    assert _rgba(pal, -32.1) == (0, 0, 0, 0)


def test_rala_ramps_are_piecewise_not_a_single_gradient():
    """Midpoints sit on the segment between neighboring anchors, not a global curve."""
    pal = load_palette("mpwg-rala-2026-09")
    stops = list(RALA_ANCHORS.items())
    for (lo, clo), (hi, chi) in zip(stops, stops[1:]):
        mid = (lo + hi) / 2.0
        expected = np.clip(
            np.array(clo, dtype=np.float64) * 0.5 + np.array(chi, dtype=np.float64) * 0.5,
            0,
            255,
        )
        got = np.array(_rgba(pal, mid), dtype=np.float64)
        np.testing.assert_allclose(got, expected, atol=1.5)
        assert tuple(int(c) for c in got) not in {clo, chi}


def test_rala_anchor_feel_and_no_cyan():
    pal = load_palette("mpwg-rala-2026-09")
    # 2 dBZ is visible and subtle: green, partial alpha, not a loud swath.
    r2, g2, b2, a2 = _rgba(pal, 2.0)
    assert 100 < a2 < 200
    assert g2 > r2 and g2 > b2 and g2 < 80
    # 10.3 is weak green, not neon.
    r10, g10, b10, a10 = _rgba(pal, 10.3)
    assert a10 >= 200 and g10 > r10 + 40 and g10 < 160 and b10 < 80 and r10 < 60
    # 24.8 medium green, still green rather than yellow.
    r25, g25, b25, a25 = _rgba(pal, 24.8)
    assert a25 == 255 and g25 > 150 and r25 < 80 and b25 < 90 and g25 > r25 + 80
    # 31.7 strong green with yellow arriving (R has climbed, G still leads).
    r32, g32, b32, _a32 = _rgba(pal, 31.7)
    assert g32 > 180 and r32 > 100 and g32 > r32 and b32 < 80
    # 39.7 is the yellow→orange transition, not full orange and not cyan.
    r40, g40, b40, a40 = _rgba(pal, 39.7)
    assert a40 == 255 and r40 > 220 and g40 > 170 and b40 < 40
    # 48.3 orange, 56.4 red, 64.7 magenta/pink (B overtakes G, R stays high).
    r48, g48, b48, _ = _rgba(pal, 48.3)
    assert r48 > 220 and 80 < g48 < 160 and b48 < 40
    r56, g56, b56, _ = _rgba(pal, 56.4)
    assert r56 > 200 and g56 < 40 and b56 < 40
    r65, g65, b65, a65 = _rgba(pal, 64.7)
    assert a65 == 255 and r65 > 200 and b65 > 150 and g65 < 80 and b65 > g65
    # Valid low dBZ stays a green wisp. Below the display floor is clear.
    r5, g5, b5, a5 = _rgba(pal, 5.0)
    assert 0 < a5 < 255 and g5 > r5 and g5 > b5
    rm, gm, bm, am = _rgba(pal, -5.0)
    assert 0 < am < a2 and gm > rm and gm > bm and gm < 140
    for dbz in np.linspace(-32.0, 75.0, 216):
        r, g, b, a = _rgba(pal, float(dbz))
        assert a > 0
        assert not (r < 90 and g > 140 and b > 140), (dbz, r, g, b)
