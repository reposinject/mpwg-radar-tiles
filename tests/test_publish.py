from __future__ import annotations

import time
from pathlib import Path
from threading import Lock

import pytest

from mpwg_radar.config import R2Config
from mpwg_radar.publish import (
    R2Publisher,
    R2UploadTimeout,
    collect_cook_uploads,
)


def _r2_cfg(**kwargs) -> R2Config:
    base = dict(
        account_id="abc",
        access_key_id="AKIAEXAMPLE",
        secret_access_key="secret",
        bucket="mpwg-radar",
        prefix="radar",
        upload_workers=4,
        upload_timeout_seconds=30,
        connect_timeout=10,
        read_timeout=30,
        max_attempts=3,
    )
    base.update(kwargs)
    return R2Config(**base)


def _publisher(client, cfg: R2Config | None = None, **kwargs) -> R2Publisher:
    cfg = cfg or _r2_cfg()
    publisher = R2Publisher.__new__(R2Publisher)
    publisher.cfg = cfg
    publisher.client = client
    publisher.bucket = cfg.bucket
    publisher.prefix = cfg.prefix
    publisher.workers = kwargs.get("workers", cfg.upload_workers)
    publisher.timeout_seconds = float(
        kwargs.get("timeout_seconds", cfg.upload_timeout_seconds)
    )
    return publisher


def _touch_tree(root: Path, relative: str, data: bytes = b"x") -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


class RecordingClient:
    def __init__(self, delay: float = 0.0, fail_on: str | None = None):
        self.delay = delay
        self.fail_on = fail_on
        self.put_calls: list[dict] = []
        self.upload_file_calls: list = []
        self._lock = Lock()

    def put_object(self, **kwargs):
        if self.delay:
            time.sleep(self.delay)
        key = kwargs["Key"]
        if self.fail_on and self.fail_on in key:
            raise RuntimeError(f"injected failure for {key}")
        body = kwargs.get("Body")
        if hasattr(body, "read"):
            body.read()
        with self._lock:
            self.put_calls.append(kwargs)
        return {}

    def upload_file(self, *args, **kwargs):
        self.upload_file_calls.append((args, kwargs))
        raise AssertionError("upload_file (TransferManager) must not be used")


def test_collect_cook_uploads_skips_retained_history(tmp_path: Path):
    radar = tmp_path / "radar"
    _touch_tree(radar, "clean/oldframe/6/0/0.png")
    _touch_tree(radar, "clean/oldframe/frame.json", b"{}")
    _touch_tree(radar, "clean/newframe/6/1/1.png")
    _touch_tree(radar, "clean/newframe/frame.json", b"{}")
    _touch_tree(radar, "clean/latest/6/1/1.png")
    _touch_tree(radar, "clean/latest/frame.json", b"{}")
    _touch_tree(radar, "colorbar.png")
    _touch_tree(radar, "manifest.json", b"{}")

    plan = collect_cook_uploads(
        radar, frame_id="newframe", modes=["clean"], all_frames=False
    )
    keys = [rel for _, rel in plan.all_files()]
    assert "clean/newframe/6/1/1.png" in keys
    assert "clean/latest/6/1/1.png" in keys
    assert "manifest.json" in keys
    assert "colorbar.png" in keys
    assert all("oldframe" not in rel for rel in keys)
    assert [rel for _, rel in plan.manifest_files] == ["manifest.json"]


def test_collect_cook_uploads_all_frames_includes_history(tmp_path: Path):
    radar = tmp_path / "radar"
    _touch_tree(radar, "clean/oldframe/6/0/0.png")
    _touch_tree(radar, "clean/newframe/6/1/1.png")
    _touch_tree(radar, "manifest.json", b"{}")
    plan = collect_cook_uploads(
        radar, frame_id="newframe", modes=["clean"], all_frames=True
    )
    keys = [rel for _, rel in plan.all_files()]
    assert "clean/oldframe/6/0/0.png" in keys
    assert "clean/newframe/6/1/1.png" in keys
    assert keys[-1] == "manifest.json"


def test_upload_file_uses_put_object_not_transfer_manager(tmp_path: Path):
    png = _touch_tree(tmp_path, "0/1/2.png", b"\x89PNG\r\n")
    client = RecordingClient()
    publisher = _publisher(client)
    key = publisher.upload_file(png, "clean/latest/0/1/2.png")
    assert key == "radar/clean/latest/0/1/2.png"
    assert len(client.put_calls) == 1
    call = client.put_calls[0]
    assert call["Bucket"] == "mpwg-radar"
    assert call["Key"] == key
    assert call["ContentType"] == "image/png"
    assert "max-age=30" in call["CacheControl"]
    assert client.upload_file_calls == []


def test_upload_cook_is_incremental_and_manifest_last(tmp_path: Path):
    radar = tmp_path / "radar"
    _touch_tree(radar, "clean/old/6/0/0.png")
    _touch_tree(radar, "clean/new/6/1/1.png")
    _touch_tree(radar, "clean/new/frame.json", b"{}")
    _touch_tree(radar, "clean/latest/6/1/1.png")
    _touch_tree(radar, "clean/latest/frame.json", b"{}")
    _touch_tree(radar, "colorbar.png")
    _touch_tree(radar, "manifest.json", b"{}")

    client = RecordingClient()
    publisher = _publisher(client, workers=1)
    keys = publisher.upload_cook(
        radar, frame_id="new", modes=["clean"], all_frames=False
    )
    relatives = [k.removeprefix("radar/") for k in keys]
    assert "clean/old/6/0/0.png" not in relatives
    assert relatives[-1] == "manifest.json"
    assert "clean/new/6/1/1.png" in relatives
    assert "clean/latest/6/1/1.png" in relatives
    frame_idx = relatives.index("clean/new/6/1/1.png")
    latest_idx = relatives.index("clean/latest/6/1/1.png")
    assert frame_idx < latest_idx < relatives.index("manifest.json")


def test_upload_files_concurrent_workers(tmp_path: Path):
    files = [_touch_tree(tmp_path, f"{i}.png") for i in range(8)]
    items = [(path, path.name) for path in files]
    client = RecordingClient()
    publisher = _publisher(client, workers=4, timeout_seconds=30)
    keys = publisher.upload_files(items)
    assert len(keys) == 8
    assert {call["Key"] for call in client.put_calls} == {f"radar/{i}.png" for i in range(8)}
    assert client.upload_file_calls == []


def test_upload_files_timeout_fails_fast(tmp_path: Path):
    files = [_touch_tree(tmp_path, f"{i}.png") for i in range(4)]
    items = [(path, path.name) for path in files]
    client = RecordingClient(delay=0.4)
    publisher = _publisher(client, workers=2, timeout_seconds=0.15)
    t0 = time.monotonic()
    with pytest.raises(R2UploadTimeout, match="timed out"):
        publisher.upload_files(items)
    elapsed = time.monotonic() - t0
    assert elapsed < 2.0


def test_publisher_init_passes_botocore_timeouts():
    pytest.importorskip("boto3")
    cfg = _r2_cfg(connect_timeout=7, read_timeout=11, max_attempts=2, upload_workers=3)
    publisher = R2Publisher(cfg)
    botocore_cfg = publisher.client.meta.config
    assert botocore_cfg.connect_timeout == 7
    assert botocore_cfg.read_timeout == 11
    assert botocore_cfg.retries.get("mode") == "standard"
    assert publisher.workers == 3
    assert publisher.timeout_seconds == 30
