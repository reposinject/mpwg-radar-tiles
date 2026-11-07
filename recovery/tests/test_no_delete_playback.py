"""No-delete playback behavior of the installed composite cooker plus the patch.

The sandbox is the uploaded box snapshot, not stock src/. config.py was not
in that snapshot; the test copies the repo config and adds only the names
the snapshot imports. Dummy R2 fields are the literals "test", not secrets.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SNAPSHOT = REPO / "recovery" / "box-snapshot"
PATCH = REPO / "recovery" / "mpwg-reclaim-default-off.patch"
STOCK_CONFIG = REPO / "src" / "mpwg_radar" / "config.py"
SANDBOX = Path("/tmp/mpwg-recovery-sandbox")


def _build_sandbox() -> None:
    if SANDBOX.exists():
        shutil.rmtree(SANDBOX)
    shutil.copytree(SNAPSHOT, SANDBOX)
    subprocess.check_call(["patch", "-p1", "-d", str(SANDBOX), "-i", str(PATCH)])
    config = SANDBOX / "src" / "mpwg_radar" / "config.py"
    text = STOCK_CONFIG.read_text()
    text = text.replace(
        '    user_agent: str = "mpwg-radar-tiles/1.0 (+https://github.com/reposinject/mpwg-radar-tiles)"\n',
        '    user_agent: str = "mpwg-radar-tiles/1.0 (+https://github.com/reposinject/mpwg-radar-tiles)"\n'
        '    rala_dbz_interp: str = ""\n',
    )
    text += (
        "\nRALA_DBZ_INTERP_MONOTONE_PCHIP = 'monotone_pchip'\n"
        "RALA_DBZ_INTERP_REVIEW = frozenset({'monotone_pchip', 'bilinear_peak_hold', 'tight_peak_hold'})\n"
        "RALA_DBZ_INTERP_PEAK_HOLD = frozenset({'bilinear_peak_hold', 'tight_peak_hold'})\n"
    )
    config.write_text(text)
    for name in list(sys.modules):
        if name == "mpwg_radar" or name.startswith("mpwg_radar."):
            del sys.modules[name]
    sys.path.insert(0, str(SANDBOX / "src"))


_build_sandbox()

from mpwg_radar.config import load_config  # noqa: E402
from mpwg_radar.cooker import _reclaim_enabled, cook  # noqa: E402
from mpwg_radar.geo import REGIONS  # noqa: E402
from mpwg_radar.grib import frame_id_for  # noqa: E402
from mpwg_radar.publish import UploadStats  # noqa: E402
from mpwg_radar.synthetic import synthetic_central_texas  # noqa: E402


class _FakeR2:
    deleted: list[str] = []

    def __init__(self, cfg):
        self.cfg = cfg

    def upload_frame(self, *args, **kwargs):
        return UploadStats(uploaded=1)

    def upload_manifest(self, root):
        return UploadStats(uploaded=1)

    def delete_prefix(self, prefix):
        _FakeR2.deleted.append(prefix)
        return 1


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch, tmp_path):
    for key in list(os.environ):
        if key.startswith(("MPWG_", "R2_", "AWS_", "MRMS_")):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("mpwg_radar.cooker.R2Publisher", _FakeR2)
    _FakeR2.deleted = []
    yield


def _cfg(out: Path, retention: int = 2, keep_dbz: bool = False):
    os.environ["MPWG_RETENTION_FRAMES"] = str(retention)
    os.environ["MPWG_KEEP_DBZ"] = "1" if keep_dbz else "0"
    cfg = load_config(
        {
            "region_name": "central-texas",
            "bbox": REGIONS["central-texas"],
            "modes": ["clean"],
            "min_zoom": 6,
            "max_zoom": 6,
            "output_dir": out,
            "data_dir": out / "data",
            "product_id": "composite",
            "keep_dbz": keep_dbz,
            "retention_frames": retention,
        }
    )
    cfg.r2.access_key_id = "test"
    cfg.r2.secret_access_key = "test"
    cfg.r2.bucket = "mpwg-radar"
    cfg.r2.endpoint = "https://example.invalid"
    return cfg


def _plant(out: Path, when: datetime) -> str:
    frame_id = frame_id_for(when)
    mode = out / "radar" / "clean" / frame_id
    mode.mkdir(parents=True)
    (mode / "frame.json").write_text(
        json.dumps(
            {
                "id": frame_id,
                "valid_time": when.astimezone(timezone.utc).isoformat(),
                "palette": {"id": "mpwg-clean-2026-10-61", "version": "2026-10-61"},
                "mode_spec": {"sample": "masked-bilinear", "smooth_kind": "none", "spatial": "none"},
                "render_version": "mpwg-r61mb-2026-10",
            }
        )
    )
    return frame_id


def _manifest_ids(out: Path) -> list[str]:
    doc = json.loads((out / "radar" / "manifest.json").read_text())
    frames = doc["products"]["composite"]["modes"]["clean"]["frames"]
    return [item["id"] for item in frames]


def _dirs(out: Path) -> set[str]:
    clean = out / "radar" / "clean"
    return {path.name for path in clean.iterdir() if path.is_dir() and path.name != "latest"}


def test_reclaim_defaults_off(monkeypatch):
    monkeypatch.delenv("MPWG_RECLAIM", raising=False)
    assert _reclaim_enabled() is False
    monkeypatch.setenv("MPWG_RECLAIM", "yes")
    assert _reclaim_enabled() is True


def test_no_upload_keeps_stamps_and_does_not_publish(tmp_path):
    out = tmp_path / "out"
    old_npz = out / "dbz" / "composite" / "already-there.npz"
    old_npz.parent.mkdir(parents=True)
    old_npz.write_bytes(b"keep")
    cfg = _cfg(out, retention=30, keep_dbz=False)
    when = datetime(2026, 11, 7, 20, 48, tzinfo=timezone.utc)
    result = cook(cfg, source="synthetic", upload=False, loaded_frame=synthetic_central_texas(when))
    frame_id = result["frame_id"]
    meta = json.loads((out / "radar" / "clean" / frame_id / "frame.json").read_text())
    assert meta["render_version"] == "mpwg-r61mb-2026-10"
    assert meta["renderVersion"] == "mpwg-r61mb-2026-10"
    assert meta["mode_spec"]["sample"] == "masked-bilinear"
    assert meta["mode_spec"]["smooth_kind"] == "none"
    assert meta["palette"]["id"] == "mpwg-clean-2026-10-61"
    assert meta["palette"]["version"] == "2026-10-61"
    assert meta["palette"]["display_min_dbz"] == 15.0
    assert result["uploaded"] == 0
    assert result["upload_finished_at"] is None
    assert not (out / "radar" / ".published-composite").exists()
    assert not (out / "dbz" / "composite" / f"{frame_id}.npz").exists()
    assert old_npz.read_bytes() == b"keep"
    assert _FakeR2.deleted == []
    pngs = list((out / "radar" / "clean" / frame_id).rglob("*.png"))
    assert pngs
    assert len(pngs) == meta["stats"]["written"]


def test_playback_cap_does_not_delete_objects_and_pin_is_bounded(tmp_path, monkeypatch):
    monkeypatch.delenv("MPWG_RECLAIM", raising=False)
    out = tmp_path / "out"
    cfg = _cfg(out, retention=2, keep_dbz=False)
    base = datetime(2026, 11, 1, tzinfo=timezone.utc)
    planted = [_plant(out, base + timedelta(hours=i)) for i in range(4)]
    newest = base + timedelta(hours=10)
    cook(cfg, source="synthetic", upload=True, loaded_frame=synthetic_central_texas(newest))
    ids = _manifest_ids(out)
    assert len(ids) == 2
    assert ids[0] == frame_id_for(newest)
    assert set(planted).issubset(_dirs(out))
    assert frame_id_for(newest) in _dirs(out)
    assert _FakeR2.deleted == []

    pin = tmp_path / "pin.txt"
    pin.write_text(planted[0] + "\n" + planted[1] + "\n")
    monkeypatch.setenv("MPWG_PIN_FRAME_IDS_FILE", str(pin))
    later = newest + timedelta(hours=1)
    cook(
        _cfg(out, retention=2, keep_dbz=False),
        source="synthetic",
        upload=True,
        loaded_frame=synthetic_central_texas(later),
    )
    pinned_ids = _manifest_ids(out)
    assert planted[0] in pinned_ids and planted[1] in pinned_ids
    assert len(pinned_ids) < len(_dirs(out))
    assert set(planted).issubset(_dirs(out))
    assert _FakeR2.deleted == []


def test_explicit_reclaim_can_prune(tmp_path, monkeypatch):
    monkeypatch.setenv("MPWG_RECLAIM", "1")
    out = tmp_path / "out"
    old = _plant(out, datetime(2026, 10, 1, tzinfo=timezone.utc))
    cook(
        _cfg(out, retention=1, keep_dbz=False),
        source="synthetic",
        upload=True,
        loaded_frame=synthetic_central_texas(datetime(2026, 11, 2, tzinfo=timezone.utc)),
    )
    assert old not in _dirs(out)
    assert _FakeR2.deleted


def test_cli_no_upload_overrides_config_and_output(tmp_path, monkeypatch):
    from mpwg_radar import cli

    seen = {}

    def fake_cook(cfg, source="mrms", grib_path=None, upload=None, archive=True, loaded_frame=None):
        seen["upload"] = upload
        seen["out"] = Path(cfg.output_dir)
        seen["product"] = cfg.product_id
        seen["modes"] = list(cfg.modes)
        seen["palette"] = cfg.palette_id
        seen["retention"] = cfg.retention_frames
        return {"frame_id": "x", "uploaded": 0}

    monkeypatch.setattr(cli, "cook", fake_cook)
    monkeypatch.setenv("MPWG_UPLOAD", "true")
    monkeypatch.setenv("MPWG_OUTPUT_DIR", "/workspace/mpwg-cook-out")
    out = tmp_path / "isolated"
    code = cli.main(
        [
            "cook",
            "--no-upload",
            "--source",
            "synthetic",
            "--region",
            "central-texas",
            "--modes",
            "clean",
            "--product",
            "composite",
            "--min-zoom",
            "6",
            "--max-zoom",
            "6",
            "--out",
            str(out),
        ]
    )
    assert code == 0
    assert seen["upload"] is False
    assert seen["out"] == out
    assert seen["product"] == "composite"
    assert seen["modes"] == ["clean"]
    assert seen["palette"] == "mpwg-clean-2026-10-61"


def test_launcher_contract_and_patch_text():
    launch = (REPO / "recovery" / "mpwg-recovery-launch.sh").read_text()
    plan = (REPO / "recovery" / "PLAN.txt").read_text()
    patch = PATCH.read_text()
    assert "/workspace/mpwg-recovery" in launch
    assert "nohup" in launch
    assert ".writer.lock" in launch
    assert "does not survive" in launch.lower() or "does not survive" in plan.lower()
    for banned in ("systemctl", "supervisord", "crontab", "/tmp/", "aws ec2", "i-024dc2853a125784b"):
        assert banned not in launch
    assert "MPWG_RECLAIM" in patch
    assert "2147483647" not in patch
    assert "delete_prefix" not in patch or "stale = {mode: []" in patch
    assert not any(token in launch for token in ("AKIA", "BEGIN PRIVATE", "R2_SECRET_ACCESS_KEY="))
