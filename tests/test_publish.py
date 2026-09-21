from __future__ import annotations

import logging
import sys
import time
from pathlib import Path

import pytest

from mpwg_radar.config import R2Config
from mpwg_radar.publish import (
    R2Publisher,
    UploadError,
    UploadTimeoutError,
    classify_radar_files,
)


def _touch(path: Path, data: bytes = b"x") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def _radar_tree(root: Path) -> None:
    _touch(root / "manifest.json", b"{}")
    _touch(root / "colorbar.png", b"\x89PNG")
    _touch(root / "clean" / "20260915T160000Z" / "6" / "0" / "0.png")
    _touch(root / "clean" / "20260915T160000Z" / "frame.json", b"{}")
    _touch(root / "clean" / "20260915T163641Z" / "6" / "1" / "2.png")
    _touch(root / "clean" / "20260915T163641Z" / "frame.json", b"{}")
    _touch(root / "clean" / "latest" / "6" / "1" / "2.png")
    _touch(root / "clean" / "latest" / "frame.json", b"{}")


def test_classify_skips_retained_frames(tmp_path: Path):
    radar = tmp_path / "radar"
    _radar_tree(radar)
    groups, skipped = classify_radar_files(radar, "20260915T163641Z", ["clean"])
    rels = {
        name: [rel for _, rel in items] for name, items in groups.items()
    }
    assert "clean/20260915T163641Z/6/1/2.png" in rels["frame"]
    assert "clean/20260915T163641Z/frame.json" in rels["frame"]
    assert "clean/latest/6/1/2.png" in rels["latest"]
    assert rels["manifest"] == ["manifest.json"]
    assert rels["root"] == ["colorbar.png"]
    retained = [
        rel
        for items in groups.values()
        for _, rel in items
        if "20260915T160000Z" in rel
    ]
    assert retained == []
    assert skipped == 2


def test_classify_skips_other_product_tree(tmp_path: Path):
    radar = tmp_path / "radar"
    _radar_tree(radar)
    _touch(radar / "rala" / "clean" / "20260915T163641Z" / "6" / "1" / "2.png")
    _touch(radar / "rala" / "clean" / "latest" / "6" / "1" / "2.png")
    groups, skipped = classify_radar_files(
        radar, "20260915T163641Z", ["clean"], product_id="composite"
    )
    rels = [rel for items in groups.values() for _, rel in items]
    assert all(not rel.startswith("rala/") for rel in rels)
    assert skipped >= 4

    rala_groups, _ = classify_radar_files(
        radar, "20260915T163641Z", ["clean"], product_id="rala"
    )
    rala_rels = [rel for items in rala_groups.values() for _, rel in items]
    assert "rala/clean/20260915T163641Z/6/1/2.png" in rala_rels
    assert "rala/clean/latest/6/1/2.png" in rala_rels
    assert all(not rel.startswith("clean/") for rel in rala_rels if rel != "manifest.json")


class FakeClient:
    def __init__(self, put=None):
        self.puts = []
        self._put = put

    def put_object(self, **kwargs):
        if self._put:
            self._put(**kwargs)
        self.puts.append(kwargs)
        return {}


def _publisher(client: FakeClient, **overrides) -> R2Publisher:
    cfg = R2Config(
        account_id="abc",
        access_key_id="AKIAEXAMPLE",
        secret_access_key="secret",
        bucket="mpwg-radar",
        prefix="radar",
        connect_timeout_seconds=overrides.get("connect_timeout_seconds", 10),
        read_timeout_seconds=overrides.get("read_timeout_seconds", 30),
        object_timeout_seconds=overrides.get("object_timeout_seconds", 60),
        upload_timeout_seconds=overrides.get("upload_timeout_seconds", 180),
        upload_concurrency=overrides.get("upload_concurrency", 2),
        max_attempts=overrides.get("max_attempts", 2),
    )
    publisher = R2Publisher.__new__(R2Publisher)
    publisher.cfg = cfg
    publisher.client = client
    publisher.bucket = cfg.bucket
    publisher.prefix = cfg.prefix
    publisher.connect_timeout = cfg.connect_timeout_seconds
    publisher.read_timeout = cfg.read_timeout_seconds
    publisher.upload_timeout = cfg.upload_timeout_seconds
    publisher.object_timeout = cfg.object_timeout_seconds
    publisher.concurrency = cfg.upload_concurrency
    publisher.max_attempts = cfg.max_attempts
    return publisher


