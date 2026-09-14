"""mpwg-radar CLI: cook, smoke, decode."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Optional, Sequence

from mpwg_radar import __version__
from mpwg_radar.config import load_config
from mpwg_radar.cooker import cook
from mpwg_radar.grib import decode_grib2
from mpwg_radar.preview import write_preview
from mpwg_radar.qc import apply_mode
from mpwg_radar.synthetic import synthetic_central_texas


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="mpwg-radar",
        description="MPWG NOAA MRMS radar tile cooker (Central Texas, 512px XYZ → R2)",
    )
    parser.add_argument("--version", action="version", version=f"mpwg-radar {__version__}")
    parser.add_argument("--log-level", default=None, help="DEBUG, INFO, WARNING")
    sub = parser.add_subparsers(dest="cmd", required=True)

    cook_p = sub.add_parser("cook", help="Ingest MRMS, cook tiles, optionally upload to R2")
    cook_p.add_argument("--source", choices=("mrms", "synthetic", "nexrad"), default="mrms")
    cook_p.add_argument("--grib", type=Path, default=None, help="Local .grib2 / .grib2.gz")
    cook_p.add_argument("--modes", default=None, help="Comma list: clean,standard,all")
    cook_p.add_argument("--min-zoom", type=int, default=None)
    cook_p.add_argument("--max-zoom", type=int, default=None)
    cook_p.add_argument("--out", type=Path, default=None)
    cook_p.add_argument("--no-upload", action="store_true")
    cook_p.add_argument("--upload", action="store_true", help="Upload even if MPWG_UPLOAD=false")
    cook_p.add_argument("--keep-empty", action="store_true", help="Write fully transparent tiles")

    smoke_p = sub.add_parser(
        "smoke",
        help="Produce sample Clean-palette 512 tiles (synthetic, or --live NOAA MRMS)",
    )
    smoke_p.add_argument("--live", action="store_true", help="Download latest NOAA MRMS")
    smoke_p.add_argument("--out", type=Path, default=Path("./output/smoke"))
    smoke_p.add_argument("--max-zoom", type=int, default=8)

    dec_p = sub.add_parser("decode", help="Print GRIB2 crop stats (debug)")
    dec_p.add_argument("grib", type=Path)

    args = parser.parse_args(argv)
    _setup_logging(args.log_level)

    if args.cmd == "cook":
        return _cmd_cook(args)
    if args.cmd == "smoke":
        return _cmd_smoke(args)
    if args.cmd == "decode":
        return _cmd_decode(args)
    parser.error("unknown command")
    return 2


def _setup_logging(level: Optional[str]) -> None:
    from mpwg_radar.config import load_config

    resolved = (level or load_config().log_level).upper()
    logging.basicConfig(
        level=getattr(logging, resolved, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%SZ",
    )


def _cmd_cook(args) -> int:
    overrides = {}
    if args.modes:
        overrides["modes"] = [m.strip() for m in args.modes.split(",") if m.strip()]
    if args.min_zoom is not None:
        overrides["min_zoom"] = args.min_zoom
    if args.max_zoom is not None:
        overrides["max_zoom"] = args.max_zoom
    if args.out is not None:
        overrides["output_dir"] = args.out
        overrides["data_dir"] = args.out / "data"
    if args.keep_empty:
        overrides["skip_empty_tiles"] = False
    cfg = load_config(overrides)
    upload = False if args.no_upload else (True if args.upload else None)
    result = cook(cfg, source=args.source, grib_path=args.grib, upload=upload)
    print(json.dumps(result, indent=2))
    return 0


def _cmd_smoke(args) -> int:
    out = args.out
    out.mkdir(parents=True, exist_ok=True)
    overrides = {
        "output_dir": out,
        "data_dir": out / "data",
        "modes": ["clean"],
        "min_zoom": 6,
        "max_zoom": args.max_zoom,
        "skip_empty_tiles": False,
        "keep_dbz": True,
        "upload": False,
    }
    cfg = load_config(overrides)
    source = "mrms" if args.live else "synthetic"
    try:
        result = cook(cfg, source=source, upload=False)
    except Exception as exc:  # noqa: BLE001
        if args.live:
            logging.getLogger(__name__).warning(
                "Live NOAA ingest failed (%s); falling back to synthetic", exc
            )
            result = cook(cfg, source="synthetic", upload=False)
            result["live_error"] = str(exc)
        else:
            raise
    preview = write_preview(out / "radar", cfg)
    result["preview"] = str(preview)
    _assert_smoke_tiles(out / "radar" / "clean")
    print(json.dumps(result, indent=2))
    print(f"\nSmoke tiles: {out / 'radar' / 'clean'}")
    print(f"Preview:     {preview}")
    print("Open preview.html in a browser (or: python -m http.server --directory output/smoke).")
    return 0


def _assert_smoke_tiles(mode_root: Path) -> None:
    pngs = list(mode_root.rglob("*.png"))
    tiles = [p for p in pngs if p.name[0].isdigit() or True]
    tiles = [p for p in pngs if p.parent.parent.parent.name in {"clean", "latest"} or p.suffix == ".png"]
    sized = []
    from PIL import Image

    for path in pngs:
        if path.name == "colorbar.png":
            continue
        # tiles live at {frame}/{z}/{x}/{y}.png — skip json-only dirs
        if not path.name.endswith(".png"):
            continue
        with Image.open(path) as im:
            if im.size == (512, 512) and im.mode == "RGBA":
                sized.append(path)
    if not sized:
        raise SystemExit("smoke test produced no 512×512 RGBA tiles")
    # At least one tile must have a non-zero alpha (echo).
    has_echo = False
    import numpy as np

    for path in sized:
        with Image.open(path) as im:
            arr = np.array(im)
            if arr[..., 3].max() > 0:
                has_echo = True
                break
    if not has_echo:
        raise SystemExit("smoke tiles are 512×512 but fully transparent (no echo)")


def _cmd_decode(args) -> int:
    cfg = load_config()
    frame = decode_grib2(args.grib, bbox=cfg.bbox)
    cleaned = apply_mode(frame, "clean")
    import numpy as np

    payload = {
        "frame_id": frame.frame_id,
        "valid_time": frame.valid_time.isoformat(),
        "shape": list(frame.dbz.shape),
        "bbox": frame.bbox.as_dict(),
        "finite": int(np.isfinite(frame.dbz).sum()),
        "max_dbz": float(np.nanmax(frame.dbz)) if np.isfinite(frame.dbz).any() else None,
        "clean_finite": int(np.isfinite(cleaned.dbz).sum()),
        "clean_max_dbz": float(np.nanmax(cleaned.dbz)) if np.isfinite(cleaned.dbz).any() else None,
    }
    print(json.dumps(payload, indent=2))
    return 0
