"""Cook one MRMS (or synthetic) frame into 512px XYZ tiles + manifests."""

from __future__ import annotations

import fcntl
import json
import logging
import shutil
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterator, List, Optional

import numpy as np

from mpwg_radar.config import CookerConfig, load_config
from mpwg_radar.geo import count_tiles, tiles_by_zoom
from mpwg_radar.grib import ReflectivityFrame, decode_grib2
from mpwg_radar.ingest import download_latest_mrms
from mpwg_radar.palette import Palette, load_palette
from mpwg_radar.products import DEFAULT_PRODUCT_ID, PRODUCTS, ProductSpec
from mpwg_radar.publish import R2Publisher, UploadStats
from mpwg_radar.qc import MODES, apply_mode
from mpwg_radar.synthetic import synthetic_central_texas
from mpwg_radar.tiles import write_colorbar, write_tiles

log = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def cook(
    cfg: Optional[CookerConfig] = None,
    *,
    source: str = "mrms",
    grib_path: Optional[Path] = None,
    upload: Optional[bool] = None,
) -> Dict:
    cfg = cfg or load_config()
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

    frame = _load_frame(cfg, source=source, grib_path=grib_path)
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
    for mode in cfg.modes:
        cooked = apply_mode(
            frame,
            mode,
            min_dbz=product.min_dbz_override,
            apply_dbz_floor=product.apply_dbz_floor,
        )
        summary = _write_mode(cfg, palette, cooked, mode, radar_root, product)
        mode_summaries.append(summary)

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
        )
        # Stamp the manifest just before its PUT. The latency line below
        # uses the clock after that PUT returns.
        manifest_upload_started = _now()
        manifest_stats = UploadStats()

        def _put_manifest() -> None:
            nonlocal manifest_stats
            manifest_stats = publisher.upload_manifest(radar_root)
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
        )
        upload_finished_at = _now()
        _add_upload_stats(upload_stats, manifest_stats)
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


def _write_mode(
    cfg: CookerConfig,
    palette: Palette,
    frame: ReflectivityFrame,
    mode: str,
    radar_root: Path,
    product: ProductSpec,
) -> Dict:
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
            "despeckle": MODES[mode].despeckle,
            "smooth": MODES[mode].smooth,
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
    if latest_dir.exists():
        shutil.rmtree(latest_dir)
    shutil.copytree(frame_dir, latest_dir)
    latest_meta = dict(meta)
    latest_meta["tile_url_template"] = product.tile_url_template(mode, "latest")
    latest_meta["alias"] = "latest"
    (latest_dir / "frame.json").write_text(json.dumps(latest_meta, indent=2) + "\n")
    return meta


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

    this_modes = {}
    for summary in mode_summaries:
        mode = summary["mode"]
        frames = _list_frames(product.mode_dir(radar_root, mode), product, mode)
        this_modes[mode] = {
            "latest": product.tile_url_template(mode, "latest"),
            "latest_frame": summary["id"],
            "frames": frames[: cfg.retention_frames],
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
            entry["latest_frame"] = frame.frame_id
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
    keep = max(1, cfg.retention_frames)
    stale: Dict[str, List[str]] = {}
    for mode in cfg.modes:
        frames = _list_frames(product.mode_dir(radar_root, mode), product, mode)
        stale[mode] = [item["id"] for item in frames[keep:]]
    return stale


def _prune_old_frames(
    cfg: CookerConfig, radar_root: Path, product: ProductSpec
) -> None:
    keep = max(1, cfg.retention_frames)
    for mode in cfg.modes:
        mode_dir = product.mode_dir(radar_root, mode)
        if not mode_dir.is_dir():
            continue
        named = sorted(
            [
                p
                for p in mode_dir.iterdir()
                if p.is_dir() and p.name != "latest" and (p / "frame.json").is_file()
            ],
            key=lambda p: p.name,
            reverse=True,
        )
        for extra in named[keep:]:
            log.info("Pruning local frame %s", extra)
            shutil.rmtree(extra, ignore_errors=True)
