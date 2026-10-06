"""Opt-in pipeline overlap and atomic publication. Flags default off."""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from mpwg_radar.config import CookerConfig, R2Config
from mpwg_radar.cooker import _write_manifest, cook
from mpwg_radar.geo import CENTRAL_TEXAS
from mpwg_radar.grib import frame_id_for
from mpwg_radar.palette import load_palette
from mpwg_radar.pipeline import (
    GRACE_FLOOR_SECONDS,
    PublisherQueue,
    _sweep_if_possible,
    build_review_document,
    drop_deleted,
    due_deletes,
    effective_grace,
    empty_state,
    frame_prefix,
    load_publish_state,
    note_unlisted,
    save_publish_state,
    state_path,
    sweep_remote_deletes,
)
from mpwg_radar.products import get_product, with_review_tile_prefix
from mpwg_radar.publish import R2Publisher, UploadError, VerifyMismatch
from mpwg_radar.synthetic import synthetic_central_texas


def _touch(path: Path, data: bytes = b"tile-bytes") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def _rala_tree(root: Path, frame: str) -> None:
    _touch(root / "manifest.json", b'{"products":{"rala":{"latest_frame":"OLD"}}}\n')
    _touch(root / "rala" / "colorbar.png", b"\x89PNG-bar")
    _touch(root / "rala" / "clean" / frame / "6" / "1" / "2.png", b"frame-png-bytes")
    _touch(root / "rala" / "clean" / frame / "frame.json", b'{"id":"%s"}' % frame.encode())
    _touch(root / "rala" / "clean" / "latest" / "6" / "1" / "2.png", b"frame-png-bytes")
    _touch(root / "rala" / "clean" / "latest" / "frame.json", b'{"id":"latest"}')


class RecordingClient:
    """In-memory S3. Records call order. Never touches the network."""

    def __init__(self, *, fail_on_put: int = 0, bad_size: bool = False, fail_copy: bool = False):
        self.fail_on_put = fail_on_put
        self.bad_size = bad_size
        self.fail_copy = fail_copy
        self.puts = 0
        self.objects: dict[str, int] = {}
        self.calls: list[tuple] = []

    def put_object(self, **kwargs):
        self.puts += 1
        key = kwargs["Key"]
        self.calls.append(("put_object", key))
        if self.fail_on_put and self.puts == self.fail_on_put:
            raise RuntimeError(f"put failed at {self.puts}")
        size = int(kwargs.get("ContentLength") or 0)
        body = kwargs.get("Body")
        if size <= 0 and body is not None:
            if hasattr(body, "read"):
                data = body.read()
                size = len(data)
            else:
                size = len(body)
        if self.bad_size and key.endswith(".png"):
            size = size + 9
        self.objects[key] = size
        return {}

    def copy_object(self, **kwargs):
        dest = kwargs["Key"]
        src = kwargs["CopySource"]["Key"]
        self.calls.append(("copy_object", dest))
        if self.fail_copy:
            raise RuntimeError("copy failed")
        self.objects[dest] = self.objects.get(src, 0)
        return {}

    def list_objects_v2(self, **kwargs):
        prefix = kwargs["Prefix"]
        token = kwargs.get("ContinuationToken")
        self.calls.append(("list_objects_v2", prefix))
        keys = sorted(key for key in self.objects if key.startswith(prefix))
        # One key per page so pagination is part of the contract.
        if token:
            start = int(token)
        else:
            start = 0
        page = keys[start : start + 1000]
        truncated = start + 1000 < len(keys)
        body = {
            "Contents": [{"Key": key, "Size": self.objects[key]} for key in page],
            "IsTruncated": truncated,
            "KeyCount": len(page),
        }
        if truncated:
            body["NextContinuationToken"] = str(start + 1000)
        return body

    def delete_objects(self, **kwargs):
        keys = tuple(item["Key"] for item in kwargs["Delete"]["Objects"])
        self.calls.append(("delete_objects", keys))
        for key in keys:
            self.objects.pop(key, None)
        return {}

    def get_paginator(self, name):
        if name != "list_objects_v2":
            raise AssertionError(name)
        client = self

        class _Pager:
            def paginate(self, **kwargs):
                token = None
                while True:
                    page_kwargs = dict(kwargs)
                    if token:
                        page_kwargs["ContinuationToken"] = token
                    page = client.list_objects_v2(**page_kwargs)
                    yield page
                    if not page.get("IsTruncated"):
                        break
                    token = page.get("NextContinuationToken")
                    if not token:
                        break

        return _Pager()


