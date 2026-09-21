"""Cook one MRMS (or synthetic) frame into 512px XYZ tiles + manifests."""

from __future__ import annotations

import json
import logging
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np

from mpwg_radar.config import CookerConfig, load_config
from mpwg_radar.geo import count_tiles, tiles_by_zoom
from mpwg_radar.grib import ReflectivityFrame, decode_grib2
from mpwg_radar.ingest import download_latest_mrms
from mpwg_radar.palette import Palette, load_palette
from mpwg_radar.products import DEFAULT_PRODUCT_ID, PRODUCTS
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
    radar_root = cfg.output_dir / "radar"
    radar_root.mkdir(parents=True, exist_ok=True)

    if cfg.keep_dbz:
        _write_dbz(cfg.output_dir / "dbz" / f"{frame.frame_id}.npz", frame)

    mode_summaries = []
    for mode in cfg.modes:
        cooked = apply_mode(
            frame,
            mode,
            min_dbz=product.min_dbz_override,
            apply_dbz_floor=product.apply_dbz_floor,
            apply_despeckle=product.apply_despeckle,
        )
        summary = _write_mode(cfg, palette, cooked, mode, radar_root, product)
        mode_summaries.append(summary)

    colorbar_rel = product.colorbar_rel()
    write_colorbar(palette, radar_root / colorbar_rel)
    if product.id == DEFAULT_PRODUCT_ID:
        write_colorbar(palette, radar_root / "colorbar.png")
    manifest = _write_manifest(cfg, palette, radar_root, frame, mode_summaries, product)
    stale = _stale_frame_ids(cfg, radar_root, product)
    _prune_old_frames(cfg, radar_root, product)

    should_upload = cfg.upload if upload is None else upload
    upload_stats = UploadStats()
    if should_upload and cfg.r2.enabled:
        publisher = R2Publisher(cfg.r2)
        upload_stats = publisher.upload_frame(
            radar_root,
            frame_id=frame.frame_id,
            modes=cfg.modes,
            product_id=product.id,
        )
        for mode, ids in stale.items():
            for stale_id in ids:
                publisher.delete_prefix(product.tile_url_template(mode, stale_id).split("/{z}")[0])
    elif should_upload and not cfg.r2.enabled:
        log.info("R2 env not set — cooked locally, skipped upload")

    result = {
        "frame_id": frame.frame_id,
        "valid_time": frame.valid_time.astimezone(timezone.utc).isoformat(),
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
    (cfg.output_dir / "status.json").write_text(json.dumps(result, indent=2) + "\n")
    log.info("Cook complete product=%s frame=%s modes=%s", product.id, frame.frame_id, cfg.modes)
    return result


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
        sample_mode=product.sample_mode,
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
) -> Dict:
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
            entry["latest_valid_time"] = frame.valid_time.astimezone(
                timezone.utc
            ).isoformat()
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
