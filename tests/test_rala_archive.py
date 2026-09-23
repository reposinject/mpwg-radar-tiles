"""RALA rolling archive: keep real scans, do not thin, composite cap stays put."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from mpwg_radar.config import CookerConfig
from mpwg_radar.cooker import (
    FrameRetention,
    _prune_old_frames,
    _stale_frame_ids,
    _write_manifest,
    apply_retention,
    cook,
    frame_retention,
)
from mpwg_radar.geo import CENTRAL_TEXAS
from mpwg_radar.grib import frame_id_for
from mpwg_radar.ingest import list_recent_s3_scans
from mpwg_radar.palette import load_palette
from mpwg_radar.products import get_product
from mpwg_radar.synthetic import synthetic_central_texas


def _aware(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def _seed_frame(mode_dir: Path, when: datetime) -> str:
    when = _aware(when)
    fid = frame_id_for(when)
    frame_dir = mode_dir / fid
    frame_dir.mkdir(parents=True, exist_ok=True)
    (frame_dir / "frame.json").write_text(
        json.dumps(
            {
                "id": fid,
                "valid_time": when.isoformat(),
                "mode": "clean",
            }
        )
    )
    return fid


def test_frame_retention_is_rala_specific():
    rala = CookerConfig(product_id="rala", retention_frames=5)
    policy = frame_retention(rala)
    assert policy.max_frames == 60
    assert policy.max_age_seconds == 75 * 60
    composite = CookerConfig(product_id="composite", retention_frames=5)
    other = frame_retention(composite)
    assert other.max_frames == 5
    assert other.max_age_seconds is None


def test_two_minute_scans_in_a_sixty_minute_loop_are_not_thinned():
    now = datetime(2026, 9, 22, 16, 0, tzinfo=timezone.utc)
    frames = []
    for minutes in range(0, 76, 2):
        when = now - timedelta(minutes=minutes)
        frames.append({"id": frame_id_for(when), "valid_time": when.isoformat()})
    # 76 minutes is outside the 75-minute window and must drop.
    outside = now - timedelta(minutes=76)
    frames.append({"id": frame_id_for(outside), "valid_time": outside.isoformat()})
    kept = apply_retention(frames, FrameRetention(max_frames=60, max_age_seconds=75 * 60), now=now)
    kept_ids = [item["id"] for item in kept]
    inside_ids = [
        frame_id_for(now - timedelta(minutes=minutes)) for minutes in range(0, 76, 2)
    ]
    # 0,2,...,74 → 38 real scans. The cap (60) does not remove any of them.
    assert len(inside_ids) == 38
    assert kept_ids == inside_ids
    assert frame_id_for(now - timedelta(minutes=60)) in kept_ids
    assert frame_id_for(now - timedelta(minutes=30)) in kept_ids
    assert frame_id_for(outside) not in kept_ids
    # Consecutive ids: no every-other-frame subsample.
    stamps = [
        datetime.strptime(item["id"], "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
        for item in kept
    ]
    gaps = [(stamps[i] - stamps[i + 1]).total_seconds() for i in range(len(stamps) - 1)]
    assert gaps == [120.0] * (len(stamps) - 1)


def test_composite_retention_stays_count_only():
    now = datetime(2026, 9, 22, 16, 0, tzinfo=timezone.utc)
    frames = []
    for minutes in (0, 10, 300):
        when = now - timedelta(minutes=minutes)
        frames.append({"id": frame_id_for(when), "valid_time": when.isoformat()})
    kept = apply_retention(frames, FrameRetention(max_frames=30), now=now)
    assert len(kept) == 3  # the 5-hour-old frame is still inside a count-only cap
    capped = apply_retention(frames, FrameRetention(max_frames=2), now=now)
    assert [item["id"] for item in capped] == [
        frame_id_for(now),
        frame_id_for(now - timedelta(minutes=10)),
    ]


def test_rala_prune_uses_age_not_the_shared_keep_n(tmp_path: Path):
    now_shift = datetime.now(timezone.utc)
    cfg = CookerConfig(
        product_id="rala",
        retention_frames=1,
        rala_retention_frames=60,
        rala_retention_minutes=75,
        modes=["clean"],
        output_dir=tmp_path,
    )
    mode_dir = tmp_path / "radar" / "rala" / "clean"
    recent = _seed_frame(mode_dir, now_shift - timedelta(minutes=10))
    mid = _seed_frame(mode_dir, now_shift - timedelta(minutes=40))
    old = _seed_frame(mode_dir, now_shift - timedelta(minutes=200))
    radar = tmp_path / "radar"
    stale = _stale_frame_ids(cfg, radar, get_product("rala"))
    assert stale["clean"] == [old]
    _prune_old_frames(cfg, radar, get_product("rala"))
    assert (mode_dir / recent).is_dir()
    assert (mode_dir / mid).is_dir()
    assert not (mode_dir / old).is_dir()


def test_manifest_lists_rala_hour_and_composite_cap_unchanged(tmp_path: Path):
    now = datetime.now(timezone.utc).replace(microsecond=0)
    radar = tmp_path / "radar"
    rala_dir = radar / "rala" / "clean"
    # 0..68 min step 2 → 35 frames, all inside 75 minutes even if the clock ticks.
    times = [now - timedelta(minutes=minutes) for minutes in range(0, 70, 2)]
    assert len(times) == 35
    for when in times:
        _seed_frame(rala_dir, when)
    old = _seed_frame(rala_dir, now - timedelta(minutes=120))
    cfg = CookerConfig(
        bbox=CENTRAL_TEXAS,
        region_name="central-texas",
        product_id="rala",
        retention_frames=5,
        modes=["clean"],
        output_dir=tmp_path,
    )
    newest = synthetic_central_texas(valid_time=times[0])
    _write_manifest(
        cfg,
        load_palette("mpwg-rala-2026-09"),
        radar,
        newest,
        [{"mode": "clean", "id": newest.frame_id}],
        get_product("rala"),
        cook_finished_at=now,
    )
    manifest = json.loads((radar / "manifest.json").read_text())
    listed = manifest["products"]["rala"]["modes"]["clean"]["frames"]
    ids = [item["id"] for item in listed]
    assert old not in ids
    assert len(ids) == 35
    assert ids[0] == frame_id_for(times[0])
    retention = manifest["products"]["rala"]["retention"]
    assert retention["max_age_minutes"] == 75
    assert retention["max_frames"] == 60
    # 0..60 step 2 is 31 frames. The frame at exactly 60 min stays inside
    # the small clock skew; 62 min does not.
    assert retention["frames_last_60_minutes"] == 31
    assert manifest["products"]["rala"]["palette"]["version"] == "2026-09-rala-p3d"
    for item, when in zip(listed, times):
        assert item["valid_time"].startswith(when.strftime("%Y-%m-%dT%H:%M:%S"))

    # Composite keep-N is unchanged by the RALA helper.
    comp_dir = radar / "clean"
    comp_times = [now - timedelta(minutes=minutes) for minutes in (0, 4, 8, 12)]
    for when in comp_times:
        _seed_frame(comp_dir, when)
    comp_cfg = CookerConfig(
        bbox=CENTRAL_TEXAS,
        region_name="central-texas",
        product_id="composite",
        retention_frames=2,
        modes=["clean"],
        output_dir=tmp_path,
    )
    comp_frame = synthetic_central_texas(valid_time=comp_times[0])
    _write_manifest(
        comp_cfg,
        load_palette("mpwg-clean-2026-09"),
        radar,
        comp_frame,
        [{"mode": "clean", "id": comp_frame.frame_id}],
        get_product("composite"),
        cook_finished_at=now,
    )
    manifest = json.loads((radar / "manifest.json").read_text())
    comp_ids = [item["id"] for item in manifest["modes"]["clean"]["frames"]]
    assert comp_ids == [frame_id_for(when) for when in comp_times[:2]]
    # The RALA block written above survives a composite manifest merge.
    assert len(manifest["products"]["rala"]["modes"]["clean"]["frames"]) == 35


def test_rala_archive_cooks_every_listed_scan_newest_first(tmp_path: Path, monkeypatch):
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    times = [now - timedelta(minutes=minutes) for minutes in (0, 2, 4)]

    def _key(when: datetime) -> str:
        stamp = when.strftime("%Y%m%d-%H%M%S")
        return (
            "CONUS/ReflectivityAtLowestAltitude_00.50/"
            f"{when:%Y%m%d}/MRMS_ReflectivityAtLowestAltitude_00.50_{stamp}.grib2.gz"
        )

    scans_by_key = {_key(when): when for when in times}

    monkeypatch.setattr(
        "mpwg_radar.cooker.list_recent_s3_scans",
        lambda cfg, max_age, now=None: [
            type("Scan", (), {"valid_time": when, "key": _key(when)})()
            for when in times
        ],
    )
    monkeypatch.setattr("mpwg_radar.cooker._load_ncep_latest_frame", lambda cfg: None)

    def fake_download(cfg, key, dest):
        dest.mkdir(parents=True, exist_ok=True)
        path = dest / Path(key).name
        path.write_bytes(b"grib")
        return path

    monkeypatch.setattr("mpwg_radar.cooker.download_s3_key", fake_download)

    decoded = []

    def fake_decode(path, bbox=None, product=None):
        when = scans_by_key[next(key for key in scans_by_key if key.endswith(Path(path).name))]
        decoded.append(when)
        return synthetic_central_texas(valid_time=when)

    monkeypatch.setattr("mpwg_radar.cooker.decode_grib2", fake_decode)

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
        upload=False,
        product_id="rala",
        retention_frames=5,
        rala_catchup_budget_seconds=0,
    )
    first = cook(cfg, source="mrms", upload=False)
    assert first["archive_cooked"] == 1
    assert first["archive_pending"] == 2
    assert first["frame_id"] == frame_id_for(times[0])
    assert decoded == [times[0]]
    mode_dir = tmp_path / "radar" / "rala" / "clean"
    assert (mode_dir / frame_id_for(times[0]) / "frame.json").is_file()
    assert not (mode_dir / frame_id_for(times[1])).exists()

    cfg.rala_catchup_budget_seconds = 1500
    second = cook(cfg, source="mrms", upload=False)
    assert second["archive_cooked"] == 2
    assert second["archive_pending"] == 0
    manifest = json.loads((tmp_path / "radar" / "manifest.json").read_text())
    rala = manifest["products"]["rala"]
    assert rala["latest_frame"] == frame_id_for(times[0])
    assert rala["latest_valid_time"].startswith(times[0].strftime("%Y-%m-%dT%H:%M:%S"))
    listed = [item["id"] for item in rala["modes"]["clean"]["frames"]]
    assert listed == [frame_id_for(when) for when in times]
    for item, when in zip(rala["modes"]["clean"]["frames"], times):
        assert item["valid_time"].startswith(when.strftime("%Y-%m-%dT%H:%M:%S"))
    latest = json.loads((mode_dir / "latest" / "frame.json").read_text())
    assert latest["id"] == frame_id_for(times[0])
    frame_pngs = list((mode_dir / frame_id_for(times[0])).rglob("*.png"))
    assert frame_pngs
    linked = mode_dir / "latest" / frame_pngs[0].relative_to(mode_dir / frame_id_for(times[0]))
    assert linked.is_file()
    assert linked.stat().st_ino == frame_pngs[0].stat().st_ino
    assert latest["mode_spec"]["sample"] == "masked-splat"
    assert latest["mode_spec"]["despeckle"] is False
    assert latest["palette"]["version"] == "2026-09-rala-p3d"
    # Source product and QC flags are the RALA spec, not a new field.
    assert get_product("rala").mrms_name == "ReflectivityAtLowestAltitude"
    assert get_product("rala").apply_dbz_floor is False

    third = cook(cfg, source="mrms", upload=False)
    assert third["skipped"] == "unchanged"
    assert third["archive_cooked"] == 0
    assert third["frame_id"] == frame_id_for(times[0])

    # A palette bump repaints the real scan; it does not invent a new one.
    stale_path = mode_dir / frame_id_for(times[0]) / "frame.json"
    stale_meta = json.loads(stale_path.read_text())
    stale_meta["palette"]["version"] = "2026-09-rala-p2d"
    stale_path.write_text(json.dumps(stale_meta))
    repaint = cook(cfg, source="mrms", upload=False)
    assert repaint["archive_cooked"] == 1
    assert repaint["frame_id"] == frame_id_for(times[0])
    refreshed = json.loads(stale_path.read_text())
    assert refreshed["palette"]["version"] == "2026-09-rala-p3d"
    assert refreshed["valid_time"].startswith(times[0].strftime("%Y-%m-%dT%H:%M:%S"))


def test_empty_relist_does_not_drop_the_two_minute_queue(tmp_path: Path, monkeypatch):
    """A later ListObjects that returns nothing must not stop the catch-up."""
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    times = [now - timedelta(minutes=minutes) for minutes in (0, 2, 4, 6)]

    def _key(when: datetime) -> str:
        stamp = when.strftime("%Y%m%d-%H%M%S")
        return (
            "CONUS/ReflectivityAtLowestAltitude_00.50/"
            f"{when:%Y%m%d}/MRMS_ReflectivityAtLowestAltitude_00.50_{stamp}.grib2.gz"
        )

    scans = [type("Scan", (), {"valid_time": when, "key": _key(when)})() for when in times]
    calls = {"n": 0}

    def _list(cfg, max_age, now=None):
        calls["n"] += 1
        if calls["n"] == 1:
            return list(scans)
        return []

    monkeypatch.setattr("mpwg_radar.cooker.list_recent_s3_scans", _list)
    monkeypatch.setattr("mpwg_radar.cooker._load_ncep_latest_frame", lambda cfg: None)

    def _download(cfg, key, dest):
        dest.mkdir(parents=True, exist_ok=True)
        path = dest / Path(key).name
        path.write_bytes(b"grib")
        return path

    monkeypatch.setattr("mpwg_radar.cooker.download_s3_key", _download)
    decoded = []

    def fake_decode(path, bbox=None, product=None):
        name = Path(path).name
        when = next(item.valid_time for item in scans if item.key.endswith(name))
        decoded.append(when)
        return synthetic_central_texas(valid_time=when)

    monkeypatch.setattr("mpwg_radar.cooker.decode_grib2", fake_decode)
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
        upload=False,
        product_id="rala",
        rala_catchup_budget_seconds=1500,
        tile_workers=1,
    )
    result = cook(cfg, source="mrms", upload=False)
    assert calls["n"] > 1
    assert result["archive_listed"] == 4
    assert result["archive_cooked"] == 4
    assert result["archive_pending"] == 0
    assert decoded == times
    manifest = json.loads((tmp_path / "radar" / "manifest.json").read_text())
    listed = [item["id"] for item in manifest["products"]["rala"]["modes"]["clean"]["frames"]]
    assert listed == [frame_id_for(when) for when in times]


def test_complete_frames_are_skipped_newest_hole_first(tmp_path: Path, monkeypatch):
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    times = [now - timedelta(minutes=minutes) for minutes in (0, 2, 4, 6)]

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
    decoded = []

    def fake_decode(path, bbox=None, product=None):
        name = Path(path).name
        when = next(item.valid_time for item in scans if item.key.endswith(name))
        decoded.append(when)
        return synthetic_central_texas(valid_time=when)

    monkeypatch.setattr("mpwg_radar.cooker.decode_grib2", fake_decode)
    # The scan 4 minutes back is already on disk with the live palette.
    # It must not be decoded again. The newer hole is cooked first.
    held = times[2]
    mode_dir = tmp_path / "radar" / "rala" / "clean"
    held_id = _seed_frame(mode_dir, held)
    meta_path = mode_dir / held_id / "frame.json"
    meta = json.loads(meta_path.read_text())
    meta["palette"] = {"version": "2026-09-rala-p3d"}
    meta_path.write_text(json.dumps(meta))
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
        upload=False,
        product_id="rala",
        palette_id="mpwg-rala-2026-09",
        rala_catchup_budget_seconds=0,
        tile_workers=1,
    )
    first = cook(cfg, source="mrms", upload=False)
    assert first["archive_cooked"] == 1
    assert first["frame_id"] == frame_id_for(times[0])
    assert decoded == [times[0]]
    cfg.rala_catchup_budget_seconds = 1500
    second = cook(cfg, source="mrms", upload=False)
    assert held not in decoded
    assert decoded == [times[0], times[1], times[3]]
    assert second["archive_cooked"] == 2
    assert second["archive_pending"] == 0


def test_composite_cook_does_not_list_the_rala_archive(tmp_path: Path, monkeypatch):
    def boom(*_args, **_kwargs):
        raise AssertionError("composite cook must not list the RALA archive")

    monkeypatch.setattr("mpwg_radar.cooker.list_recent_s3_scans", boom)
    monkeypatch.setattr(
        "mpwg_radar.cooker._load_frame",
        lambda cfg, source, grib_path: synthetic_central_texas(),
    )
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
        upload=False,
        product_id="composite",
    )
    result = cook(cfg, source="mrms", upload=False)
    assert result["product_id"] == "composite"
    assert "archive_cooked" not in result


def test_list_recent_s3_scans_paginates_and_drops_old_files(monkeypatch):
    now = datetime(2026, 9, 22, 15, 10, tzinfo=timezone.utc)
    day = "20260922"
    prefix = "CONUS/ReflectivityAtLowestAltitude_00.50"
    old = f"{prefix}/{day}/MRMS_ReflectivityAtLowestAltitude_00.50_20260922-130000.grib2.gz"
    keep_a = f"{prefix}/{day}/MRMS_ReflectivityAtLowestAltitude_00.50_20260922-140000.grib2.gz"
    keep_b = f"{prefix}/{day}/MRMS_ReflectivityAtLowestAltitude_00.50_20260922-150200.grib2.gz"
    calls: list[str] = []

    def listing(keys: list[str], truncated: bool = False, token: str | None = None) -> bytes:
        contents = "".join(f"<Contents><Key>{key}</Key></Contents>" for key in keys)
        token_xml = (
            f"<NextContinuationToken>{token}</NextContinuationToken>" if token else ""
        )
        trunc = "true" if truncated else "false"
        return (
            '<?xml version="1.0"?>'
            '<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
            f"<IsTruncated>{trunc}</IsTruncated>{token_xml}{contents}"
            "</ListBucketResult>"
        ).encode()

    def fake_request(url, timeout, user_agent):
        calls.append(url)
        if "20260921" in url:
            return listing([])
        if "continuation-token" not in url:
            return listing([old], truncated=True, token="next/page+1")
        return listing([keep_a, keep_b, f"{prefix}/{day}/not-a-grib.txt"])

    monkeypatch.setattr("mpwg_radar.ingest._request", fake_request)
    cfg = CookerConfig(product_id="rala")
    scans = list_recent_s3_scans(cfg, max_age=timedelta(minutes=75), now=now)
    assert [scan.key for scan in scans] == [keep_b, keep_a]
    assert any("continuation-token=" in url for url in calls)
    assert any("next%2Fpage%2B1" in url for url in calls)


def test_list_recent_s3_scans_keeps_every_two_minute_key(monkeypatch):
    """Intermediate 2-minute objects on a later page are part of the window."""
    now = datetime(2026, 9, 22, 16, 0, tzinfo=timezone.utc)
    day = "20260922"
    prefix = "CONUS/ReflectivityAtLowestAltitude_00.50"
    stamps = [
        (now - timedelta(minutes=minutes)).strftime("%Y%m%d-%H%M%S")
        for minutes in range(0, 16, 2)
    ]
    # 0,2,4,6,8,10,12,14 → 8 real scans. Lex order is oldest first, so the
    # first page is the back of the hour and the tip is on the next page.
    keys = [
        f"{prefix}/{day}/MRMS_ReflectivityAtLowestAltitude_00.50_{stamp}.grib2.gz"
        for stamp in reversed(stamps)
    ]
    outside = (
        f"{prefix}/{day}/MRMS_ReflectivityAtLowestAltitude_00.50_"
        "20260922-140000.grib2.gz"
    )

    def listing(page_keys, truncated=False, token=None) -> bytes:
        contents = "".join(f"<Contents><Key>{key}</Key></Contents>" for key in page_keys)
        token_xml = f"<NextContinuationToken>{token}</NextContinuationToken>" if token else ""
        trunc = "true" if truncated else "false"
        return (
            '<?xml version="1.0"?>'
            '<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
            f"<IsTruncated>{trunc}</IsTruncated>{token_xml}{contents}"
            "</ListBucketResult>"
        ).encode()

    def fake_request(url, timeout, user_agent):
        if "20260921" in url:
            return listing([])
        if "continuation-token" not in url:
            return listing([outside, keys[0], keys[1]], truncated=True, token="page-2")
        return listing(keys[2:])

    monkeypatch.setattr("mpwg_radar.ingest._request", fake_request)
    cfg = CookerConfig(product_id="rala")
    scans = list_recent_s3_scans(cfg, max_age=timedelta(minutes=75), now=now)
    assert len(scans) == 8
    assert outside not in [scan.key for scan in scans]
    got = [scan.valid_time for scan in scans]
    assert got == sorted(got, reverse=True)
    gaps = [(got[i] - got[i + 1]).total_seconds() for i in range(len(got) - 1)]
    assert gaps == [120.0] * (len(got) - 1)
    assert scans[0].valid_time == now
    assert scans[-1].valid_time == now - timedelta(minutes=14)