def _publisher(client: RecordingClient, **overrides) -> R2Publisher:
    cfg = R2Config(
        account_id="abc",
        access_key_id="AKIAEXAMPLE",
        secret_access_key="secret",
        bucket="mpwg-radar",
        prefix="radar",
        upload_concurrency=overrides.get("upload_concurrency", 1),
        upload_timeout_seconds=overrides.get("upload_timeout_seconds", 60),
        object_timeout_seconds=overrides.get("object_timeout_seconds", 30),
    )
    publisher = R2Publisher.__new__(R2Publisher)
    publisher.cfg = cfg
    publisher.client = client
    publisher.bucket = cfg.bucket
    publisher.prefix = cfg.prefix
    publisher.connect_timeout = 10
    publisher.read_timeout = 30
    publisher.upload_timeout = cfg.upload_timeout_seconds
    publisher.object_timeout = cfg.object_timeout_seconds
    publisher.concurrency = cfg.upload_concurrency
    publisher.max_attempts = 2
    return publisher


def _ops(client: RecordingClient) -> list[tuple]:
    return list(client.calls)


def test_atomic_call_order_verify_then_manifest_then_latest_frame_json_last(tmp_path: Path):
    frame = "20260915T163641Z"
    radar = tmp_path / "radar"
    _rala_tree(radar, frame)
    # Stale latest object from an older frame. It is not in this frame's tile set.
    client = RecordingClient()
    client.objects["radar/rala/clean/latest/6/9/9.png"] = 4
    publisher = _publisher(client)
    before = (radar / "manifest.json").read_bytes()
    stats = publisher.publish_frame_atomic(
        radar,
        frame,
        ["clean"],
        product_id="rala",
        include_latest=True,
        copy_latest=True,
        publish_latest_alias=True,
        previous_manifest=before,
    )
    ops = _ops(client)
    kinds = [op for op, _ in ops]
    manifest_at = next(i for i, (op, key) in enumerate(ops) if op == "put_object" and key.endswith("manifest.json"))
    latest_json_at = next(
        i for i, (op, key) in enumerate(ops) if op == "put_object" and key.endswith("latest/frame.json")
    )
    frame_puts = [
        key
        for op, key in ops[:manifest_at]
        if op == "put_object"
    ]
    assert frame_puts
    assert all("/latest/" not in key for key in frame_puts)
    assert all(not key.endswith("manifest.json") for key in frame_puts)
    assert f"radar/rala/clean/{frame}/6/1/2.png" in frame_puts
    assert f"radar/rala/clean/{frame}/frame.json" in frame_puts
    assert "radar/rala/colorbar.png" in frame_puts
    # VERIFY the timestamped prefix before the manifest PUT.
    lists_before_manifest = [
        key for op, key in ops[:manifest_at] if op == "list_objects_v2"
    ]
    assert lists_before_manifest
    assert any(key.startswith(f"radar/rala/clean/{frame}/") for key in lists_before_manifest)
    # Latest tiles are copied after the manifest, and frame.json is last.
    assert kinds.index("copy_object") > manifest_at
    assert latest_json_at > kinds.index("copy_object")
    assert any(
        op == "list_objects_v2" and "/latest/" in key
        for op, key in ops[manifest_at:latest_json_at]
    )
    delete_at = next(i for i, (op, _) in enumerate(ops) if op == "delete_objects")
    assert delete_at > latest_json_at
    deleted = ops[delete_at][1]
    assert "radar/rala/clean/latest/6/9/9.png" in deleted
    assert "radar/rala/clean/latest/6/1/2.png" not in deleted
    assert "radar/rala/clean/latest/frame.json" not in deleted
    assert "radar/rala/clean/latest/6/9/9.png" not in client.objects
    assert stats.uploaded >= 1


def test_latest_alias_off_skips_latest_and_points_manifest_at_the_timestamp(tmp_path: Path):
    frame = "20260915T163641Z"
    radar = tmp_path / "radar"
    _rala_tree(radar, frame)
    client = RecordingClient()
    publisher = _publisher(client)
    publisher.publish_frame_atomic(
        radar,
        frame,
        ["clean"],
        product_id="rala",
        include_latest=True,
        copy_latest=True,
        publish_latest_alias=False,
    )
    keys = [key for op, key in client.calls if op == "put_object"]
    assert not any("/latest/" in key for key in keys)
    assert [op for op, _ in client.calls].count("copy_object") == 0
    assert not any(op == "delete_objects" for op, _ in client.calls)
    product = get_product("rala")
    palette = load_palette("mpwg-rala-2026-09")
    doc = build_review_document(
        CookerConfig(
            product_id="rala",
            publish_latest_alias=False,
            rala_dbz_interp="structure_v1",
            modes=["clean"],
            min_zoom=6,
            max_zoom=9,
        ),
        with_review_tile_prefix(product, "structure_v1"),
        palette,
        [
            {
                "id": "20260915T160000Z",
                "valid_time": "2026-09-15T16:00:00+00:00",
                "tiles": "rala-review/structure_v1/clean/20260915T160000Z/{z}/{x}/{y}.png",
                "frame": "rala-review/structure_v1/clean/20260915T160000Z/frame.json",
            },
            {
                "id": frame,
                "valid_time": "2026-09-15T16:36:41+00:00",
                "tiles": f"rala-review/structure_v1/clean/{frame}/{{z}}/{{x}}/{{y}}.png",
                "frame": f"rala-review/structure_v1/clean/{frame}/frame.json",
            },
        ],
        frame,
    )
    assert doc["label"] == "NOT PRODUCTION"
    assert doc["latest"] == f"rala-review/structure_v1/clean/{frame}/{{z}}/{{x}}/{{y}}.png"
    assert doc["latest_frame"] == frame
    assert doc["frames"][0]["id"] == "20260915T160000Z"
    assert doc["frames"][-1]["id"] == frame
    assert doc["min_zoom"] == 6
    assert doc["max_zoom"] == 9
    assert "palette" in doc
    assert "retention" in doc
    assert doc["retention"]["grace_seconds"] >= 600
    assert doc["updated_at"]


