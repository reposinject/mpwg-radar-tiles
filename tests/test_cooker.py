from __future__ import annotations

import json
from pathlib import Path

import pytest

from mpwg_radar.config import CookerConfig, R2Config
from mpwg_radar.cooker import cook
from mpwg_radar.geo import CENTRAL_TEXAS
from mpwg_radar.publish import R2Publisher


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
    tiles = list((tmp_path / "radar" / "clean").rglob("*.png"))
    assert tiles
    from PIL import Image

    with Image.open(tiles[0]) as im:
        assert im.size == (512, 512)
    assert (tmp_path / "dbz" / f"{result['frame_id']}.npz").is_file()


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
    assert manifest["bbox"]["west"] == pytest.approx(-130.0)
    assert manifest["bbox"]["north"] == pytest.approx(55.0)
    assert manifest["min_zoom"] == 6
    assert manifest["max_zoom"] == 7
    assert manifest["tile_size"] == 512


def test_r2_publisher_uses_boto3_when_env_set(tmp_path: Path, monkeypatch):
    uploaded = []

    class FakeClient:
        def upload_file(self, local, bucket, key, ExtraArgs=None):
            uploaded.append((local, bucket, key, ExtraArgs))

    class FakeBoto3:
        def client(self, *args, **kwargs):
            return FakeClient()

    import mpwg_radar.publish as pub

    monkeypatch.setitem(__import__("sys").modules, "boto3", FakeBoto3())
    monkeypatch.setitem(
        __import__("sys").modules,
        "botocore.config",
        type("m", (), {"Config": lambda *a, **k: None}),
    )

    # Bypass real imports inside R2Publisher by injecting after construct.
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
    key = publisher.upload_file(png, "clean/latest/0/1/2.png")
    assert key == "radar/clean/latest/0/1/2.png"
    assert uploaded[0][1] == "mpwg-radar"
    assert uploaded[0][3]["ContentType"] == "image/png"
