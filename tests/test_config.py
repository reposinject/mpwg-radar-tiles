from __future__ import annotations

import pytest

from mpwg_radar.config import CookerConfig, load_config, resolve_region_bbox
from mpwg_radar.geo import CENTRAL_TEXAS, CONUS, CONUS_MAX_ZOOM, CONUS_MIN_ZOOM


@pytest.fixture(autouse=True)
def _ignore_local_dotenv(monkeypatch):
    monkeypatch.setattr("mpwg_radar.config.load_dotenv", lambda path=None: None)


def test_cooker_config_defaults_are_conus_z6_8():
    cfg = CookerConfig()
    assert cfg.region_name == "conus"
    assert cfg.bbox == CONUS
    assert cfg.min_zoom == CONUS_MIN_ZOOM == 6
    assert cfg.max_zoom == CONUS_MAX_ZOOM == 8
    assert cfg.tile_size == 512
    assert cfg.modes == ["clean"]
    assert "MergedReflectivityQCComposite" in cfg.mrms_latest_url
    assert cfg.mrms_s3_prefix == "CONUS/MergedReflectivityQCComposite_00.50"
    assert cfg.product_id == "composite"
    assert cfg.product.mrms_name == "MergedReflectivityQCComposite"


def test_load_config_defaults(monkeypatch):
    monkeypatch.delenv("MPWG_REGION", raising=False)
    monkeypatch.delenv("REGION", raising=False)
    monkeypatch.delenv("MPWG_BBOX", raising=False)
    monkeypatch.delenv("BBOX", raising=False)
    monkeypatch.delenv("MPWG_MIN_ZOOM", raising=False)
    monkeypatch.delenv("MPWG_MAX_ZOOM", raising=False)
    monkeypatch.delenv("MPWG_PRODUCT", raising=False)
    monkeypatch.delenv("MPWG_PALETTE", raising=False)
    monkeypatch.delenv("MPWG_UPLOAD_TIMEOUT", raising=False)
    cfg = load_config()
    assert cfg.region_name == "conus"
    assert cfg.bbox == CONUS
    assert cfg.min_zoom == 6
    assert cfg.max_zoom == 8
    assert cfg.product_id == "composite"
    assert cfg.r2.upload_timeout_seconds == 900


def test_load_config_product_rala_ignores_leftover_composite_url(monkeypatch):
    monkeypatch.setenv("MPWG_PRODUCT", "rala")
    monkeypatch.setenv(
        "MRMS_LATEST_URL",
        "https://mrms.ncep.noaa.gov/2D/MergedReflectivityQCComposite/"
        "MRMS_MergedReflectivityQCComposite.latest.grib2.gz",
    )
    monkeypatch.setenv("MRMS_S3_PREFIX", "CONUS/MergedReflectivityQCComposite_00.50")
    cfg = load_config()
    assert cfg.product_id == "rala"
    assert "ReflectivityAtLowestAltitude" in cfg.mrms_latest_url
    assert cfg.mrms_s3_prefix == "CONUS/ReflectivityAtLowestAltitude_00.50"
    assert cfg.palette_id == "mpwg-rala-2026-09"


def test_rala_retention_ignores_shared_frame_cap(monkeypatch):
    monkeypatch.setenv("MPWG_PRODUCT", "rala")
    monkeypatch.setenv("MPWG_RETENTION_FRAMES", "5")
    cfg = load_config()
    assert cfg.retention_frames == 5
    assert cfg.rala_retention_frames == 60
    assert cfg.rala_retention_minutes == 75
    assert cfg.rala_catchup_budget_seconds == 1500
    monkeypatch.setenv("MPWG_RALA_RETENTION_FRAMES", "48")
    monkeypatch.setenv("MPWG_RALA_RETENTION_MINUTES", "90")
    monkeypatch.setenv("MPWG_RALA_CATCHUP_BUDGET_SECONDS", "600")
    tuned = load_config()
    assert tuned.rala_retention_frames == 48
    assert tuned.rala_retention_minutes == 90
    assert tuned.rala_catchup_budget_seconds == 600
    assert tuned.retention_frames == 5


def test_cooker_config_product_rala_syncs_endpoints():
    cfg = CookerConfig(product_id="rala")
    assert "ReflectivityAtLowestAltitude" in cfg.mrms_latest_url
    assert cfg.palette_id == "mpwg-rala-2026-09"
    assert cfg.product.apply_dbz_floor is False


def test_load_config_region_alias_and_central_texas(monkeypatch):
    monkeypatch.setenv("REGION", "central-texas")
    monkeypatch.delenv("MPWG_REGION", raising=False)
    cfg = load_config()
    assert cfg.region_name == "central-texas"
    assert cfg.bbox == CENTRAL_TEXAS


def test_load_config_bbox_override(monkeypatch):
    monkeypatch.setenv("MPWG_REGION", "conus")
    monkeypatch.setenv("MPWG_BBOX", "-125,24,-66.5,49.5")
    cfg = load_config()
    assert cfg.region_name == "conus"
    assert cfg.bbox.west == -125.0
    assert cfg.bbox.south == 24.0
    assert cfg.bbox.east == -66.5
    assert cfg.bbox.north == 49.5


def test_unknown_region_raises():
    with pytest.raises(ValueError, match="Unknown region"):
        resolve_region_bbox("europe")


def test_load_config_upload_timeouts(monkeypatch):
    monkeypatch.setenv("MPWG_UPLOAD_CONNECT_TIMEOUT", "8")
    monkeypatch.setenv("MPWG_UPLOAD_READ_TIMEOUT", "12")
    monkeypatch.setenv("MPWG_UPLOAD_OBJECT_TIMEOUT", "45")
    monkeypatch.setenv("MPWG_UPLOAD_TIMEOUT", "90")
    monkeypatch.setenv("MPWG_UPLOAD_CONCURRENCY", "2")
    monkeypatch.setenv("MPWG_UPLOAD_MAX_ATTEMPTS", "2")
    cfg = load_config()
    assert cfg.r2.connect_timeout_seconds == 8
    assert cfg.r2.read_timeout_seconds == 12
    assert cfg.r2.object_timeout_seconds == 45
    assert cfg.r2.upload_timeout_seconds == 90
    assert cfg.r2.upload_concurrency == 2
    assert cfg.r2.max_attempts == 2