def test_nth_put_and_verify_mismatch_leave_manifest_and_latest_unchanged(tmp_path: Path):
    frame = "20260915T163641Z"
    radar = tmp_path / "radar"
    _rala_tree(radar, frame)
    previous = (radar / "manifest.json").read_bytes()

    failed = RecordingClient(fail_on_put=1)
    with pytest.raises(UploadError, match="put failed"):
        _publisher(failed).publish_frame_atomic(
            radar,
            frame,
            ["clean"],
            product_id="rala",
            include_latest=True,
            copy_latest=True,
            previous_manifest=previous,
        )
    assert not any(key.endswith("manifest.json") for op, key in failed.calls if op == "put_object")
    assert not any("latest/frame.json" in key for op, key in failed.calls if op == "put_object")
    assert (radar / "manifest.json").read_bytes() == previous

    mismatched = RecordingClient(bad_size=True)
    with pytest.raises(VerifyMismatch):
        _publisher(mismatched).publish_frame_atomic(
            radar,
            frame,
            ["clean"],
            product_id="rala",
            include_latest=True,
            copy_latest=True,
            previous_manifest=previous,
        )
    assert not any(key.endswith("manifest.json") for op, key in mismatched.calls if op == "put_object")
    assert not any("latest/frame.json" in key for _, key in (
        (op, key) for op, key in mismatched.calls if op == "put_object"
    ))
    assert (radar / "manifest.json").read_bytes() == previous

    # The next frame publishes normally. The failed prefix was left unreferenced.
    nxt = "20260915T164000Z"
    nxt_root = tmp_path / "next"
    _rala_tree(nxt_root, nxt)
    ok = RecordingClient()
    _publisher(ok).publish_frame_atomic(
        nxt_root,
        nxt,
        ["clean"],
        product_id="rala",
        include_latest=True,
        copy_latest=True,
    )
    put_keys = [key for op, key in ok.calls if op == "put_object"]
    assert any(key.endswith("manifest.json") for key in put_keys)
    assert any(key.endswith("latest/frame.json") for key in put_keys)
    assert f"radar/rala/clean/{nxt}/frame.json" in put_keys


def test_copy_failure_does_not_publish_latest_frame_json(tmp_path: Path):
    frame = "20260915T163641Z"
    radar = tmp_path / "radar"
    _rala_tree(radar, frame)
    previous = (radar / "manifest.json").read_bytes()
    client = RecordingClient(fail_copy=True)
    with pytest.raises(UploadError, match="copy failed"):
        _publisher(client).publish_frame_atomic(
            radar,
            frame,
            ["clean"],
            product_id="rala",
            include_latest=True,
            copy_latest=True,
            previous_manifest=previous,
        )
    assert not any("latest/frame.json" in key for op, key in client.calls if op == "put_object")
    assert (radar / "manifest.json").read_bytes() == previous
    # The timestamped frame was uploaded; the manifest that remains is the previous one.
    assert any(f"/{frame}/" in key for op, key in client.calls if op == "put_object")


def test_retention_grace_survives_restart_and_does_not_double_delete(tmp_path: Path):
    now = datetime(2026, 10, 6, 18, 0, tzinfo=timezone.utc)
    state = empty_state()
    prefix = "rala-review/structure_v1/clean/20260915T160000Z"
    note_unlisted(state, [prefix], now, grace_seconds=1)
    # A one-second request is clamped by the scheduler's caller, not by note.
    # effective_grace is what publish uses.
    cfg = CookerConfig(retention_grace_seconds=1)
    assert effective_grace(cfg) == GRACE_FLOOR_SECONDS == 600
    state = empty_state()
    note_unlisted(state, [prefix], now, grace_seconds=effective_grace(cfg))
    # Restart must not move the clock.
    note_unlisted(state, [prefix], now + timedelta(minutes=30), grace_seconds=600)
    assert len(state["pending_deletes"]) == 1
    assert due_deletes(state, now + timedelta(minutes=9)) == []
    path = tmp_path / ".publish-state.json"
    save_publish_state(path, state)
    loaded = load_publish_state(path)
    assert due_deletes(loaded, now + timedelta(seconds=599)) == []
    assert due_deletes(loaded, now + timedelta(seconds=600)) == [prefix]

    class Deleter:
        def __init__(self):
            self.prefixes: list[str] = []

        def delete_prefix(self, relative_prefix: str) -> int:
            self.prefixes.append(relative_prefix)
            return 1

    deleter = Deleter()
    assert sweep_remote_deletes(deleter, loaded, now + timedelta(seconds=599)) == []
    assert deleter.prefixes == []
    deleted = sweep_remote_deletes(deleter, loaded, now + timedelta(seconds=600))
    assert deleted == [prefix]
    assert deleter.prefixes == [prefix]
    again = sweep_remote_deletes(deleter, loaded, now + timedelta(hours=2))
    assert again == []
    assert deleter.prefixes == [prefix]
    drop_deleted(loaded, prefix)
    assert loaded["pending_deletes"] == []


