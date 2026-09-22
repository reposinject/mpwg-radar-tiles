"""Cook one MRMS (or synthetic) frame into 512px XYZ tiles + manifests."""

from __future__ import annotations

import fcntl
import json
import logging
import os
import shutil
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Tuple

import numpy as np

from mpwg_radar.config import CookerConfig, load_config
from mpwg_radar.geo import count_tiles, tiles_by_zoom
from mpwg_radar.grib import ReflectivityFrame, decode_grib2, frame_id_for
from mpwg_radar.ingest import (
    IngestError,
    MrmsScan,
    download_latest_mrms,
    download_ncep_latest,
    download_s3_key,
    list_recent_s3_scans,
)
from mpwg_radar.palette import Palette, load_palette
from mpwg_radar.products import DEFAULT_PRODUCT_ID, PRODUCTS, ProductSpec
from mpwg_radar.publish import R2Publisher, UploadStats
from mpwg_radar.qc import MODES, apply_mode
from mpwg_radar.synthetic import synthetic_central_texas
from mpwg_radar.tiles import write_colorbar, write_tiles

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class FrameRetention:
    """Rolling archive limits for one product.

    ``max_age_seconds`` None means count-only (composite). RALA sets an age
    window and a frame ceiling above the expected ~2-minute count so the
    ceiling does not subsample. Frames are kept in full; nothing is decimated.
    """

    max_frames: int
    max_age_seconds: Optional[float] = None


def frame_retention(cfg: CookerConfig) -> FrameRetention:
    """RALA uses its own window. Composite stays on ``retention_frames``."""
    if cfg.product_id == "rala":
        return FrameRetention(
            max_frames=max(1, int(cfg.rala_retention_frames)),
            max_age_seconds=float(cfg.rala_retention_minutes) * 60.0,
        )
    return FrameRetention(max_frames=max(1, int(cfg.retention_frames)))


def _now() -> datetime:
    return datetime.now(timezone.utc)


# A frame valid at exactly 60 minutes can tick a few seconds past the hour
# while the manifest is written. Count it; do not count the next scan.
_HOUR_COUNT_SKEW_SECONDS = 5.0


def _frames_in_last_hour(frames: List[Dict], now: datetime) -> int:
    """How many listed frames have a valid_time inside the last 60 minutes."""
    limit = 3600.0 + _HOUR_COUNT_SKEW_SECONDS
    count = 0
    for item in frames:
        when = _parse_time(item.get("valid_time"))
        if when is None:
            continue
        if (now - when).total_seconds() <= limit:
            count += 1
    return count


