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
    cfg = load_config()
    assert cfg.region_name == "conus"
    assert cfg.bbox == CONUS
    assert cfg.min_zoom == 6
    assert cfg.max_zoom == 8


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
