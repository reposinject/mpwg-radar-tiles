from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from datetime import datetime, timezone

from mpwg_radar.config import CookerConfig, R2Config
from mpwg_radar.cooker import cook
from mpwg_radar.geo import CENTRAL_TEXAS
from mpwg_radar.publish import R2Publisher
from mpwg_radar.synthetic import synthetic_central_texas


def test_synthetic_cook_writes_manifest_and_512_tiles(tmp_path: Path):
    cfg = CookerConfig(
        bbox=CENTRAL_TEXAS,
        region_name="central-texas",
        modes=["clean"],
        min_zoom=6,
        max_zoom=7,
        tile_size=512,
        skip_empty_tiles=True,
        keep_dbz=True,
        data_dir=tmp_path / "data",
        output_dir=tmp_path,
        upload=False,
    )
    result = cook(cfg, source="synthetic", upload=False)
    assert result["frame_id"]
    manifest = json.loads((tmp_path / "radar" / "manifest.json").read_text())
    assert manifest["tile_size"] == 512
    assert manifest["default_mode"] == "clean"
    assert "clean" in manifest["modes"]
    assert manifest["palette"]["display_min_dbz"] == 15
    assert manifest["palette"]["stops"][0]["dbz"] == 15
    assert manifest["palette"]["stops"][0]["rgba"] == [8, 119, 46, 255]
    tiles = list((tmp_path / "radar" / "clean").rglob("*.png"))
    assert tiles
    from PIL import Image

    with Image.open(tiles[0]) as im:
        assert im.size == (512, 512)
    dbz_path = tmp_path / "dbz" / f"{result['frame_id']}.npz"
    assert dbz_path.is_file()
    stored = np.load(dbz_path)
    finite = stored["dbz"][np.isfinite(stored["dbz"])]
    assert finite.size > 0
    assert float(finite.min()) < 15.0  # display cutoff must not discard the crop

    with Image.open(tmp_path / "radar" / "colorbar.png") as bar:
        arr = np.array(bar)
        assert tuple(int(c) for c in arr[0, 0]) == (8, 119, 46, 255)


def test_default_conus_cook_writes_conus_manifest(tmp_path: Path):
    cfg = CookerConfig(
        data_dir=tmp_path / "data",
        output_dir=tmp_path,
        upload=False,
        skip_empty_tiles=True,
        keep_dbz=False,
        min_zoom=6,
        max_zoom=7,
    )
    result = cook(cfg, source="synthetic", upload=False)
    assert result["region"] == "conus"
    manifest = json.loads((tmp_path / "radar" / "manifest.json").read_text())
    assert manifest["region"] == "conus"
    assert manifest["default_product"] == "composite"
    assert "composite" in manifest["products"]
    assert "rala" in manifest["products"]
    assert manifest["products"]["composite"]["mrms_product"] == "MergedReflectivityQCComposite"
    assert manifest["products"]["rala"]["mrms_product"] == "ReflectivityAtLowestAltitude"
    assert manifest["products"]["composite"]["default"] is True
    assert manifest["bbox"]["west"] == pytest.approx(-130.0)
    assert manifest["bbox"]["north"] == pytest.approx(55.0)
    assert manifest["min_zoom"] == 6
    assert manifest["max_zoom"] == 7
    assert manifest["tile_size"] == 512


def test_r2_publisher_uses_boto3_when_env_set(tmp_path: Path):
    uploaded = []

    class FakeClient:
        def put_object(self, **kwargs):
            uploaded.append(kwargs)

    png = tmp_path / "0" / "1" / "2.png"
    png.parent.mkdir(parents=True)
    png.write_bytes(b"\x89PNG\r\n")
    cfg = R2Config(
        account_id="abc",
        access_key_id="AKIAEXAMPLE",
        secret_access_key="secret",
        bucket="mpwg-radar",
        prefix="radar",
    )
    publisher = R2Publisher.__new__(R2Publisher)
    publisher.cfg = cfg
    publisher.client = FakeClient()
    publisher.bucket = cfg.bucket
    publisher.prefix = cfg.prefix
    publisher.connect_timeout = 10
    publisher.read_timeout = 30
    publisher.upload_timeout = 180
    publisher.object_timeout = 60
    publisher.concurrency = 2
    publisher.max_attempts = 2
    key = publisher.upload_file(png, "clean/latest/0/1/2.png")
    assert key == "radar/clean/latest/0/1/2.png"
    assert uploaded[0]["Bucket"] == "mpwg-radar"
    assert uploaded[0]["ContentType"] == "image/png"