def _parse_time(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    text = str(value).strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def apply_retention(
    frames: List[Dict],
    policy: FrameRetention,
    now: Optional[datetime] = None,
) -> List[Dict]:
    """Return the frames that stay in the archive, newest first.

    Scans outside the age window are dropped and do not consume ``max_frames``.
    The cap then keeps the newest survivors. It does not keep every Nth frame.
    """
    moment = now or _now()

    def _when(item: Dict) -> datetime:
        return _parse_time(item.get("valid_time")) or datetime.min.replace(
            tzinfo=timezone.utc
        )

    ordered = sorted(frames, key=_when, reverse=True)
    kept: List[Dict] = []
    for item in ordered:
        if policy.max_age_seconds is not None:
            when = _parse_time(item.get("valid_time"))
            if when is None:
                continue
            age = (moment - when).total_seconds()
            if age > policy.max_age_seconds or age < -600:
                continue
        kept.append(item)
        if len(kept) >= policy.max_frames:
            break
    return kept


def cook(
    cfg: Optional[CookerConfig] = None,
    *,
    source: str = "mrms",
    grib_path: Optional[Path] = None,
    upload: Optional[bool] = None,
    archive: bool = True,
    loaded_frame: Optional[ReflectivityFrame] = None,
) -> Dict:
    cfg = cfg or load_config()
    # RALA live cooks fill a rolling window of real scans. Composite, synthetic,
    # and an explicit GRIB path stay single-frame.
    if (
        archive
        and loaded_frame is None
        and source == "mrms"
        and grib_path is None
        and cfg.product_id == "rala"
    ):
        return _cook_rala_archive(cfg, upload=upload)
    product = cfg.product
    palette = load_palette(cfg.palette_id)
    if cfg.display_min_dbz is not None:
        palette = palette.with_display_min(cfg.display_min_dbz)
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    cfg.output_dir.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    log.info(
        "Cook start product=%s mrms=%s region=%s bbox=%s z%s-%s candidate_tiles=%d "
        "by_zoom=%s tile_size=%d palette=%s display_min=%s modes=%s apply_dbz_floor=%s",
        product.id,
        product.mrms_name,
        cfg.region_name,
        cfg.bbox.as_dict(),
        cfg.min_zoom,
        cfg.max_zoom,
        count_tiles(cfg.bbox, cfg.min_zoom, cfg.max_zoom),
        tiles_by_zoom(cfg.bbox, cfg.min_zoom, cfg.max_zoom),
        cfg.tile_size,
        palette.id,
        palette.min_dbz,
        cfg.modes,
        product.apply_dbz_floor,
    )

    frame = (
        loaded_frame
        if loaded_frame is not None
        else _load_frame(cfg, source=source, grib_path=grib_path)
    )
    fetched_at = _now()
    source_age = (fetched_at - frame.valid_time.astimezone(timezone.utc)).total_seconds()
    log.info(
        "source product=%s source_valid_time=%s fetched_at=%s source_age_s=%.1f",
        product.id,
        _iso(frame.valid_time),
        _iso(fetched_at),
        source_age,
    )
    radar_root = cfg.output_dir / "radar"
    radar_root.mkdir(parents=True, exist_ok=True)

    should_upload = cfg.upload if upload is None else upload
    if _already_published(cfg, radar_root, product, frame.frame_id, should_upload):
        finished = _now()
        lag = (finished - frame.valid_time.astimezone(timezone.utc)).total_seconds()
        log.info(
            "latency product=%s source_valid_time=%s cook_finished_at=%s "
            "upload_finished_at=%s source_age_s=%.1f cook_s=%.1f upload_s=0.0 "
            "lag_s=%.1f skipped=unchanged",
            product.id,
            _iso(frame.valid_time),
            _iso(finished),
            "-",
            source_age,
            time.monotonic() - started,
            lag,
        )
        result = {
            "frame_id": frame.frame_id,
            "valid_time": _iso(frame.valid_time),
            "source_valid_time": _iso(frame.valid_time),
            "fetched_at": _iso(fetched_at),
            "cook_finished_at": _iso(finished),
            "upload_finished_at": None,
            "source_age_seconds": round(source_age, 1),
            "cook_seconds": round(time.monotonic() - started, 2),
            "upload_seconds": 0.0,
            "lag_seconds": round(lag, 1),
            "skipped": "unchanged",
            "source": frame.source,
            "product": frame.product,
            "product_id": product.id,
            "region": cfg.region_name,
            "modes": [],
            "manifest": str((radar_root / "manifest.json").resolve()),
            "uploaded": 0,
            "upload_skipped": 0,
            "upload_duration_seconds": 0.0,
            "palette": palette.id,
            "display_min_dbz": palette.min_dbz,
        }
        _write_status(cfg, product, result)
        return result

    if cfg.keep_dbz:
        # Per-product path: composite and RALA often share a valid_time.
        _write_dbz(
            cfg.output_dir / "dbz" / product.id / f"{frame.frame_id}.npz",
            frame,
        )

    mode_summaries = []
    promoted_any = False
    for mode in cfg.modes:
        cooked = apply_mode(
            frame,
            mode,
            min_dbz=product.min_dbz_override,
            apply_dbz_floor=product.apply_dbz_floor,
            apply_despeckle=product.apply_despeckle,
            edge_aware=product.edge_aware_smooth,
            apply_grid_smooth=product.apply_grid_smooth,
        )
        summary, promoted = _write_mode(cfg, palette, cooked, mode, radar_root, product)
        mode_summaries.append(summary)
        promoted_any = promoted_any or promoted

    colorbar_rel = product.colorbar_rel()
    write_colorbar(palette, radar_root / colorbar_rel)
    if product.id == DEFAULT_PRODUCT_ID:
        write_colorbar(palette, radar_root / "colorbar.png")
    cook_finished_at = _now()
    cook_seconds = time.monotonic() - started
    # Local manifest first (no upload stamp). A failed upload must not look published.
    _write_manifest(
        cfg,
        palette,
        radar_root,
        frame,
        mode_summaries,
        product,
        cook_finished_at=cook_finished_at,
        upload_finished_at=None,
        promoted=promoted_any,
    )
    stale = _stale_frame_ids(cfg, radar_root, product)
    _prune_old_frames(cfg, radar_root, product)

    upload_stats = UploadStats()
    upload_finished_at: Optional[datetime] = None
    if should_upload and cfg.r2.enabled:
        publisher = R2Publisher(cfg.r2)
        # Tiles are product-prefixed. Hold the manifest lock only around the
        # final merge + manifest PUT so a parallel RALA/composite cook cannot
        # publish a stale products block.
        upload_stats = publisher.upload_frame(
            radar_root,
            frame_id=frame.frame_id,
            modes=cfg.modes,
            product_id=product.id,
            include_manifest=False,
            include_latest=promoted_any,
            # Promoted RALA frames already PUT the frame tiles. Copy those
            # bytes onto latest/ server-side instead of uploading them twice.
            copy_latest=(product.id == "rala" and promoted_any),
        )
        # Stamp the manifest just before its PUT. The latency line below
        # uses the clock after that PUT returns.
        manifest_upload_started = _now()
        manifest_stats = UploadStats()

        def _put_manifest() -> None:
            nonlocal manifest_stats
            manifest_stats = publisher.upload_manifest(radar_root)
            if promoted_any:
                (radar_root / f".published-{product.id}").write_text(frame.frame_id + "\n")

        _write_manifest(
            cfg,
            palette,
            radar_root,
            frame,
            mode_summaries,
            product,
            cook_finished_at=cook_finished_at,
            upload_finished_at=manifest_upload_started,
            before_unlock=_put_manifest,
            promoted=promoted_any,
        )
        upload_finished_at = _now()
        _add_upload_stats(upload_stats, manifest_stats)
        _mark_frame_uploaded(cfg, radar_root, product, frame.frame_id)
        for mode, ids in stale.items():
            for stale_id in ids:
                publisher.delete_prefix(product.tile_url_template(mode, stale_id).split("/{z}")[0])
    elif should_upload and not cfg.r2.enabled:
        log.info("R2 env not set — cooked locally, skipped upload")

    finished = upload_finished_at or cook_finished_at
    lag = (finished - frame.valid_time.astimezone(timezone.utc)).total_seconds()
    upload_seconds = upload_stats.duration_seconds
    log.info(
        "latency product=%s source_valid_time=%s cook_finished_at=%s "
        "upload_finished_at=%s source_age_s=%.1f cook_s=%.1f upload_s=%.1f "
        "lag_s=%.1f skipped=no",
        product.id,
        _iso(frame.valid_time),
        _iso(cook_finished_at),
        _iso(upload_finished_at) if upload_finished_at else "-",
        source_age,
        cook_seconds,
        upload_seconds,
        lag,
    )
    result = {
        "frame_id": frame.frame_id,
        "valid_time": _iso(frame.valid_time),
        "source_valid_time": _iso(frame.valid_time),
        "fetched_at": _iso(fetched_at),
        "cook_finished_at": _iso(cook_finished_at),
        "upload_finished_at": _iso(upload_finished_at) if upload_finished_at else None,
        "source_age_seconds": round(source_age, 1),
        "cook_seconds": round(cook_seconds, 2),
        "upload_seconds": round(upload_seconds, 2),
        "lag_seconds": round(lag, 1),
        "skipped": None,
        "source": frame.source,
        "product": frame.product,
        "product_id": product.id,
        "region": cfg.region_name,
        "modes": mode_summaries,
        "manifest": str((radar_root / "manifest.json").resolve()),
        "uploaded": upload_stats.uploaded,
        "upload_skipped": upload_stats.skipped,
        "upload_duration_seconds": round(upload_stats.duration_seconds, 2),
        "palette": palette.id,
        "display_min_dbz": palette.min_dbz,
    }
    _write_status(cfg, product, result)
    log.info("Cook complete product=%s frame=%s modes=%s", product.id, frame.frame_id, cfg.modes)
    return result


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat()


@contextmanager
def manifest_lock(radar_root: Path) -> Iterator[None]:
    """Exclusive lock for manifest read-modify-write and the manifest PUT.

    Composite and RALA cook in parallel. Tile keys do not overlap; manifest.json
    does. flock is per-process, so callers must not nest this on a second fd.
    """
    radar_root.mkdir(parents=True, exist_ok=True)
    lock_path = radar_root / ".manifest.lock"
    with lock_path.open("a+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _already_published(
    cfg: CookerConfig,
    radar_root: Path,
    product: ProductSpec,
    frame_id: str,
    should_upload: bool,
) -> bool:
    """Skip a retile when this product's frame is already cooked.

    With R2 enabled, skip only after `.published-<product>` is written, which
    happens once the manifest PUT returns. A cook that died during upload
    retries instead of treating the local manifest as fresh.
    """
    path = radar_root / "manifest.json"
    if not path.is_file():
        return False
    try:
        manifest = json.loads(path.read_text())
    except json.JSONDecodeError:
        return False
    entry = (manifest.get("products") or {}).get(product.id) or {}
    published = entry.get("latest_frame")
    if not published:
        modes = entry.get("modes") or {}
        ids = [
            (modes.get(mode) or {}).get("latest_frame")
            for mode in cfg.modes
            if (modes.get(mode) or {}).get("latest_frame")
        ]
        if ids and all(item == ids[0] for item in ids):
            published = ids[0]
    if published != frame_id:
        return False
    # A palette bump has to retile. Matching the frame id is not enough when
    # the stamp on disk is an older ramp.
    meta_path = product.mode_dir(radar_root, cfg.modes[0]) / frame_id / "frame.json"
    if meta_path.is_file():
        try:
            stamped = (json.loads(meta_path.read_text()).get("palette") or {}).get("version")
        except json.JSONDecodeError:
            return False
        if stamped and stamped != load_palette(cfg.palette_id).version:
            return False
    if should_upload and cfg.r2.enabled:
        # Written only after the manifest PUT returns. A local manifest that
        # already names this frame is not enough: the CDN copy may have failed.
        marker = radar_root / f".published-{product.id}"
        return marker.is_file() and marker.read_text().strip() == frame_id
    return True


def _add_upload_stats(total: UploadStats, extra: UploadStats) -> None:
    total.started += extra.started
    total.uploaded += extra.uploaded
    total.failed += extra.failed
    total.skipped += extra.skipped
    total.duration_seconds += extra.duration_seconds
    total.keys.extend(extra.keys)
    total.errors.extend(extra.errors)


def _write_status(cfg: CookerConfig, product: ProductSpec, result: Dict) -> None:
    payload = json.dumps(result, indent=2) + "\n"
    (cfg.output_dir / f"status-{product.id}.json").write_text(payload)
    # status.json stays the composite cook so a parallel RALA run cannot
    # clobber the production status operators already tail.
    if product.id == DEFAULT_PRODUCT_ID:
        (cfg.output_dir / "status.json").write_text(payload)


def _load_frame(
    cfg: CookerConfig, source: str, grib_path: Optional[Path]
) -> ReflectivityFrame:
    source = source.lower()
    if source == "synthetic":
        return synthetic_central_texas()
    if source == "nexrad":
        from mpwg_radar.sources import NexradSource

        return NexradSource().load()
    if source != "mrms":
        raise ValueError(f"Unknown source {source!r}")
    path = Path(grib_path) if grib_path else download_latest_mrms(cfg, cfg.data_dir)
    return decode_grib2(path, bbox=cfg.bbox, product=cfg.product.mrms_name)


def _write_dbz(path: Path, frame: ReflectivityFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(
        path,
        dbz=frame.dbz,
        category=frame.category if frame.category is not None else np.array([]),
        lat=frame.lat,
        lon=frame.lon,
        valid_time=frame.valid_time.astimezone(timezone.utc).isoformat(),
        product=frame.product,
    )
    log.info("Wrote physical dBZ crop %s shape=%s", path, frame.dbz.shape)


def _latest_id(latest_dir: Path) -> Optional[str]:
    path = latest_dir / "frame.json"
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text()).get("id")
    except json.JSONDecodeError:
        return None
    text = str(raw or "").strip()
    return text or None


def _write_mode(
    cfg: CookerConfig,
    palette: Palette,
    frame: ReflectivityFrame,
    mode: str,
    radar_root: Path,
    product: ProductSpec,
) -> Tuple[Dict, bool]:
    mode_dir = product.mode_dir(radar_root, mode)
    frame_dir = mode_dir / frame.frame_id
    if frame_dir.exists():
        shutil.rmtree(frame_dir)
    stats = write_tiles(
        frame,
        palette,
        frame_dir,
        bbox=cfg.bbox,
        min_zoom=cfg.min_zoom,
        max_zoom=cfg.max_zoom,
        tile_size=cfg.tile_size,
        skip_empty=cfg.skip_empty_tiles,
        sample_mode=product.sample_mode,
        workers=max(1, int(cfg.tile_workers)),
    )
    meta = {
        "id": frame.frame_id,
        "product": frame.product,
        "product_id": product.id,
        "source": frame.source,
        "valid_time": frame.valid_time.astimezone(timezone.utc).isoformat(),
        "mode": mode,
        "mode_spec": {
            "min_dbz": MODES[mode].min_dbz if product.apply_dbz_floor else None,
            "apply_dbz_floor": product.apply_dbz_floor,
            "despeckle": MODES[mode].despeckle and product.apply_despeckle,
            "smooth": MODES[mode].smooth,
            "smooth_kind": (
                "masked-splat"
                if product.sample_mode == "masked-splat"
                else "edge-aware"
                if product.edge_aware_smooth and MODES[mode].smooth and product.apply_grid_smooth
                else "mild-3x3"
                if MODES[mode].smooth and product.apply_grid_smooth
                else "none"
            ),
            "sample": product.sample_mode,
            "description": MODES[mode].description,
        },
        "palette": palette.as_dict(),
        "region": cfg.region_name,
        "bbox": cfg.bbox.as_dict(),
        "tile_size": cfg.tile_size,
        "min_zoom": cfg.min_zoom,
        "max_zoom": cfg.max_zoom,
        "scheme": "xyz",
        "crs": "EPSG:3857",
        "tiles": "{z}/{x}/{y}.png",
        "tile_url_template": product.tile_url_template(mode, frame.frame_id),
        "stats": stats,
        "cooked_at": _now().isoformat(),
        "attribution": product.attribution,
    }
    (frame_dir / "frame.json").write_text(json.dumps(meta, indent=2) + "\n")

    latest_dir = mode_dir / "latest"
    current = _latest_id(latest_dir)
    # Backfill of an older scan must not point `latest` backward.
    promoted = current is None or frame.frame_id >= current
    if promoted:
        _alias_frame_tree(frame_dir, latest_dir)
        latest_meta = dict(meta)
        latest_meta["tile_url_template"] = product.tile_url_template(mode, "latest")
        latest_meta["alias"] = "latest"
        (latest_dir / "frame.json").write_text(json.dumps(latest_meta, indent=2) + "\n")
    return meta, promoted


def _alias_frame_tree(src: Path, dest: Path) -> None:
    """Publish ``latest/`` without copying every PNG.

    A CONUS frame is hundreds of tiles. ``shutil.copytree`` doubled that
    write before upload, and the RALA path then CopyObject's the same bytes
    on R2. Hardlink the PNGs (frame.json stays a real file so the alias
    metadata cannot rewrite the archived frame). Fall back to a copy when
    the filesystem refuses the link.
    """
    if dest.exists():
        shutil.rmtree(dest)
    for path in src.rglob("*"):
        rel = path.relative_to(src)
        target = dest / rel
        if path.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        if path.suffix == ".png":
            try:
                os.link(path, target)
                continue
            except OSError:
                pass
        shutil.copy2(path, target)


def _write_manifest(
    cfg: CookerConfig,
    palette: Palette,
    radar_root: Path,
    frame: ReflectivityFrame,
    mode_summaries: List[Dict],
    product: ProductSpec,
    *,
    cook_finished_at: Optional[datetime] = None,
    upload_finished_at: Optional[datetime] = None,
    before_unlock=None,
    promoted: bool = True,
    _locked: bool = False,
) -> Dict:
    """Merge this product into manifest.json.

    The file lock is held for the merge and for `before_unlock` (manifest PUT).
    """
    if not _locked:
        with manifest_lock(radar_root):
            manifest = _write_manifest(
                cfg,
                palette,
                radar_root,
                frame,
                mode_summaries,
                product,
                cook_finished_at=cook_finished_at,
                upload_finished_at=upload_finished_at,
                promoted=promoted,
                _locked=True,
            )
            if before_unlock is not None:
                before_unlock()
            return manifest

    path = radar_root / "manifest.json"
    existing: Dict = {}
    if path.is_file():
        try:
            existing = json.loads(path.read_text())
        except json.JSONDecodeError:
            existing = {}

    policy = frame_retention(cfg)
    this_modes = {}
    for summary in mode_summaries:
        mode = summary["mode"]
        mode_dir = product.mode_dir(radar_root, mode)
        frames = apply_retention(
            _list_frames(mode_dir, product, mode),
            policy,
        )
        tip = _latest_id(mode_dir / "latest") or summary["id"]
        this_modes[mode] = {
            "latest": product.tile_url_template(mode, "latest"),
            "latest_frame": tip,
            "frames": frames,
        }

    products_block = dict(existing.get("products") or {})
    for pid, spec in PRODUCTS.items():
        entry = dict(products_block.get(pid) or spec.as_public_dict())
        entry.update(spec.as_public_dict())
        entry["default"] = pid == DEFAULT_PRODUCT_ID
        if pid == product.id:
            entry["palette"] = palette.as_dict()
            entry["display_min_dbz"] = palette.min_dbz
            entry["modes"] = this_modes
            entry["latest"] = product.tile_url_template(cfg.modes[0], "latest")
            if product.id == "rala":
                primary_frames = this_modes.get(cfg.modes[0], {}).get("frames") or []
                entry["retention"] = {
                    "max_age_minutes": cfg.rala_retention_minutes,
                    "max_frames": cfg.rala_retention_frames,
                    "frames_last_60_minutes": _frames_in_last_hour(primary_frames, _now()),
                }
            # An older backfill frame refreshes the frame list only. The live
            # tip stays on the newer scan already aliased as latest/.
            if promoted or not entry.get("latest_frame"):
                tip = this_modes.get(cfg.modes[0], {}).get("latest_frame") or frame.frame_id
                entry["latest_frame"] = tip
                valid = _iso(frame.valid_time)
                entry["latest_valid_time"] = valid
                entry["source_valid_time"] = valid
                cooked = cook_finished_at or _now()
                entry["cook_finished_at"] = _iso(cooked)
                if upload_finished_at is not None:
                    entry["upload_finished_at"] = _iso(upload_finished_at)
                    lag_from = upload_finished_at
                else:
                    entry["upload_finished_at"] = None
                    lag_from = cooked
                entry["lag_seconds"] = round(
                    (lag_from - frame.valid_time.astimezone(timezone.utc)).total_seconds(),
                    1,
                )
            entry["available"] = True
        else:
            entry.setdefault("available", bool(entry.get("modes")))
        products_block[pid] = entry

    # Top-level modes stay the composite tree so existing clients keep working.
    if product.id == DEFAULT_PRODUCT_ID:
        top_modes = this_modes
        top_palette = palette.as_dict()
        top_default_mode = cfg.modes[0]
        top_valid = frame.valid_time.astimezone(timezone.utc).isoformat()
    else:
        top_modes = existing.get("modes") or {}
        top_palette = existing.get("palette") or top_modes and existing.get("palette") or palette.as_dict()
        if product.id != DEFAULT_PRODUCT_ID and DEFAULT_PRODUCT_ID in products_block:
            comp = products_block[DEFAULT_PRODUCT_ID]
            top_modes = comp.get("modes") or top_modes
            top_palette = comp.get("palette") or top_palette
        top_default_mode = existing.get("default_mode") or "clean"
        top_valid = existing.get("latest_valid_time") or frame.valid_time.astimezone(
            timezone.utc
        ).isoformat()

    manifest = {
        "product": "mpwg-radar",
        "version": "1.1.0",
        "region": cfg.region_name,
        "bbox": cfg.bbox.as_dict(),
        "tile_size": cfg.tile_size,
        "min_zoom": cfg.min_zoom,
        "max_zoom": cfg.max_zoom,
        "scheme": "xyz",
        "crs": "EPSG:3857",
        "palette": top_palette,
        "default_mode": top_default_mode,
        "default_product": DEFAULT_PRODUCT_ID,
        "cooked_product": product.id,
        "products": products_block,
        "modes": top_modes,
        "updated_at": _now().isoformat(),
        "latest_valid_time": top_valid,
        "attribution": "Radar: NOAA MRMS. Palette: MPWG Clean (James Sep 2026).",
    }
    path.write_text(json.dumps(manifest, indent=2) + "\n")
    log.info("Wrote %s default_product=%s cooked=%s", path, DEFAULT_PRODUCT_ID, product.id)
    return manifest


def _list_frames(
    mode_dir: Path, product: Optional[ProductSpec] = None, mode: str = ""
) -> List[Dict]:
    frames = []
    if not mode_dir.is_dir():
        return frames
    for child in sorted(mode_dir.iterdir(), reverse=True):
        if not child.is_dir() or child.name == "latest":
            continue
        meta_path = child / "frame.json"
        if not meta_path.is_file():
            continue
        meta = json.loads(meta_path.read_text())
        if product is not None and mode:
            tiles = product.tile_url_template(mode, child.name)
            frame_rel = (
                f"{product.tile_prefix}/{mode}/{child.name}/frame.json"
                if product.tile_prefix
                else f"{mode}/{child.name}/frame.json"
            )
        else:
            tiles = f"{mode_dir.name}/{child.name}/{{z}}/{{x}}/{{y}}.png"
            frame_rel = f"{mode_dir.name}/{child.name}/frame.json"
        frames.append(
            {
                "id": meta.get("id", child.name),
                "valid_time": meta.get("valid_time"),
                "tiles": tiles,
                "frame": frame_rel,
            }
        )
    return frames


def _stale_frame_ids(
    cfg: CookerConfig, radar_root: Path, product: ProductSpec
) -> Dict[str, List[str]]:
    stale: Dict[str, List[str]] = {}
    for mode in cfg.modes:
        stale[mode] = _frame_ids_outside_retention(cfg, radar_root, product, mode)
    return stale


def _frame_ids_outside_retention(
    cfg: CookerConfig,
    radar_root: Path,
    product: ProductSpec,
    mode: str,
) -> List[str]:
    frames = _list_frames(product.mode_dir(radar_root, mode), product, mode)
    kept = {item["id"] for item in apply_retention(frames, frame_retention(cfg))}
    return [item["id"] for item in frames if item["id"] not in kept]


def _prune_old_frames(
    cfg: CookerConfig, radar_root: Path, product: ProductSpec
) -> None:
    for mode in cfg.modes:
        mode_dir = product.mode_dir(radar_root, mode)
        if not mode_dir.is_dir():
            continue
        for frame_id in _frame_ids_outside_retention(cfg, radar_root, product, mode):
            extra = mode_dir / frame_id
            if not extra.is_dir():
                continue
            log.info("Pruning local frame %s", extra)
            shutil.rmtree(extra, ignore_errors=True)
            marker = mode_dir / f".uploaded-{frame_id}"
            if marker.is_file():
                marker.unlink()


@dataclass
class _ScanJob:
    valid_time: datetime
    frame_id: str
    key: Optional[str] = None
    loaded: Optional[ReflectivityFrame] = None


def _need_upload(cfg: CookerConfig, upload: Optional[bool]) -> bool:
    should_upload = cfg.upload if upload is None else upload
    return bool(should_upload and cfg.r2.enabled)


def _frame_is_complete(
    cfg: CookerConfig,
    radar_root: Path,
    product: ProductSpec,
    frame_id: str,
    need_upload: bool,
    palette_version: Optional[str] = None,
) -> bool:
    """A scan is done when every mode has tiles and, if uploading, an upload marker.

    The marker lives beside the frame directory so it is not part of the tile
    tree that gets PUT to R2. A palette version mismatch is incomplete so a
    ramp change repaints real scans instead of leaving the old colors up.
    """
    for mode in cfg.modes:
        meta_path = product.mode_dir(radar_root, mode) / frame_id / "frame.json"
        if not meta_path.is_file():
            return False
        if palette_version is not None:
            try:
                meta = json.loads(meta_path.read_text())
            except json.JSONDecodeError:
                return False
            stamped = (meta.get("palette") or {}).get("version")
            if stamped != palette_version:
                return False
        mode_dir = product.mode_dir(radar_root, mode)
        if need_upload and not (mode_dir / f".uploaded-{frame_id}").is_file():
            return False
    return True


def _mark_frame_uploaded(
    cfg: CookerConfig, radar_root: Path, product: ProductSpec, frame_id: str
) -> None:
    for mode in cfg.modes:
        mode_dir = product.mode_dir(radar_root, mode)
        mode_dir.mkdir(parents=True, exist_ok=True)
        (mode_dir / f".uploaded-{frame_id}").write_text(frame_id + "\n")


def _load_ncep_latest_frame(cfg: CookerConfig) -> Optional[ReflectivityFrame]:
    """Best-effort NCEP `.latest` decode. S3 is the archive; NCEP may be ahead."""
    try:
        path = download_ncep_latest(cfg, cfg.data_dir)
        return decode_grib2(path, bbox=cfg.bbox, product=cfg.product.mrms_name)
    except Exception as exc:  # noqa: BLE001 — listing still stands if NCEP is down
        log.warning("RALA NCEP latest unavailable for the archive (%s)", exc)
        return None


def _union_scans(previous: List[MrmsScan], fresh: List[MrmsScan]) -> List[MrmsScan]:
    """Keep every real key. A short or empty re-list must not erase the window.

    The catch-up re-lists so a scan published mid-run joins the queue.
    Replacing the queue with an empty response would cook one GRIB and
    return, which is the same 10-minute step as a latest-only cooker.
    """
    merged: Dict[datetime, MrmsScan] = {scan.valid_time: scan for scan in previous}
    for scan in fresh:
        merged[scan.valid_time] = scan
    scans = list(merged.values())
    scans.sort(key=lambda scan: scan.valid_time, reverse=True)
    return scans


def _archive_jobs(
    scans: List[MrmsScan],
    ncep: Optional[ReflectivityFrame],
    policy: FrameRetention,
    now: datetime,
) -> List[_ScanJob]:
    jobs: Dict[str, _ScanJob] = {}
    for scan in scans:
        fid = frame_id_for(scan.valid_time)
        jobs[fid] = _ScanJob(valid_time=scan.valid_time, frame_id=fid, key=scan.key)
    if ncep is not None and policy.max_age_seconds is not None:
        when = ncep.valid_time.astimezone(timezone.utc)
        age = (now - when).total_seconds()
        if -600 <= age <= policy.max_age_seconds:
            fid = ncep.frame_id
            jobs[fid] = _ScanJob(valid_time=when, frame_id=fid, loaded=ncep)
    ordered = list(jobs.values())
    ordered.sort(key=lambda job: job.valid_time, reverse=True)
    return ordered


def _cook_rala_archive(cfg: CookerConfig, upload: Optional[bool]) -> Dict:
    """Cook every real RALA scan still missing from the rolling window.

    Order is newest first, so the live alias stays current and the loop fills
    backward with consecutive scans. Scans are not subsampled. A per-run time
    budget only defers the rest to the next timer fire; it does not drop them
    until they age out of the window.
    """
    policy = frame_retention(cfg)
    max_age = timedelta(seconds=policy.max_age_seconds or 0)
    try:
        scans = list_recent_s3_scans(cfg, max_age=max_age)
    except Exception as exc:  # noqa: BLE001 — keep the previous latest-only path
        log.warning("RALA archive listing failed (%s); cooking latest scan only", exc)
        return cook(cfg, source="mrms", upload=upload, archive=False)

    now = _now()
    ncep: Optional[ReflectivityFrame] = None
    newest_age = None
    if scans:
        newest_age = (now - scans[0].valid_time.astimezone(timezone.utc)).total_seconds()
    # Skip the extra CONUS decode when S3 is already within a few minutes.
    if newest_age is None or newest_age > 180:
        ncep = _load_ncep_latest_frame(cfg)
    jobs = _archive_jobs(scans, ncep, policy, now)
    if not jobs:
        log.warning("RALA archive window was empty; cooking latest scan only")
        return cook(cfg, source="mrms", upload=upload, archive=False)

    radar_root = cfg.output_dir / "radar"
    product = cfg.product
    need_upload = _need_upload(cfg, upload)
    palette_version = load_palette(cfg.palette_id).version
    pending = [
        job
        for job in jobs
        if not _frame_is_complete(
            cfg,
            radar_root,
            product,
            job.frame_id,
            need_upload,
            palette_version=palette_version,
        )
    ]
    log.info(
        "RALA archive window max_age_min=%s max_frames=%s listed=%d pending=%d",
        cfg.rala_retention_minutes,
        cfg.rala_retention_frames,
        len(jobs),
        len(pending),
    )
    if not pending:
        newest = jobs[0]
        finished = _now()
        age = (finished - newest.valid_time.astimezone(timezone.utc)).total_seconds()
        result = {
            "frame_id": newest.frame_id,
            "valid_time": _iso(newest.valid_time),
            "source_valid_time": _iso(newest.valid_time),
            "fetched_at": _iso(finished),
            "cook_finished_at": _iso(finished),
            "upload_finished_at": None,
            "source_age_seconds": round(age, 1),
            "cook_seconds": 0.0,
            "upload_seconds": 0.0,
            "lag_seconds": round(age, 1),
            "skipped": "unchanged",
            "source": "NOAA MRMS",
            "product": product.mrms_name,
            "product_id": product.id,
            "region": cfg.region_name,
            "modes": [],
            "manifest": str((radar_root / "manifest.json").resolve()),
            "uploaded": 0,
            "upload_skipped": 0,
            "upload_duration_seconds": 0.0,
            "palette": cfg.palette_id,
            "display_min_dbz": None,
            "archive_cooked": 0,
            "archive_pending": 0,
            "archive_listed": len(jobs),
        }
        _write_status(cfg, product, result)
        log.info(
            "RALA archive already holds %d scan(s); newest=%s",
            len(jobs),
            newest.frame_id,
        )
        return result

    started = time.monotonic()
    budget = max(0.0, float(cfg.rala_catchup_budget_seconds))
    cooked: List[Dict] = []
    attempted: set[str] = set()
    current_scans = scans
    window_jobs = jobs
    passes = 0
    # Re-list each pass so a scan published while this oneshot is still
    # cooking joins the queue. Newest incomplete first. A zero budget still
    # cooks exactly one real frame, then stops. Failures are not retried
    # in the same run, and the list is never subsampled.
    while True:
        if passes > 0 and (time.monotonic() - started) >= budget:
            log.info(
                "RALA catch-up budget %.0fs reached after %d scan(s)",
                budget,
                len(cooked),
            )
            break
        try:
            fresh = list_recent_s3_scans(cfg, max_age=max_age)
        except Exception as exc:  # noqa: BLE001 — keep cooking the window we have
            log.warning(
                "RALA archive re-list failed (%s); using the previous window",
                exc,
            )
            fresh = []
        if fresh:
            current_scans = _union_scans(current_scans, fresh)
        elif current_scans:
            log.info(
                "RALA archive re-list returned no keys; keeping %d scan(s) already listed",
                len(current_scans),
            )
        window_jobs = _archive_jobs(current_scans, ncep, policy, _now())
        pending = [
            job
            for job in window_jobs
            if job.frame_id not in attempted
            and not _frame_is_complete(
                cfg,
                radar_root,
                product,
                job.frame_id,
                need_upload,
                palette_version=palette_version,
            )
        ]
        if not pending:
            break
        job = pending[0]
        attempted.add(job.frame_id)
        passes += 1
        log.info(
            "RALA archive cook %d frame=%s valid_time=%s window=%d",
            passes,
            job.frame_id,
            _iso(job.valid_time),
            len(window_jobs),
        )
        try:
            if job.loaded is not None:
                result = cook(
                    cfg,
                    source="mrms",
                    upload=upload,
                    archive=False,
                    loaded_frame=job.loaded,
                )
            else:
                if not job.key:
                    raise IngestError(f"RALA scan {job.frame_id} has no source object")
                path = download_s3_key(cfg, job.key, cfg.data_dir)
                result = cook(
                    cfg,
                    source="mrms",
                    grib_path=path,
                    upload=upload,
                    archive=False,
                )
        except Exception as exc:  # noqa: BLE001 — one bad GRIB must not drop the hour
            log.error("RALA archive frame %s failed (%s); continuing", job.frame_id, exc)
            continue
        cooked.append(result)

    if not cooked:
        raise IngestError(
            f"RALA archive had pending scan(s) but none cooked "
            f"(attempted={len(attempted)})"
        )
    still_open = [
        job
        for job in window_jobs
        if not _frame_is_complete(
            cfg,
            radar_root,
            product,
            job.frame_id,
            need_upload,
            palette_version=palette_version,
        )
    ]
    summary = max(cooked, key=lambda item: item.get("valid_time") or "")
    summary = dict(summary)
    summary["archive_cooked"] = len(cooked)
    summary["archive_pending"] = len(still_open)
    summary["archive_listed"] = len(window_jobs)
    _write_status(cfg, product, summary)
    log.info(
        "RALA archive pass cooked=%d pending=%d listed=%d newest=%s",
        len(cooked),
        len(still_open),
        len(window_jobs),
        summary.get("frame_id"),
    )
    return summary
