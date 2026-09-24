"""RALA colorize audit: the production LUT, not a second painter."""

from __future__ import annotations

import numpy as np

from mpwg_radar.cli import main
from mpwg_radar.palette import (
    DIAGNOSTIC_DBZ,
    FAMILY_PROOF_PAIRS,
    color_diag_report,
    load_palette,
    trace_colorize,
)
from mpwg_radar.products import CAT_NO_ECHO, CAT_VALID


def _rgba(pal, dbz: float):
    return tuple(int(c) for c in pal.colorize(np.array([[dbz]], dtype=np.float32))[0, 0])


def test_trace_is_the_production_colorize_path():
    pal = load_palette("mpwg-rala-2026-09")
    assert pal.version == "2026-09-rala-p3f"
    assert pal._lut_step == 0.1
    for dbz in DIAGNOSTIC_DBZ:
        row = trace_colorize(pal, dbz)
        assert row["rgba"] == _rgba(pal, dbz)
        assert row["category"] == "valid"
        assert row["decoded"] == row["normalized"]
        assert abs(row["decoded"] - dbz) < 1e-5
        # Index is the 0.1 dBZ slot, not a 5 or 10 dBZ bin.
        assert row["lut_index"] == int(np.floor((row["decoded"] - pal._lut_dbz0) / 0.1 + 0.5))
        assert abs(row["bucket_dbz"] - (pal._lut_dbz0 + row["lut_index"] * 0.1)) < 1e-9
        assert row["alpha"] > 0


def test_probe_steps_are_not_one_flat_color():
    pal = load_palette("mpwg-rala-2026-09")
    rows = [trace_colorize(pal, dbz) for dbz in DIAGNOSTIC_DBZ]
    for prev, nxt in zip(rows, rows[1:]):
        assert prev["rgba"] != nxt["rgba"]
        assert nxt["lut_index"] > prev["lut_index"]


def test_ten_dbz_families_stay_distinct():
    pal = load_palette("mpwg-rala-2026-09")
    for lo, hi in FAMILY_PROOF_PAIRS:
        a = trace_colorize(pal, lo)
        b = trace_colorize(pal, hi)
        assert a["rgba"] != b["rgba"]
        assert b["lut_index"] - a["lut_index"] == int(round((hi - lo) / 0.1))
        channel = max(abs(a["rgba"][i] - b["rgba"][i]) for i in range(3))
        assert channel >= 25, (lo, hi, a["rgba"], b["rgba"])
        dist = float(np.linalg.norm(np.array(a["rgb"]) - np.array(b["rgb"])))
        assert dist >= 40, (lo, hi, dist)


def test_weak_returns_are_visible_and_not_cut_off_at_10():
    pal = load_palette("mpwg-rala-2026-09")
    alphas = {dbz: trace_colorize(pal, dbz)["alpha"] for dbz in (0.1, 1, 2, 5, 7.5, 9.9, 10, 10.1, 15, 20, 24.8)}
    assert 40 < alphas[0.1] < 160
    assert alphas[0.1] < alphas[2] < alphas[5] < alphas[7.5] < alphas[10] < alphas[15] < alphas[20]
    assert alphas[10] < 220
    assert alphas[24.8] == 255
    # Crossing 10 does not punch a hole or a step.
    assert abs(alphas[9.9] - alphas[10.1]) <= 2
    assert alphas[9.9] > 0 and alphas[10.1] > 0
    for dbz in (0.1, 1, 2, 5, 7.5):
        r, g, b, a = _rgba(pal, dbz)
        assert a > 0
        assert g > r and g > b


def test_no_echo_and_missing_are_alpha_zero():
    pal = load_palette("mpwg-rala-2026-09")
    for raw in (-99.0, -999.0, -3.0):
        row = trace_colorize(pal, raw)
        assert row["alpha"] == 0
        assert row["rgba"] == (0, 0, 0, 0)
        assert row["lut_index"] is None
        assert row["category"] in {"no-echo", "missing"}
    masked = trace_colorize(pal, 30.0, category=CAT_NO_ECHO)
    assert masked["rgba"] == (0, 0, 0, 0)
    assert masked["category"] == "no-echo"
    # A real weak return is not the sentinel path.
    kept = trace_colorize(pal, -20.0, category=CAT_VALID)
    assert kept["alpha"] > 0
    assert kept["category"] == "valid"


def test_color_diag_cli_prints_the_probe_table(capsys):
    assert main(["color-diag"]) == 0
    out = capsys.readouterr().out
    assert "2026-09-rala-p3f" in out
    assert "normalize: identity" in out
    for dbz in ("0.1", "10.0", "22.5", "52.5", "70.0"):
        assert dbz in out
    assert "22 vs 29" in out
    assert "31 vs 39" in out
    assert "41 vs 49" in out
    report = color_diag_report(load_palette("mpwg-rala-2026-09"))
    assert report in out
