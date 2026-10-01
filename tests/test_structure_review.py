"""STRUCTURE V1 / FIX2 gated review cook. Production flag-off stays p3l."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from mpwg_radar.config import (
    RALA_DBZ_INTERP_FIX2,
    RALA_DBZ_INTERP_STRUCTURE_V1,
    CookerConfig,
    normalize_rala_dbz_interp,
)
from mpwg_radar.cooker import cook
from mpwg_radar.geo import CENTRAL_TEXAS, tile_bounds
from mpwg_radar.grib import ReflectivityFrame
from mpwg_radar.palette import load_palette
from mpwg_radar.products import CAT_NO_ECHO, CAT_VALID, RALA
from mpwg_radar.publish import _upload_group
from mpwg_radar.structure_recon import (
    CLUSTER_AMP_BOOST,
    CORE_SAMPLES_PER_CELL,
    GAUSS_SIGMA_CELLS,
    QUANT_STEP_DBZ,
    SINGLETON_AMP_SCALE,
    count_no_echo_violations,
    offline_continuous_at,
    prepare_structure_field,
    reconstruct_dense,
    residual_d2_floor,
    sample_structure_dbz,
)
from mpwg_radar.tiles import SPATIAL_REVISION, render_tile


def test_normalize_review_tokens_and_reject_unknown():
    assert normalize_rala_dbz_interp(None) == ""
    assert normalize_rala_dbz_interp("") == ""
    assert normalize_rala_dbz_interp("off") == ""
    assert normalize_rala_dbz_interp("p3l") == ""
    assert normalize_rala_dbz_interp(" structure-v1 ") == RALA_DBZ_INTERP_STRUCTURE_V1
    assert normalize_rala_dbz_interp("FIX2") == RALA_DBZ_INTERP_FIX2
    with pytest.raises(ValueError):
        normalize_rala_dbz_interp("monotone_pchip")
    with pytest.raises(ValueError):
        normalize_rala_dbz_interp("bilinear_peak_hold")


def test_locks_match_offline_acceptance():
    assert GAUSS_SIGMA_CELLS == 0.55
    assert QUANT_STEP_DBZ == 0.5
    assert CLUSTER_AMP_BOOST == 1.55
    assert SINGLETON_AMP_SCALE == 0.25
    assert residual_d2_floor(CORE_SAMPLES_PER_CELL) == pytest.approx((1.0 / 32.0) ** 2)
    field = prepare_structure_field(
        np.zeros((4, 4)),
        np.full((4, 4), CAT_NO_ECHO, dtype=np.uint8),
        "structure-v1",
        samples_per_cell=CORE_SAMPLES_PER_CELL,
    )
    assert field.kind == "structure_v1"
    assert field.trend_params["gaussian_sigma_cells"] == 0.55
    assert field.trend_params["soft_floor_mode"] == "no_special"
    assert field.hybrid_params["residual_soft_mode"] == "continuous_add"
    assert field.hybrid_params["cluster_amp_boost"] == 1.55
    assert field.hybrid_params["singleton_amp_scale"] == 0.25
    fix2 = prepare_structure_field(
        np.zeros((4, 4)),
        np.full((4, 4), CAT_NO_ECHO, dtype=np.uint8),
        "fix2",
    )
    assert fix2.hybrid_params["cluster_amp_boost"] == 1.0
    assert fix2.hybrid_params["singleton_amp_scale"] == 1.0
    assert fix2.trend_params["gaussian_sigma_cells"] == field.trend_params["gaussian_sigma_cells"]


def _core_fixture():
    dbz = np.full((14, 16), 24.0, dtype=np.float64)
    cat = np.full(dbz.shape, CAT_VALID, dtype=np.uint8)
    dbz[5:8, 6:9] = np.array(
        [[40.0, 52.0, 44.0], [48.0, 61.0, 50.0], [42.0, 55.0, 43.0]]
    )
    cat[:, -2:] = CAT_NO_ECHO
    dbz[:, -2:] = np.nan
    cat[11, 3] = CAT_NO_ECHO
    dbz[11, 3] = np.nan
    return dbz, cat


def test_batch_matches_offline_point_and_no_echo_stays_closed():
    dbz, cat = _core_fixture()
    field = prepare_structure_field(dbz, cat, "structure_v1", samples_per_cell=8)
    pts = [(5.2, 6.4), (6.0, 7.0), (2.2, 2.4), (11.0, 3.0), (8.4, 13.2), (6.7, 8.1)]
    vals, cats = sample_structure_dbz(
        field,
        np.array([p[0] for p in pts]),
        np.array([p[1] for p in pts]),
        quantize=False,
    )
    for (rf, cf), value, category in zip(pts, vals, cats):
        offline, offline_cat = offline_continuous_at(field, rf, cf)
        assert int(category) == int(offline_cat)
        if np.isfinite(value) or np.isfinite(offline):
            assert value == pytest.approx(offline, abs=1e-8)

    for kind in ("fix2", "structure_v1"):
        dense, dense_cat, _field = reconstruct_dense(
            dbz, cat, kind, samples_per_cell=4, quantize=False
        )
        assert count_no_echo_violations(dense_cat, cat, 4) == 0
        assert not np.any((dense_cat != CAT_VALID) & np.isfinite(dense))


def test_structure_exceeds_fix2_on_the_coherent_core():
    dbz, cat = _core_fixture()
    fix2, _fix_cat, _ = reconstruct_dense(dbz, cat, "fix2", samples_per_cell=4, quantize=False)
    structure, _st_cat, _ = reconstruct_dense(
        dbz, cat, "structure_v1", samples_per_cell=4, quantize=False
    )
    # Native peak sits at row 6, col 7. Dense index is native * spc.
    peak = (6 * 4, 7 * 4)
    assert structure[peak] > fix2[peak]
    assert not np.allclose(
        np.nan_to_num(structure), np.nan_to_num(fix2), atol=1e-6, equal_nan=True
    )
    q_fix, _, _ = reconstruct_dense(dbz, cat, "fix2", samples_per_cell=4, quantize=True)
    q_st, q_cat, _ = reconstruct_dense(
        dbz, cat, "structure_v1", samples_per_cell=4, quantize=True
    )
    assert count_no_echo_violations(q_cat, cat, 4) == 0
    finite = np.isfinite(q_st)
    # Display bands are multiples of the locked 0.5 step.
    assert np.all(np.abs(q_st[finite] / QUANT_STEP_DBZ - np.rint(q_st[finite] / QUANT_STEP_DBZ)) < 1e-9)
    assert q_st[peak] >= q_fix[peak]


def _empty_frame() -> ReflectivityFrame:
    return ReflectivityFrame(
        dbz=np.full((6, 6), np.nan, dtype=np.float32),
        lat=np.linspace(31.0, 30.95, 6),
        lon=np.linspace(-98.0, -97.95, 6),
        valid_time=datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc),
        product=RALA.mrms_name,
        source="synthetic",
        category=np.full((6, 6), CAT_NO_ECHO, dtype=np.uint8),
    )


def _rala_cfg(tmp_path: Path, interp: str = "") -> CookerConfig:
    return CookerConfig(
        bbox=CENTRAL_TEXAS,
        region_name="central-texas",
        modes=["clean"],
        min_zoom=6,
        max_zoom=6,
        tile_size=512,
        skip_empty_tiles=True,
        keep_dbz=False,
        data_dir=tmp_path / "data",
        output_dir=tmp_path,
        upload=False,
        product_id="rala",
        rala_dbz_interp=interp,
    )


def test_flag_off_cook_stays_on_rala_clean(tmp_path: Path):
    result = cook(_rala_cfg(tmp_path), source="synthetic", upload=False, loaded_frame=_empty_frame())
    assert result["product_id"] == "rala"
    frame_path = tmp_path / "radar" / "rala" / "clean" / "latest" / "frame.json"
    meta = json.loads(frame_path.read_text())
    assert meta["mode_spec"]["spatial"] == "p3l"
    assert meta["palette"]["version"] == "2026-09-rala-p3k"
    assert "dbz_interp" not in meta["mode_spec"]
    assert not (tmp_path / "radar" / "rala-review").exists()
    manifest = json.loads((tmp_path / "radar" / "manifest.json").read_text())
    assert manifest["products"]["rala"]["latest"] == "rala/clean/latest/{z}/{x}/{y}.png"
    assert "review" not in manifest["products"]["rala"]


def test_review_prefixes_do_not_replace_consumer_latest(tmp_path: Path):
    frame = _empty_frame()
    cook(_rala_cfg(tmp_path), source="synthetic", upload=False, loaded_frame=frame)
    cook(
        _rala_cfg(tmp_path, "structure_v1"),
        source="synthetic",
        upload=False,
        loaded_frame=frame,
    )
    cook(
        _rala_cfg(tmp_path, "fix2"),
        source="synthetic",
        upload=False,
        loaded_frame=frame,
    )
    manifest = json.loads((tmp_path / "radar" / "manifest.json").read_text())
    rala = manifest["products"]["rala"]
    assert rala["latest"] == "rala/clean/latest/{z}/{x}/{y}.png"
    assert rala["review"]["label"] == "NOT PRODUCTION"
    assert rala["review"]["dbz_interp"] == "fix2"
    assert rala["review"]["spatial"] == "p3l"
    assert rala["review"]["latest"] == "rala-review/fix2/clean/latest/{z}/{x}/{y}.png"
    assert rala["reviews"]["structure_v1"]["latest"] == (
        "rala-review/structure_v1/clean/latest/{z}/{x}/{y}.png"
    )
    assert rala["reviews"]["fix2"]["dbz_interp"] == "fix2"
    assert rala["reviews"]["structure_v1"]["label"] == "NOT PRODUCTION"
    consumer = json.loads((tmp_path / "radar" / "rala" / "clean" / "latest" / "frame.json").read_text())
    assert "dbz_interp" not in consumer["mode_spec"]
    assert consumer["mode_spec"]["spatial"] == SPATIAL_REVISION
    review = json.loads(
        (tmp_path / "radar" / "rala-review" / "structure_v1" / "clean" / "latest" / "frame.json").read_text()
    )
    assert review["mode_spec"]["dbz_interp"] == "structure_v1"
    assert review["mode_spec"]["spatial"] == "p3l"
    assert review["palette"]["version"] == "2026-09-rala-p3k"
    assert review["max_zoom"] == 6


def test_side_directory_review_does_not_create_consumer_tiles(tmp_path: Path):
    cook(
        _rala_cfg(tmp_path, "structure-v1"),
        source="synthetic",
        upload=False,
        loaded_frame=_empty_frame(),
    )
    assert not (tmp_path / "radar" / "rala" / "clean" / "latest" / "frame.json").is_file()
    meta = json.loads(
        (tmp_path / "radar" / "rala-review" / "structure_v1" / "clean" / "latest" / "frame.json").read_text()
    )
    assert meta["tile_url_template"] == (
        "rala-review/structure_v1/clean/latest/{z}/{x}/{y}.png"
    )
    manifest = json.loads((tmp_path / "radar" / "manifest.json").read_text())
    assert manifest["products"]["rala"]["latest"] == "rala/clean/latest/{z}/{x}/{y}.png"
    assert manifest["products"]["rala"]["review"]["dbz_interp"] == "structure_v1"


def test_upload_groups_keep_review_off_the_production_prefix():
    modes = {"clean"}
    assert (
        _upload_group(
            "rala-review/structure_v1/clean/FRAME/6/1/2.png",
            "FRAME",
            modes,
            "rala",
        )
        is None
    )
    assert (
        _upload_group(
            "rala/clean/FRAME/6/1/2.png",
            "FRAME",
            modes,
            "rala",
            tile_prefix="rala-review/structure_v1",
        )
        is None
    )
    assert (
        _upload_group(
            "rala-review/fix2/clean/FRAME/6/1/2.png",
            "FRAME",
            modes,
            "rala",
            tile_prefix="rala-review/structure_v1",
        )
        is None
    )
    assert (
        _upload_group(
            "rala-review/structure_v1/clean/FRAME/6/1/2.png",
            "FRAME",
            modes,
            "rala",
            tile_prefix="rala-review/structure_v1",
        )
        == "frame"
    )
    assert (
        _upload_group(
            "rala-review/fix2/clean/latest/frame.json",
            "FRAME",
            modes,
            "rala",
            tile_prefix="rala-review/fix2",
        )
        == "latest"
    )


def test_review_tile_changes_dbz_and_keeps_p3l_footprint():
    bounds = tile_bounds(8, 58, 104)
    d = 0.01
    lat = np.arange(bounds.north, bounds.south - d, -d)
    lon = np.arange(bounds.west, bounds.east + d, d)
    # Keep the fixture small: the tile test only needs a neighborhood.
    lat = lat[:24]
    lon = lon[:24]
    dbz = np.full((lat.size, lon.size), 18.0, dtype=np.float32)
    cat = np.full(dbz.shape, CAT_VALID, dtype=np.uint8)
    dbz[8:12, 8:12] = 55.0
    cat[0, :] = CAT_NO_ECHO
    dbz[0, :] = np.nan
    frame = ReflectivityFrame(
        dbz=dbz,
        lat=lat.astype(np.float64),
        lon=lon.astype(np.float64),
        valid_time=datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc),
        product=RALA.mrms_name,
        source="synthetic",
        category=cat,
    )
    palette = load_palette("mpwg-rala-2026-09")
    plain, _ = render_tile(frame, palette, 8, 58, 104, tile_size=32, sample_mode="masked-splat")
    field = prepare_structure_field(dbz, cat, "structure_v1")
    review, _ = render_tile(
        frame,
        palette,
        8,
        58,
        104,
        tile_size=32,
        sample_mode="masked-splat",
        dbz_interp="structure_v1",
        structure_field=field,
    )
    plain_px = np.asarray(plain)
    review_px = np.asarray(review)
    assert plain_px.shape == (32, 32, 4)
    assert review_px.shape == plain_px.shape
    assert not np.array_equal(plain_px, review_px)
    # NO-ECHO / clear-air stays fully transparent. Review may drop extra
    # pixels where the recon is non-VALID, and must not paint where the
    # production splat was already transparent.
    assert np.all(review_px[..., 3][plain_px[..., 3] == 0] == 0)


def test_render_without_flag_matches_production_splat():
    bounds = tile_bounds(8, 58, 104)
    lat = np.arange(bounds.north, bounds.north - 0.2, -0.01)
    lon = np.arange(bounds.west, bounds.west + 0.2, 0.01)
    dbz = np.full((lat.size, lon.size), 30.0, dtype=np.float32)
    cat = np.full(dbz.shape, CAT_VALID, dtype=np.uint8)
    frame = ReflectivityFrame(
        dbz=dbz,
        lat=lat.astype(np.float64),
        lon=lon.astype(np.float64),
        valid_time=datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc),
        product=RALA.mrms_name,
        source="synthetic",
        category=cat,
    )
    palette = load_palette("mpwg-rala-2026-09")
    kwargs = dict(tile_size=16, sample_mode="masked-splat")
    base, _ = render_tile(frame, palette, 8, 58, 104, **kwargs)
    flagged, _ = render_tile(frame, palette, 8, 58, 104, dbz_interp="", **kwargs)
    assert np.array_equal(np.asarray(base), np.asarray(flagged))