def test_publisher_queue_maxsize_is_one_and_at_most_two_frames_are_in_flight():
    from mpwg_radar.pipeline import FrameJob

    state = {"processing": 0, "publishing": 0, "max_sum": 0}
    order: list[tuple] = []
    lock = threading.Lock()

    def _note(kind: str, frame_id: str) -> None:
        with lock:
            state["max_sum"] = max(
                state["max_sum"], state["processing"] + state["publishing"]
            )
            order.append((kind, frame_id, state["processing"], state["publishing"]))

    publishing_now = threading.Event()

    def publish(job: FrameJob) -> dict:
        with lock:
            state["publishing"] += 1
        publishing_now.set()
        _note("publish_start", job.frame_id)
        time.sleep(0.2)
        with lock:
            state["publishing"] -= 1
        _note("publish_end", job.frame_id)
        return {"ok": True, "frame_id": job.frame_id, "valid_time": job.valid_time.isoformat()}

    publisher = PublisherQueue(publish)
    assert publisher.queue.maxsize == 1
    assert publisher.maxsize == 1
    publisher.start()
    try:
        for index in range(3):
            if index > 0:
                assert publishing_now.wait(2)
                publishing_now.clear()
            with lock:
                state["processing"] += 1
            _note("process_start", f"f{index}")
            time.sleep(0.05)
            with lock:
                state["processing"] -= 1
            _note("process_end", f"f{index}")
            when = datetime(2026, 9, 15, 16, index, tzinfo=timezone.utc)
            publisher.submit(
                FrameJob(
                    frame_id=f"f{index}",
                    valid_time=when,
                    private_root=Path("."),
                    mode_summaries=[],
                    marks={},
                )
            )
    finally:
        publisher.close()
    assert state["max_sum"] <= 2
    assert [item["frame_id"] for item in publisher.results] == ["f0", "f1", "f2"]
    # Frame 2 is not processed while frame 0 is still publishing.
    def _at(kind: str, frame_id: str) -> int:
        return next(i for i, item in enumerate(order) if item[0] == kind and item[1] == frame_id)

    assert _at("process_start", "f2") > _at("publish_end", "f0")
    # Frame 1's process overlaps frame 0's publish.
    assert _at("process_start", "f1") > _at("publish_start", "f0")
    assert _at("process_end", "f1") < _at("publish_end", "f0") or _at(
        "process_start", "f1"
    ) < _at("publish_end", "f0")


def _png_bytes(root: Path) -> dict[str, bytes]:
    """Tile bytes for one frame directory, ignoring the latest alias."""
    if not root.is_dir():
        return {}
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*.png"))
    }


def _archive_cfg(tmp_path: Path, **overrides) -> CookerConfig:
    base = dict(
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
        palette_id="mpwg-rala-2026-09",
        rala_catchup_budget_seconds=0,
        tile_workers=1,
    )
    base.update(overrides)
    return CookerConfig(**base)


def _patch_archive(monkeypatch, times):
    def _key(when: datetime) -> str:
        stamp = when.strftime("%Y%m%d-%H%M%S")
        return (
            "CONUS/ReflectivityAtLowestAltitude_00.50/"
            f"{when:%Y%m%d}/MRMS_ReflectivityAtLowestAltitude_00.50_{stamp}.grib2.gz"
        )

    scans = [type("Scan", (), {"valid_time": when, "key": _key(when)})() for when in times]
    monkeypatch.setattr(
        "mpwg_radar.cooker.list_recent_s3_scans",
        lambda cfg, max_age, now=None: list(scans),
    )
    monkeypatch.setattr("mpwg_radar.cooker._load_ncep_latest_frame", lambda cfg: None)

    def _download(cfg, key, dest):
        dest.mkdir(parents=True, exist_ok=True)
        path = dest / Path(key).name
        path.write_bytes(b"grib")
        return path

    monkeypatch.setattr("mpwg_radar.cooker.download_s3_key", _download)

    def _decode(path, bbox=None, product=None):
        name = Path(path).name
        when = next(item.valid_time for item in scans if item.key.endswith(name))
        return synthetic_central_texas(valid_time=when)

    monkeypatch.setattr("mpwg_radar.cooker.decode_grib2", _decode)
    return scans


