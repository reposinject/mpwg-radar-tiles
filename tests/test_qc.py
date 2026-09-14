from __future__ import annotations

import numpy as np

from mpwg_radar.qc import apply_mode, remove_small_components, threshold
from mpwg_radar.synthetic import synthetic_central_texas


def test_clean_hides_below_10_and_speckles():
    frame = synthetic_central_texas()
    clean = apply_mode(frame, "clean")
    finite = clean.dbz[np.isfinite(clean.dbz)]
    assert finite.size > 50
    assert finite.min() >= 10.0 - 1e-3


def test_standard_keeps_more_light_echo_than_clean():
    frame = synthetic_central_texas()
    clean = apply_mode(frame, "clean")
    standard = apply_mode(frame, "standard")
    assert np.nanmin(clean.dbz) >= 10.0 - 1e-3
    assert np.isfinite(standard.dbz).sum() >= np.isfinite(clean.dbz).sum()
    assert not np.any(np.isfinite(clean.dbz) & (clean.dbz < 10))


def test_all_keeps_more_cells_than_clean():
    frame = synthetic_central_texas()
    clean = apply_mode(frame, "clean")
    all_mode = apply_mode(frame, "all")
    assert np.isfinite(all_mode.dbz).sum() >= np.isfinite(clean.dbz).sum()


def test_remove_small_components_drops_isolated_pixel():
    dbz = np.full((7, 7), np.nan, dtype=np.float32)
    dbz[1:4, 1:4] = 30.0  # 9-pixel blob
    dbz[6, 6] = 40.0  # speckle
    out = remove_small_components(dbz, min_size=8)
    assert np.isfinite(out[2, 2])
    assert np.isnan(out[6, 6])


def test_threshold_does_not_mutate_fill_as_zero():
    dbz = np.array([[np.nan, 4.0, 12.0]], dtype=np.float32)
    out = threshold(dbz, 10.0)
    assert np.isnan(out[0, 0]) and np.isnan(out[0, 1])
    assert out[0, 2] == 12.0
