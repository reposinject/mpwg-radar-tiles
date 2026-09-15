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
from mpwg_radar.publish import R2Publisher
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
    palette = load_palette(cfg.palette_id)
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    cfg.output_dir.mkdir(parents=True, exist_ok=True)
    log.info(
        "Cook start region=%s bbox=%s z%s-%s candidate_tiles=%d by_zoom=%s "
        "tile_size=%d palette=%s modes=%s",
        cfg.region_name,
        cfg.bbox.as_dict(),
        cfg.min_zoom,
        cfg.max_zoom,
        count_tiles(cfg.bbox, cfg.min_zoom, cfg.max_zoom),
        tiles_by_zoom(cfg.bbox, cfg.min_zoom, cfg.max_zoom),
        cfg.tile_size,
        palette.id,
        cfg.modes,
    )

    frame = _load_frame(cfg, source=source, grib_path=grib_path)
    radar_root = cfg.output_dir / "radar"
    radar_root.mkdir(parents=True, exist_ok=True)

    if cfg.keep_dbz:
        _write_dbz(cfg.output_dir / "dbz" / f"{frame.frame_id}.npz", frame)

    mode_summaries = []
    for mode in cfg.modes:
        cooked = apply_mode(frame, mode)
        summary = _write_mode(cfg, palette, cooked, mode, radar_root)
        mode_summaries.append(summary)

    write_colorbar(palette, radar_root / "colorbar.png")
    manifest = _write_manifest(cfg, palette, radar_root, frame, mode_summaries)
    stale = _stale_frame_ids(cfg, radar_root)
    _prune_old_frames(cfg, radar_root)

    should_upload = cfg.upload if upload is None else upload
    uploaded: List[str] = []
    if should_upload and cfg.r2.enabled:
        publisher = R2Publisher(
            cfg.r2,
            workers=cfg.upload_workers,
            timeout_seconds=cfg.upload_timeout_seconds,
        )
        uploaded = publisher.upload_cook(
            radar_root,
            frame_id=frame.frame_id,
            modes=cfg.modes,
            all_frames=cfg.upload_all_frames,
        )
        for mode, ids in stale.items():
            for stale_id in ids:
                publisher.delete_prefix(f"{mode}/{stale_id}")
    elif should_upload and not cfg.r2.enabled:
        log.info("R2 env not set — cooked locally, skipped upload")

    result = {
        "frame_id": frame.frame_id,
        "valid_time": frame.valid_time.astimezone(timezone.utc).isoformat(),
        "source": frame.source,
        "product": frame.product,
        "region": cfg.region_name,
        "modes": mode_summaries,
        "manifest": str((radar_root / "manifest.json").resolve()),
        "uploaded": len(uploaded),
        "palette": palette.id,
    }
    (cfg.output_dir / "status.json").write_text(json.dumps(result, indent=2) + "\n")
    log.info("Cook complete frame=%s modes=%s", frame.frame_id, cfg.modes)
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
    return decode_grib2(path, bbox=cfg.bbox)


def _write_dbz(path: Path, frame: ReflectivityFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(
        path,
        dbz=frame.dbz,
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
) -> Dict:
    frame_rel = f"{mode}/{frame.frame_id}"
    frame_dir = radar_root / frame_rel
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
        "source": frame.source,
        "valid_time": frame.valid_time.astimezone(timezone.utc).isoformat(),
        "mode": mode,
        "mode_spec": {
            "min_dbz": MODES[mode].min_dbz,
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
        "tile_url_template": f"{mode}/{frame.frame_id}/{{z}}/{{x}}/{{y}}.png",
        "stats": stats,
        "cooked_at": _now().isoformat(),
        "attribution": "NOAA MRMS MergedReflectivityQCComposite",
    }
    (frame_dir / "frame.json").write_text(json.dumps(meta, indent=2) + "\n")

    latest_dir = radar_root / mode / "latest"
    if latest_dir.exists():
        shutil.rmtree(latest_dir)
    shutil.copytree(frame_dir, latest_dir)
    latest_meta = dict(meta)
    latest_meta["tile_url_template"] = f"{mode}/latest/{{z}}/{{x}}/{{y}}.png"
    latest_meta["alias"] = "latest"
    (latest_dir / "frame.json").write_text(json.dumps(latest_meta, indent=2) + "\n")
    return meta


def _write_manifest(
    cfg: CookerConfig,
    palette: Palette,
    radar_root: Path,
    frame: ReflectivityFrame,
    mode_summaries: List[Dict],
) -> Dict:
    modes = {}
    for summary in mode_summaries:
        mode = summary["mode"]
        frames = _list_frames(radar_root / mode)
        modes[mode] = {
            "latest": f"{mode}/latest/{{z}}/{{x}}/{{y}}.png",
            "latest_frame": summary["id"],
            "frames": frames[: cfg.retention_frames],
        }
    manifest = {
        "product": "mpwg-radar",
        "version": "1.0.0",
        "region": cfg.region_name,
        "bbox": cfg.bbox.as_dict(),
        "tile_size": cfg.tile_size,
        "min_zoom": cfg.min_zoom,
        "max_zoom": cfg.max_zoom,
        "scheme": "xyz",
        "crs": "EPSG:3857",
        "palette": palette.as_dict(),
        "default_mode": cfg.modes[0],
        "modes": modes,
        "updated_at": _now().isoformat(),
        "latest_valid_time": frame.valid_time.astimezone(timezone.utc).isoformat(),
        "attribution": "Radar: NOAA MRMS. Palette: MPWG Clean (James Sep 2026).",
    }
    path = radar_root / "manifest.json"
    path.write_text(json.dumps(manifest, indent=2) + "\n")
    log.info("Wrote %s", path)
    return manifest


def _list_frames(mode_dir: Path) -> List[Dict]:
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
        frames.append(
            {
                "id": meta.get("id", child.name),
                "valid_time": meta.get("valid_time"),
                "tiles": f"{mode_dir.name}/{child.name}/{{z}}/{{x}}/{{y}}.png",
                "frame": f"{mode_dir.name}/{child.name}/frame.json",
            }
        )
    return frames


def _stale_frame_ids(cfg: CookerConfig, radar_root: Path) -> Dict[str, List[str]]:
    keep = max(1, cfg.retention_frames)
    stale: Dict[str, List[str]] = {}
    for mode in cfg.modes:
        frames = _list_frames(radar_root / mode)
        stale[mode] = [item["id"] for item in frames[keep:]]
    return stale


def _prune_old_frames(cfg: CookerConfig, radar_root: Path) -> None:
    keep = max(1, cfg.retention_frames)
    for mode in cfg.modes:
        mode_dir = radar_root / mode
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