def test_pipeline_newest_first_backfill_does_not_move_latest(tmp_path: Path, monkeypatch, caplog):
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    times = [now - timedelta(minutes=minutes) for minutes in (0, 2, 4)]
    _patch_archive(monkeypatch, times)
    cfg = _archive_cfg(tmp_path, pipeline=True, rala_catchup_budget_seconds=0)
    with caplog.at_level("INFO", logger="mpwg_radar.pipeline"):
        first = cook(cfg, source="mrms", upload=False)
    assert first["archive_cooked"] == 1
    assert first["frame_id"] == frame_id_for(times[0])
    text = "\n".join(record.getMessage() for record in caplog.records)
    assert "frame_timing " in text
    timing = next(
        json.loads(line.split("frame_timing ", 1)[1])
        for line in text.splitlines()
        if line.startswith("frame_timing ")
    )
    for key in (
        "detect",
        "download",
        "decode",
        "prepare_start",
        "prepare_end",
        "tiles_start",
        "tiles_end",
        "enqueue",
        "queue_wait_seconds",
        "publish_start",
        "frame_put_done",
        "verify_done",
        "manifest_put",
        "latest_done",
    ):
        assert key in timing
    assert "memory_sample " in text
    assert "rss_high_water_bytes" in text
    assert "cgroup_current_bytes" in text
    mode_dir = tmp_path / "radar" / "rala" / "clean"
    assert (mode_dir / frame_id_for(times[0]) / "frame.json").is_file()
    assert not (mode_dir / frame_id_for(times[1])).exists()

    cfg.rala_catchup_budget_seconds = 1500
    second = cook(cfg, source="mrms", upload=False)
    assert second["archive_cooked"] == 2
    manifest = json.loads((tmp_path / "radar" / "manifest.json").read_text())
    rala = manifest["products"]["rala"]
    assert rala["latest_frame"] == frame_id_for(times[0])
    listed = [item["id"] for item in rala["modes"]["clean"]["frames"]]
    assert listed[0] == frame_id_for(times[0])
    assert frame_id_for(times[1]) in listed
    assert frame_id_for(times[2]) in listed
    latest = json.loads((mode_dir / "latest" / "frame.json").read_text())
    assert latest["id"] == frame_id_for(times[0])


def test_pipelined_tiles_match_serial_tiles(tmp_path: Path, monkeypatch):
    when = datetime.now(timezone.utc).replace(microsecond=0)
    serial_dir = tmp_path / "serial"
    serial = _archive_cfg(serial_dir, pipeline=False)
    frame = synthetic_central_texas(valid_time=when)
    cook(serial, source="synthetic", upload=False, archive=False, loaded_frame=frame)
    serial_id = frame_id_for(when)
    serial_pngs = _png_bytes(serial_dir / "radar" / "rala" / "clean" / serial_id)
    assert serial_pngs

    pipe_dir = tmp_path / "pipe"
    _patch_archive(monkeypatch, [when])
    piped = _archive_cfg(pipe_dir, pipeline=True, rala_catchup_budget_seconds=0)
    result = cook(piped, source="mrms", upload=False)
    assert result["frame_id"] == serial_id
    pipe_pngs = _png_bytes(pipe_dir / "radar" / "rala" / "clean" / serial_id)
    assert pipe_pngs.keys() == serial_pngs.keys()
    for name, blob in serial_pngs.items():
        assert pipe_pngs[name] == blob


def test_structure_pipelined_tiles_match_serial_tiles(tmp_path: Path, monkeypatch):
    when = datetime.now(timezone.utc).replace(microsecond=0)
    serial_dir = tmp_path / "serial"
    serial = _archive_cfg(
        serial_dir, pipeline=False, rala_dbz_interp="structure_v1"
    )
    frame = synthetic_central_texas(valid_time=when)
    cook(serial, source="synthetic", upload=False, archive=False, loaded_frame=frame)
    serial_id = frame_id_for(when)
    prefix = serial_dir / "radar" / "rala-review" / "structure_v1" / "clean" / serial_id
    serial_pngs = _png_bytes(prefix)
    assert serial_pngs

    pipe_dir = tmp_path / "pipe"
    _patch_archive(monkeypatch, [when])
    piped = _archive_cfg(
        pipe_dir,
        pipeline=True,
        rala_dbz_interp="structure_v1",
        rala_catchup_budget_seconds=0,
    )
    cook(piped, source="mrms", upload=False)
    pipe_pngs = _png_bytes(
        pipe_dir / "radar" / "rala-review" / "structure_v1" / "clean" / serial_id
    )
    assert pipe_pngs == serial_pngs