def test_cook_uploads_only_new_frame_not_retention(tmp_path: Path, monkeypatch):
    puts: list[str] = []

    class FakeClient:
        def put_object(self, **kwargs):
            puts.append(kwargs["Key"])

        def get_paginator(self, _name):
            return type("P", (), {"paginate": lambda self, **kw: []})()

    def fake_init(self, cfg):
        self.cfg = cfg
        self.client = FakeClient()
        self.bucket = cfg.bucket
        self.prefix = cfg.prefix.strip("/")
        self.connect_timeout = cfg.connect_timeout_seconds
        self.read_timeout = cfg.read_timeout_seconds
        self.upload_timeout = cfg.upload_timeout_seconds
        self.object_timeout = cfg.object_timeout_seconds
        self.concurrency = max(1, cfg.upload_concurrency)
        self.max_attempts = cfg.max_attempts

    monkeypatch.setattr(R2Publisher, "__init__", fake_init)

    times = [
        datetime(2026, 9, 15, 16, 0, 0, tzinfo=timezone.utc),
        datetime(2026, 9, 15, 16, 36, 41, tzinfo=timezone.utc),
    ]

    def fake_load(cfg, source, grib_path):
        return synthetic_central_texas(valid_time=times.pop(0))

    monkeypatch.setattr("mpwg_radar.cooker._load_frame", fake_load)

    cfg = CookerConfig(
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
        upload=True,
        retention_frames=5,
        r2=R2Config(
            account_id="abc",
            access_key_id="AKIAEXAMPLE",
            secret_access_key="secret",
            bucket="mpwg-radar",
            prefix="radar",
        ),
    )
    first = cook(cfg, source="synthetic", upload=True)
    assert first["frame_id"] == "20260915T160000Z"
    assert any("20260915T160000Z" in key for key in puts)
    puts.clear()

    second = cook(cfg, source="synthetic", upload=True)
    assert second["frame_id"] == "20260915T163641Z"
    assert second["uploaded"] == len(puts)
    assert second["upload_skipped"] >= 1
    assert any("20260915T163641Z" in key for key in puts)
    assert any("/latest/" in key for key in puts)
    assert any(key.endswith("manifest.json") for key in puts)
    assert all("20260915T160000Z" not in key for key in puts)


def test_rala_cook_writes_prefixed_tiles_and_keeps_composite(tmp_path: Path):
    composite_cfg = CookerConfig(
        bbox=CENTRAL_TEXAS,
        region_name="central-texas",
        modes=["clean"],
        min_zoom=6,
        max_zoom=6,
        tile_size=512,
        skip_empty_tiles=True,
        keep_dbz=True,
        data_dir=tmp_path / "data",
        output_dir=tmp_path,
        upload=False,
        product_id="composite",
    )
    composite = cook(composite_cfg, source="synthetic", upload=False)
    assert (tmp_path / "radar" / "clean" / "latest").is_dir()
    assert composite["product_id"] == "composite"
    assert composite["palette"] == "mpwg-clean-2026-09"

    rala_cfg = CookerConfig(
        bbox=CENTRAL_TEXAS,
        region_name="central-texas",
        modes=["clean"],
        min_zoom=6,
        max_zoom=6,
        tile_size=512,
        skip_empty_tiles=True,
        keep_dbz=True,
        data_dir=tmp_path / "data",
        output_dir=tmp_path,
        upload=False,
        product_id="rala",
    )
    rala = cook(rala_cfg, source="synthetic", upload=False)
    assert rala["product_id"] == "rala"
    assert rala["palette"] == "mpwg-rala-2026-09"
    assert rala["display_min_dbz"] == -32
    assert (tmp_path / "radar" / "rala" / "clean" / "latest").is_dir()
    assert (tmp_path / "radar" / "clean" / "latest").is_dir()  # composite untouched
    manifest = json.loads((tmp_path / "radar" / "manifest.json").read_text())
    assert manifest["default_product"] == "composite"
    assert manifest["cooked_product"] == "rala"
    assert manifest["products"]["rala"]["available"] is True
    assert manifest["products"]["composite"]["available"] is True
    assert manifest["products"]["rala"]["latest"] == "rala/clean/latest/{z}/{x}/{y}.png"
    assert manifest["modes"]["clean"]["latest"] == "clean/latest/{z}/{x}/{y}.png"
    assert manifest["palette"]["display_min_dbz"] == 15
    rala_frame = json.loads(
        (tmp_path / "radar" / "rala" / "clean" / "latest" / "frame.json").read_text()
    )
    assert rala_frame["mode_spec"]["despeckle"] is False
    assert rala_frame["mode_spec"]["sample"] == "masked-splat"
    assert rala_frame["mode_spec"]["smooth_kind"] == "masked-splat"
    assert rala_frame["valid_time"].endswith("+00:00")
    comp_frame = json.loads(
        (tmp_path / "radar" / "clean" / "latest" / "frame.json").read_text()
    )
    assert comp_frame["mode_spec"]["despeckle"] is True
    assert comp_frame["mode_spec"]["sample"] == "nearest"
    assert comp_frame["mode_spec"]["smooth_kind"] == "mild-3x3"
    tiles = list((tmp_path / "radar" / "rala" / "clean").rglob("*.png"))
    assert tiles
    from PIL import Image

    with Image.open(tiles[0]) as im:
        assert im.size == (512, 512)

