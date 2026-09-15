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
        def put_object(self, **kwargs):
            body = kwargs.get("Body")
            if hasattr(body, "read"):
                body.read()
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
    publisher.workers = 1
    publisher.timeout_seconds = 30
    key = publisher.upload_file(png, "clean/latest/0/1/2.png")
    assert key == "radar/clean/latest/0/1/2.png"
    assert uploaded[0]["Bucket"] == "mpwg-radar"
    assert uploaded[0]["ContentType"] == "image/png"


def test_cook_calls_incremental_upload_cook(tmp_path: Path, monkeypatch):
    calls = []

    class FakePublisher:
        def __init__(self, cfg, **kwargs):
            calls.append({"cfg": cfg, "kwargs": kwargs})

        def upload_cook(self, radar_root, *, frame_id, modes, all_frames=False):
            calls.append(
                {
                    "radar_root": Path(radar_root),
                    "frame_id": frame_id,
                    "modes": list(modes),
                    "all_frames": all_frames,
                }
            )
            return ["radar/manifest.json"]

        def delete_prefix(self, relative_prefix: str) -> int:
            calls.append({"delete": relative_prefix})
            return 0

    monkeypatch.setattr("mpwg_radar.cooker.R2Publisher", FakePublisher)
    cfg = CookerConfig(
        bbox=CENTRAL_TEXAS,
        region_name="central-texas",
        modes=["clean"],
        min_zoom=6,
        max_zoom=7,
        tile_size=512,
        skip_empty_tiles=True,
        keep_dbz=False,
        data_dir=tmp_path / "data",
        output_dir=tmp_path,
        upload=True,
        upload_all_frames=False,
        r2=R2Config(
            account_id="abc",
            access_key_id="AKIAEXAMPLE",
            secret_access_key="secret",
            bucket="mpwg-radar",
            upload_workers=4,
            upload_timeout_seconds=120,
        ),
    )
    result = cook(cfg, source="synthetic", upload=True)
    upload_call = next(c for c in calls if "frame_id" in c)
    assert upload_call["frame_id"] == result["frame_id"]
    assert upload_call["modes"] == ["clean"]
    assert upload_call["all_frames"] is False
    assert upload_call["radar_root"] == tmp_path / "radar"
    assert result["uploaded"] == 1
    init = next(c for c in calls if "kwargs" in c)
    assert init["kwargs"]["workers"] == 4
    assert init["kwargs"]["timeout_seconds"] == 120
