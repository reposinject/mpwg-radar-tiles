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


def test_load_config_defaults(monkeypatch):
    monkeypatch.delenv("MPWG_REGION", raising=False)
    monkeypatch.delenv("REGION", raising=False)
    monkeypatch.delenv("MPWG_BBOX", raising=False)
    monkeypatch.delenv("BBOX", raising=False)
    monkeypatch.delenv("MPWG_MIN_ZOOM", raising=False)
    monkeypatch.delenv("MPWG_MAX_ZOOM", raising=False)
    monkeypatch.delenv("MPWG_UPLOAD_WORKERS", raising=False)
    monkeypatch.delenv("MPWG_UPLOAD_TIMEOUT_SECONDS", raising=False)
    monkeypatch.delenv("MPWG_UPLOAD_ALL_FRAMES", raising=False)
    monkeypatch.delenv("MPWG_R2_CONNECT_TIMEOUT", raising=False)
    monkeypatch.delenv("MPWG_R2_READ_TIMEOUT", raising=False)
    monkeypatch.delenv("MPWG_RETENTION_FRAMES", raising=False)
    cfg = load_config()
    assert cfg.region_name == "conus"
    assert cfg.bbox == CONUS
    assert cfg.min_zoom == 6
    assert cfg.max_zoom == 8
    assert cfg.retention_frames == 30
    assert cfg.upload_workers == 4
    assert cfg.upload_timeout_seconds == 120
    assert cfg.upload_all_frames is False
    assert cfg.r2.connect_timeout == 10
    assert cfg.r2.read_timeout == 30


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


def test_load_config_upload_knobs(monkeypatch):
    monkeypatch.setenv("MPWG_RETENTION_FRAMES", "12")
    monkeypatch.setenv("MPWG_UPLOAD_WORKERS", "6")
    monkeypatch.setenv("MPWG_UPLOAD_TIMEOUT_SECONDS", "90")
    monkeypatch.setenv("MPWG_R2_CONNECT_TIMEOUT", "8")
    monkeypatch.setenv("MPWG_R2_READ_TIMEOUT", "20")
    monkeypatch.setenv("MPWG_UPLOAD_ALL_FRAMES", "true")
    cfg = load_config()
    assert cfg.retention_frames == 12
    assert cfg.upload_workers == 6
    assert cfg.upload_timeout_seconds == 90
    assert cfg.r2.connect_timeout == 8
    assert cfg.r2.read_timeout == 20
    assert cfg.upload_all_frames is True


def test_upload_workers_clamped(monkeypatch):
    monkeypatch.setenv("MPWG_UPLOAD_WORKERS", "99")
    cfg = load_config()
    assert cfg.upload_workers == 8


def test_unknown_region_raises():
    with pytest.raises(ValueError, match="Unknown region"):
        resolve_region_bbox("europe")
