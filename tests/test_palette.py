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


def test_rala_recording_ramp_is_continuous_and_not_olive():
    """Side-by-side notes: brighter greens, wider orange, magenta cores, wispy lows."""
    pal = load_palette("mpwg-rala-2026-09")
    assert pal.min_dbz == -32.0
    # James hexes that still match the recording, at the dBZ where RALA uses them.
    assert _rgba(pal, 20.0) == (15, 163, 61, 255)  # #0FA33D
    assert _rgba(pal, 25.0) == (50, 201, 75, 255)  # #32C94B
    assert _rgba(pal, 30.0) == (244, 242, 13, 255)  # #F4F20D
    assert _rgba(pal, 44.0) == (245, 138, 22, 255)  # #F58A16
    assert _rgba(pal, 60.0) == (229, 42, 174, 255)  # #E52AAE magenta, not dark red
    assert _rgba(pal, 65.0) == (181, 42, 203, 255)
    assert _rgba(pal, 70.0) == (120, 40, 200, 255)
    assert _rgba(pal, 75.0) == (217, 182, 255, 255)
    # 15 is brighter than the olive Clean anchor #08772E (8, 119, 46).
    r15, g15, b15, a15 = _rgba(pal, 15.0)
    assert g15 > 119 and a15 >= 220
    assert (r15, g15, b15) != (8, 119, 46)
    # Neon lift between James green and yellow, still green (not yellow yet).
    r28, g28, b28, a28 = _rgba(pal, 28.0)
    assert a28 == 255 and g28 > 220 and r28 < 180 and g28 > r28
    # 31 dBZ is between yellow (30) and gold (34), not a 5 dBZ bucket.
    y30 = np.array(_rgba(pal, 30.0), dtype=np.float64)
    gold = np.array(_rgba(pal, 34.0), dtype=np.float64)
    expected = np.clip(y30 + 0.25 * (gold - y30), 0, 255)
    got = np.array(_rgba(pal, 31.0), dtype=np.float64)
    np.testing.assert_allclose(got, expected, atol=1.5)
    assert got[0] > 200 and got[1] > 180 and got[2] < 40
    # Orange occupies a wide span; 46 dBZ is still orange, not red.
    for dbz in (36.0, 42.0, 47.0):
        r, g, b, a = _rgba(pal, dbz)
        assert a == 255 and r > 220 and 60 < g < 210 and b < 50, (dbz, r, g, b)
    red_r, red_g, red_b, _ = _rgba(pal, 57.0)
    assert red_r > 200 and red_g < 50 and red_b < 50
    # 60+ is magenta/purple, not the dark-red plateau.
    for dbz in (60.0, 62.0, 68.0):
        r, g, b, a = _rgba(pal, dbz)
        assert a == 255 and r > 140 and b > 140 and g < 120, (dbz, r, g, b)
    # Weak valid returns fade in alpha instead of a hard cutoff or a dark block.
    a_lo = _rgba(pal, -32.0)[3]
    a0 = _rgba(pal, 0.0)[3]
    a8 = _rgba(pal, 8.0)[3]
    assert 0 < a_lo < a0 < a8 < 255
    assert _rgba(pal, -32.1) == (0, 0, 0, 0)
    # Continuous, not nearest-step: 21 dBZ is not identical to 20 or 25.
    c20 = _rgba(pal, 20.0)
    c21 = _rgba(pal, 21.0)
    c25 = _rgba(pal, 25.0)
    assert c21 != c20 and c21 != c25


def test_rala_ramp_has_no_cyan_and_low_end_is_a_wisp():
    pal = load_palette("mpwg-rala-2026-09")
    for dbz in np.linspace(-32.0, 75.0, 216):
        r, g, b, a = _rgba(pal, float(dbz))
        assert a > 0
        assert not (r < 90 and g > 140 and b > 140), (dbz, r, g, b)
    # Very light rain reads as green, not near-black, and is not fully opaque.
    r, g, b, a = _rgba(pal, -5.0)
    assert a < 200
    assert g > 180 and g > r and g > b
