"""No-echo vs missing vs weak valid dBZ — physical grid, then colorize."""

from __future__ import annotations

import numpy as np

from mpwg_radar.grib import classify_dbz, mask_fill
from mpwg_radar.palette import load_palette
from mpwg_radar.products import CAT_MISSING, CAT_NO_ECHO, CAT_VALID
from mpwg_radar.qc import apply_mode, threshold
from mpwg_radar.grib import ReflectivityFrame
from datetime import datetime, timezone


def _frame(dbz, category=None):
    ny, nx = np.asarray(dbz).shape
    return ReflectivityFrame(
        dbz=np.asarray(dbz, dtype=np.float32),
        lat=np.linspace(31.0, 30.8, ny, dtype=np.float32),
        lon=np.linspace(-98.0, -97.8, nx, dtype=np.float32),
        valid_time=datetime(2026, 9, 21, 12, 0, tzinfo=timezone.utc),
        product="test",
        category=category,
    )


def test_classify_three_categories():
    raw = np.array(
        [
            [-999.0, -99.0, 0.5, 5.0, 22.0, -3.0],
        ],
        dtype=np.float32,
    )
    dbz, cat = classify_dbz(raw)
    assert cat[0, 0] == CAT_MISSING  # no coverage
    assert cat[0, 1] == CAT_NO_ECHO  # NSSL Missing
    assert cat[0, 2] == CAT_VALID
    assert cat[0, 3] == CAT_VALID
    assert cat[0, 4] == CAT_VALID
    assert cat[0, 5] == CAT_MISSING  # GRIB2 no-coverage variant
    assert np.isnan(dbz[0, 0]) and np.isnan(dbz[0, 1]) and np.isnan(dbz[0, 5])
    assert dbz[0, 2] == np.float32(0.5)
    assert dbz[0, 3] == np.float32(5.0)
    assert dbz[0, 4] == np.float32(22.0)


def test_minus_99_is_no_echo_not_swallowed_by_physical_floor():
    """-99 < -32, but it is NSSL Missing (no-echo), not no-coverage."""
    raw = np.array([[-99.0, -32.5, -10.0]], dtype=np.float32)
    dbz, cat = classify_dbz(raw)
    assert cat[0, 0] == CAT_NO_ECHO
    assert cat[0, 1] == CAT_MISSING  # below physical range, not the -99 sentinel
    assert cat[0, 2] == CAT_VALID
    assert dbz[0, 2] == np.float32(-10.0)


def test_mask_fill_still_nans_both_sentinels():
    arr = np.array([[-999.0, -99.0, 12.0]], dtype=np.float32)
    out = mask_fill(arr)
    assert np.isnan(out[0, 0]) and np.isnan(out[0, 1])
    assert out[0, 2] == 12.0


def test_missing_does_not_render_as_green_even_if_dbz_is_zero():
    """If missing were collapsed to 0 dBZ, the RALA ramp would paint green."""
    pal = load_palette("mpwg-rala-2026-09")
    assert pal.min_dbz == -32.0
    dbz = np.array([[0.0, 0.0, 5.0]], dtype=np.float32)
    cat = np.array([[CAT_MISSING, CAT_NO_ECHO, CAT_VALID]], dtype=np.uint8)
    rgba = pal.colorize(dbz, category=cat)
    assert tuple(int(c) for c in rgba[0, 0]) == (0, 0, 0, 0)
    assert tuple(int(c) for c in rgba[0, 1]) == (0, 0, 0, 0)
    assert rgba[0, 2, 3] == 255  # weak valid is painted


def test_missing_without_mask_would_be_green_at_display_min_zero():
    pal = load_palette("mpwg-rala-2026-09")
    rgba = pal.colorize(np.array([[0.0]], dtype=np.float32))
    assert rgba[0, 0, 3] == 255  # why the mask is required


def test_weak_valid_dbz_not_blanked_by_rala_qc():
    dbz = np.full((12, 12), np.nan, dtype=np.float32)
    dbz[2:10, 2:10] = 5.0  # large enough to survive despeckle
    cat = np.full(dbz.shape, CAT_NO_ECHO, dtype=np.uint8)
    cat[np.isfinite(dbz)] = CAT_VALID
    frame = _frame(dbz, cat)
    rala = apply_mode(frame, "clean", apply_dbz_floor=False)
    assert np.nanmin(rala.dbz) < 10.0
    assert (rala.category == CAT_VALID).sum() >= 8
    composite = apply_mode(frame, "clean", apply_dbz_floor=True)
    finite = composite.dbz[np.isfinite(composite.dbz)]
    if finite.size:
        assert finite.min() >= 10.0 - 1e-3
    else:
        assert True  # 5 dBZ blob fully dropped by composite Clean floor


def test_composite_clean_floor_unchanged():
    dbz = np.array([[9.9, 12.0, 15.0]], dtype=np.float32)
    kept, cat = threshold(dbz, 10.0, category=np.array([[CAT_VALID] * 3], dtype=np.uint8))
    assert np.isnan(kept[0, 0])
    assert kept[0, 1] == 12.0
    assert cat[0, 0] == CAT_NO_ECHO  # hidden clutter, not missing
    pal = load_palette("mpwg-clean-2026-09")
    rgba = pal.colorize(kept, category=cat)
    assert tuple(int(c) for c in rgba[0, 1]) == (0, 0, 0, 0)  # 12 < 15 display
    assert rgba[0, 2, 3] == 255


def test_rala_palette_colors_values_below_15():
    pal = load_palette("mpwg-rala-2026-09")
    rgba = pal.colorize(np.array([[5.0]], dtype=np.float32))
    assert rgba[0, 0, 3] == 255
    clean = load_palette("mpwg-clean-2026-09")
    assert tuple(int(c) for c in clean.colorize(np.array([[5.0]], dtype=np.float32))[0, 0]) == (
        0,
        0,
        0,
        0,
    )