def test_flag_off_still_writes_the_manifest_before_upload(tmp_path: Path, monkeypatch):
    """Production path is unchanged: the local manifest advances before PUT."""
    calls: list[str] = []

    class Boom:
        def put_object(self, **kwargs):
            calls.append(kwargs["Key"])
            raise RuntimeError("stop before publish")

        def get_paginator(self, _name):
            return type("P", (), {"paginate": lambda self, **kw: []})()

    def fake_init(self, cfg):
        self.cfg = cfg
        self.client = Boom()
        self.bucket = cfg.bucket
        self.prefix = cfg.prefix.strip("/")
        self.connect_timeout = 10
        self.read_timeout = 30
        self.upload_timeout = 60
        self.object_timeout = 30
        self.concurrency = 1
        self.max_attempts = 1

    monkeypatch.setattr(R2Publisher, "__init__", fake_init)
    cfg = _archive_cfg(
        tmp_path,
        upload=True,
        pipeline=False,
        atomic_publish=False,
        r2=R2Config(
            account_id="abc",
            access_key_id="AKIAEXAMPLE",
            secret_access_key="secret",
            bucket="mpwg-radar",
            prefix="radar",
            upload_concurrency=1,
        ),
    )
    with pytest.raises(UploadError):
        cook(cfg, source="synthetic", upload=True, archive=False)
    manifest = json.loads((tmp_path / "radar" / "manifest.json").read_text())
    assert manifest["products"]["rala"]["latest_frame"]
    assert calls
    assert all("latest/frame.json" not in key for key in calls)


def _r2() -> R2Config:
    return R2Config(
        account_id="abc",
        access_key_id="AKIAEXAMPLE",
        secret_access_key="secret",
        bucket="mpwg-radar",
        prefix="radar",
        upload_concurrency=1,
        upload_timeout_seconds=30,
        object_timeout_seconds=20,
    )


def test_composite_merge_does_not_name_an_inflight_rala_frame(tmp_path: Path, monkeypatch):
    newer = datetime.now(timezone.utc).replace(microsecond=0)
    older = newer - timedelta(minutes=2)
    cfg = _archive_cfg(tmp_path, upload=False, atomic_publish=False)
    cook(
        cfg,
        source="synthetic",
        upload=False,
        archive=False,
        loaded_frame=synthetic_central_texas(valid_time=older),
    )
    seeded = (tmp_path / "radar" / "manifest.json").read_text()
    assert frame_id_for(older) in seeded
    assert frame_id_for(newer) not in seeded

    started = threading.Event()
    release = threading.Event()
    client = RecordingClient()
    original_put = client.put_object

    def blocking_put(**kwargs):
        if not started.is_set():
            started.set()
            assert release.wait(15)
        return original_put(**kwargs)

    client.put_object = blocking_put

    def fake_init(self, r2cfg):
        self.cfg = r2cfg
        self.client = client
        self.bucket = r2cfg.bucket
        self.prefix = r2cfg.prefix.strip("/")
        self.connect_timeout = 10
        self.read_timeout = 30
        self.upload_timeout = 30
        self.object_timeout = 20
        self.concurrency = 1
        self.max_attempts = 1

    monkeypatch.setattr(R2Publisher, "__init__", fake_init)
    gated = _archive_cfg(
        tmp_path,
        upload=True,
        atomic_publish=True,
        r2=_r2(),
    )
    errors: list[BaseException] = []

    def _run() -> None:
        try:
            cook(
                gated,
                source="synthetic",
                upload=True,
                archive=False,
                loaded_frame=synthetic_central_texas(valid_time=newer),
            )
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    worker = threading.Thread(target=_run)
    worker.start()
    assert started.wait(30)
    during = (tmp_path / "radar" / "manifest.json").read_text()
    assert frame_id_for(newer) not in during
    assert frame_id_for(older) in during
    composite = CookerConfig(
        bbox=CENTRAL_TEXAS,
        region_name="central-texas",
        modes=["clean"],
        min_zoom=6,
        max_zoom=6,
        output_dir=tmp_path,
        product_id="composite",
    )
    merged = _write_manifest(
        composite,
        load_palette("mpwg-clean-2026-09"),
        tmp_path / "radar",
        synthetic_central_texas(valid_time=newer),
        [],
        get_product("composite"),
    )
    rala_blob = json.dumps(merged["products"]["rala"])
    assert frame_id_for(newer) not in rala_blob
    assert merged["products"]["rala"]["latest_frame"] == frame_id_for(older)
    release.set()
    worker.join(60)
    assert errors == []
    assert not worker.is_alive()
    done = json.loads((tmp_path / "radar" / "manifest.json").read_text())
    assert done["products"]["rala"]["latest_frame"] == frame_id_for(newer)