def test_upload_frame_sends_only_new_frame_latest_and_manifest(tmp_path: Path):
    radar = tmp_path / "radar"
    _radar_tree(radar)
    client = FakeClient()
    publisher = _publisher(client)
    stats = publisher.upload_frame(radar, "20260915T163641Z", ["clean"])
    keys = stats.keys
    assert stats.started == 6
    assert stats.uploaded == 6
    assert stats.failed == 0
    assert stats.skipped == 2
    assert stats.duration_seconds >= 0
    assert "radar/clean/20260915T163641Z/6/1/2.png" in keys
    assert "radar/clean/latest/6/1/2.png" in keys
    assert "radar/manifest.json" in keys
    assert "radar/colorbar.png" in keys
    assert all("20260915T160000Z" not in key for key in keys)
    assert keys[-1] == "radar/manifest.json"


def test_upload_file_uses_put_object_not_upload_file(tmp_path: Path):
    png = tmp_path / "0" / "1" / "2.png"
    _touch(png, b"\x89PNG\r\n")
    client = FakeClient()
    publisher = _publisher(client)
    key = publisher.upload_file(png, "clean/latest/0/1/2.png")
    assert key == "radar/clean/latest/0/1/2.png"
    assert client.puts[0]["Bucket"] == "mpwg-radar"
    assert client.puts[0]["ContentType"] == "image/png"
    assert client.puts[0]["CacheControl"] == "public, max-age=30, s-maxage=30"
    assert client.puts[0]["Key"] == key


def test_upload_fails_fast_on_object_error(tmp_path: Path):
    radar = tmp_path / "radar"
    _radar_tree(radar)
    calls = []

    def put(**kwargs):
        calls.append(kwargs["Key"])
        raise RuntimeError("r2 boom")

    publisher = _publisher(FakeClient(put=put), upload_concurrency=1)
    with pytest.raises(UploadError, match="r2 boom"):
        publisher.upload_frame(radar, "20260915T163641Z", ["clean"])
    assert calls
    # Queued work is cancelled; at most a couple in-flight puts may start.
    assert len(calls) < 6


def test_upload_times_out_when_put_hangs(tmp_path: Path):
    radar = tmp_path / "radar"
    _radar_tree(radar)

    def put(**kwargs):
        time.sleep(0.8)

    publisher = _publisher(
        FakeClient(put=put),
        upload_concurrency=2,
        object_timeout_seconds=0.2,
        upload_timeout_seconds=0.4,
    )
    t0 = time.monotonic()
    with pytest.raises(UploadTimeoutError, match="timed out|timeout|stalled"):
        publisher.upload_frame(radar, "20260915T163641Z", ["clean"])
    assert time.monotonic() - t0 < 2.0


def test_r2_publisher_configures_botocore_timeouts(monkeypatch):
    captured = {}

    class FakeConfig:
        def __init__(self, **kwargs):
            captured["config"] = kwargs

    class FakeBoto3:
        def client(self, *args, **kwargs):
            captured["client_kwargs"] = kwargs
            return FakeClient()

    monkeypatch.setitem(sys.modules, "boto3", FakeBoto3())
    monkeypatch.setitem(
        sys.modules, "botocore.config", type("m", (), {"Config": FakeConfig})
    )
    cfg = R2Config(
        account_id="abc",
        access_key_id="AKIAEXAMPLE",
        secret_access_key="secret",
        bucket="mpwg-radar",
        prefix="radar",
        connect_timeout_seconds=9,
        read_timeout_seconds=11,
        upload_concurrency=2,
        max_attempts=2,
    )
    publisher = R2Publisher(cfg)
    assert publisher.client is not None
    config = captured["config"]
    assert config["connect_timeout"] == 9
    assert config["read_timeout"] == 11
    assert config["retries"]["max_attempts"] == 2
    assert config["signature_version"] == "s3v4"


def test_upload_logs_counts(tmp_path: Path, caplog):
    radar = tmp_path / "radar"
    _radar_tree(radar)
    publisher = _publisher(FakeClient())
    with caplog.at_level(logging.INFO, logger="mpwg_radar.publish"):
        publisher.upload_frame(radar, "20260915T163641Z", ["clean"])
    text = "\n".join(record.getMessage() for record in caplog.records)
    assert "R2 upload start" in text
    assert "objects=6" in text
    assert "skipped=2" in text
    assert "R2 upload complete" in text
    assert "uploaded=6" in text
    assert "failed=0" in text
    assert "duration=" in text