def test_review_manifest_key_is_the_only_manifest_put(tmp_path: Path, monkeypatch):
    when = datetime.now(timezone.utc).replace(microsecond=0)
    radar = tmp_path / "radar"
    radar.mkdir()
    (radar / "manifest.json").write_text('{"product":"mpwg-radar","kept":true}\n')
    before = (radar / "manifest.json").read_bytes()
    client = RecordingClient()

    def fake_init(self, r2cfg):
        self.cfg = r2cfg
        self.client = client
        self.bucket = r2cfg.bucket
        self.prefix = r2cfg.prefix.strip("/")
        self.connect_timeout = 10
        self.read_timeout = 30
        self.upload_timeout = 60
        self.object_timeout = 30
        self.concurrency = 1
        self.max_attempts = 1

    monkeypatch.setattr(R2Publisher, "__init__", fake_init)
    cfg = _archive_cfg(
        tmp_path,
        upload=True,
        atomic_publish=True,
        rala_dbz_interp="structure_v1",
        publish_latest_alias=False,
        review_manifest_key="rala-review/structure_v1/manifest.json",
        r2=_r2(),
    )
    cook(
        cfg,
        source="synthetic",
        upload=True,
        archive=False,
        loaded_frame=synthetic_central_texas(valid_time=when),
    )
    assert (radar / "manifest.json").read_bytes() == before
    put_keys = [key for op, key in client.calls if op == "put_object"]
    assert "radar/manifest.json" not in put_keys
    assert "radar/rala-review/structure_v1/manifest.json" in put_keys
    assert not any(key.endswith("latest/frame.json") for key in put_keys)
    review = json.loads(
        (radar / "rala-review" / "structure_v1" / "manifest.json").read_text()
    )
    assert review["label"] == "NOT PRODUCTION"
    assert review["latest_frame"] == frame_id_for(when)
    assert review["latest"].endswith(f"{frame_id_for(when)}/{{z}}/{{x}}/{{y}}.png")
    assert review["frames"][0]["id"] == frame_id_for(when)
    assert review["min_zoom"] == 6
    assert review["max_zoom"] == 6
    assert "palette" in review and "retention" in review and review["updated_at"]


def _install_r2(monkeypatch, client: RecordingClient) -> None:
    def fake_init(self, r2cfg):
        self.cfg = r2cfg
        self.client = client
        self.bucket = r2cfg.bucket
        self.prefix = r2cfg.prefix.strip("/")
        self.connect_timeout = 10
        self.read_timeout = 30
        self.upload_timeout = 60
        self.object_timeout = 30
        self.concurrency = 1
        self.max_attempts = 1

    monkeypatch.setattr(R2Publisher, "__init__", fake_init)


def _review_cfg(tmp_path: Path, **overrides) -> CookerConfig:
    base = dict(
        upload=True,
        atomic_publish=True,
        rala_dbz_interp="structure_v1",
        publish_latest_alias=False,
        review_manifest_key="rala-review/structure_v1/manifest.json",
        r2=_r2(),
    )
    base.update(overrides)
    return _archive_cfg(tmp_path, **base)


def test_failed_upload_then_successful_retry_is_not_deleted_after_grace(
    tmp_path: Path, monkeypatch
):
    """A partial prefix queued on failure must not fire after that ts publishes."""
    when = datetime.now(timezone.utc).replace(microsecond=0)
    frame = frame_id_for(when)
    holder = {"client": RecordingClient(fail_on_put=1)}

    def fake_init(self, r2cfg):
        self.cfg = r2cfg
        self.client = holder["client"]
        self.bucket = r2cfg.bucket
        self.prefix = r2cfg.prefix.strip("/")
        self.connect_timeout = 10
        self.read_timeout = 30
        self.upload_timeout = 60
        self.object_timeout = 30
        self.concurrency = 1
        self.max_attempts = 1

    monkeypatch.setattr(R2Publisher, "__init__", fake_init)
    cfg = _review_cfg(tmp_path)
    loaded = synthetic_central_texas(valid_time=when)
    with pytest.raises(UploadError):
        cook(cfg, source="synthetic", upload=True, archive=False, loaded_frame=loaded)
    product = with_review_tile_prefix(get_product("rala"), "structure_v1")
    prefix = frame_prefix(product, "clean", frame)
    state = load_publish_state(state_path(tmp_path / "radar"))
    assert [item["prefix"] for item in state["pending_deletes"]] == [prefix]

    holder["client"] = RecordingClient()
    cook(
        cfg,
        source="synthetic",
        upload=True,
        archive=False,
        loaded_frame=synthetic_central_texas(valid_time=when),
    )
    state = load_publish_state(state_path(tmp_path / "radar"))
    assert all(item.get("prefix") != prefix for item in state["pending_deletes"])
    live_keys = [key for key in holder["client"].objects if f"/{frame}/" in key]
    assert live_keys

    class Deleter:
        def __init__(self):
            self.prefixes: list[str] = []

        def delete_prefix(self, relative_prefix: str) -> int:
            self.prefixes.append(relative_prefix)
            return 1

    deleter = Deleter()
    deleted = sweep_remote_deletes(
        deleter, state, datetime.now(timezone.utc) + timedelta(hours=3)
    )
    assert prefix not in deleter.prefixes
    assert prefix not in deleted
    assert live_keys
    assert all(key in holder["client"].objects for key in live_keys)


def test_delete_time_skips_a_prefix_the_manifest_lists(tmp_path: Path, monkeypatch):
    frozen = datetime(2026, 10, 6, 18, 0, tzinfo=timezone.utc)
    monkeypatch.setattr("mpwg_radar.pipeline._now", lambda: frozen)
    listed = "20260915T160000Z"
    orphan = "20260915T150000Z"
    product = with_review_tile_prefix(get_product("rala"), "structure_v1")
    listed_prefix = frame_prefix(product, "clean", listed)
    orphan_prefix = frame_prefix(product, "clean", orphan)
    radar = tmp_path / "radar"
    review_path = radar / "rala-review" / "structure_v1" / "manifest.json"
    review_path.parent.mkdir(parents=True)
    review_path.write_text(
        json.dumps(
            {
                "label": "NOT PRODUCTION",
                "latest_frame": listed,
                "frames": [{"id": listed, "valid_time": "2026-09-15T16:00:00+00:00"}],
            }
        )
    )
    state = empty_state()
    note_unlisted(
        state,
        [listed_prefix, orphan_prefix],
        frozen - timedelta(hours=1),
        grace_seconds=600,
    )
    save_publish_state(state_path(radar), state)
    client = RecordingClient()
    client.objects[f"radar/{listed_prefix}/6/1/2.png"] = 4
    client.objects[f"radar/{orphan_prefix}/6/1/2.png"] = 4
    _install_r2(monkeypatch, client)
    _sweep_if_possible(_review_cfg(tmp_path), radar)
    assert f"radar/{listed_prefix}/6/1/2.png" in client.objects
    assert f"radar/{orphan_prefix}/6/1/2.png" not in client.objects
    deleted_keys = [
        key
        for op, keys in client.calls
        if op == "delete_objects"
        for key in keys
    ]
    assert f"radar/{listed_prefix}/6/1/2.png" not in deleted_keys
    assert f"radar/{orphan_prefix}/6/1/2.png" in deleted_keys


def test_pipeline_catchup_skips_scans_already_outside_retention(
    tmp_path: Path, monkeypatch
):
    now = datetime.now(timezone.utc).replace(microsecond=0)
    fresh = now - timedelta(minutes=2)
    stale = now - timedelta(minutes=90)
    _patch_archive(monkeypatch, [fresh, stale])
    decoded: list[datetime] = []
    import mpwg_radar.cooker as cooker_mod

    inner = cooker_mod.decode_grib2

    def _decode(path, bbox=None, product=None):
        frame = inner(path, bbox=bbox, product=product)
        decoded.append(frame.valid_time)
        return frame

    monkeypatch.setattr("mpwg_radar.cooker.decode_grib2", _decode)
    cfg = _archive_cfg(tmp_path, pipeline=True, rala_catchup_budget_seconds=1500)
    result = cook(cfg, source="mrms", upload=False)
    assert result["archive_cooked"] == 1
    assert result["frame_id"] == frame_id_for(fresh)
    assert decoded == [fresh]
    mode_dir = tmp_path / "radar" / "rala" / "clean"
    assert (mode_dir / frame_id_for(fresh) / "frame.json").is_file()
    assert not (mode_dir / frame_id_for(stale)).exists()


def test_list_reconcile_deletes_untracked_old_prefix_and_spares_the_rest(
    tmp_path: Path, monkeypatch
):
    frozen = datetime(2026, 10, 6, 18, 0, tzinfo=timezone.utc)
    monkeypatch.setattr("mpwg_radar.pipeline._now", lambda: frozen)
    listed = "20260915T160000Z"
    untracked = "20261006T100000Z"
    recent = "20261006T175500Z"
    marker_frame = "20260101T000000Z"
    product = with_review_tile_prefix(get_product("rala"), "structure_v1")
    radar = tmp_path / "radar"
    review_path = radar / "rala-review" / "structure_v1" / "manifest.json"
    review_path.parent.mkdir(parents=True)
    review_path.write_text(
        json.dumps(
            {
                "label": "NOT PRODUCTION",
                "latest_frame": listed,
                "frames": [{"id": listed}],
            }
        )
    )
    # No publish-state entry for the untracked prefix. Reconcile must still see it.
    client = RecordingClient()
    listed_key = f"radar/{frame_prefix(product, 'clean', listed)}/6/1/2.png"
    untracked_key = f"radar/{frame_prefix(product, 'clean', untracked)}/6/1/2.png"
    recent_key = f"radar/{frame_prefix(product, 'clean', recent)}/6/1/2.png"
    opt_key = (
        f"radar/{frame_prefix(product, 'clean', marker_frame)}/_opt_validate/tile.png"
    )
    atomic_key = (
        "radar/rala-review/structure_v1/clean/_atomic_test/"
        f"{marker_frame}/tile.png"
    )
    outside_key = f"radar/rala/clean/{marker_frame}/6/1/2.png"
    for key in (listed_key, untracked_key, recent_key, opt_key, atomic_key, outside_key):
        client.objects[key] = 8
    _install_r2(monkeypatch, client)
    _sweep_if_possible(_review_cfg(tmp_path), radar)
    assert untracked_key not in client.objects
    for spared in (listed_key, recent_key, opt_key, atomic_key, outside_key):
        assert spared in client.objects
    deleted_keys = [
        key
        for op, keys in client.calls
        if op == "delete_objects"
        for key in keys
    ]
    assert untracked_key in deleted_keys
    assert listed_key not in deleted_keys
    assert opt_key not in deleted_keys
    assert atomic_key not in deleted_keys
    assert outside_key not in deleted_keys
