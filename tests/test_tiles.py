from __future__ import annotations

from pathlib import Path

import numpy as np
from PIL import Image

from mpwg_radar.geo import CENTRAL_TEXAS, CONUS, count_tiles, latlon_to_global_xy
from mpwg_radar.palette import load_palette
from mpwg_radar.qc import apply_mode
from mpwg_radar.synthetic import synthetic_central_texas
from mpwg_radar.tiles import render_tile, write_tiles


def test_tile_is_512_rgba_with_transparency(tmp_path: Path):
    frame = apply_mode(synthetic_central_texas(), "clean")
    pal = load_palette()
    z = 8
    x, y = latlon_to_global_xy(-97.74, 30.27, z)
    image, has_echo = render_tile(frame, pal, z, int(x), int(y), tile_size=512)
    assert image.size == (512, 512)
    assert image.mode == "RGBA"
    arr = np.array(image)
    assert arr[..., 3].min() == 0  # some clear air
    if has_echo:
        assert arr[..., 3].max() > 0


def test_write_tiles_smoke_layout(tmp_path: Path):
    frame = apply_mode(synthetic_central_texas(), "clean")
    pal = load_palette()
    stats = write_tiles(
        frame,
        pal,
        tmp_path,
        bbox=CENTRAL_TEXAS,
        min_zoom=6,
        max_zoom=6,
        tile_size=512,
        skip_empty=False,
    )
    assert stats["written"] >= 1
    png = next(tmp_path.rglob("*.png"))
    with Image.open(png) as im:
        assert im.size == (512, 512)
        assert im.mode == "RGBA"


def test_write_tiles_skips_empty_conus_windows(tmp_path: Path):
    """Synthetic echo is Central Texas-only; CONUS occupancy skip must not render the rest."""
    frame = apply_mode(synthetic_central_texas(), "clean")
    pal = load_palette()
    stats = write_tiles(
        frame,
        pal,
        tmp_path,
        bbox=CONUS,
        min_zoom=6,
        max_zoom=7,
        tile_size=512,
        skip_empty=True,
    )
    candidates = count_tiles(CONUS, 6, 7)
    assert candidates == 126 + 442
    assert stats["written"] >= 1
    assert stats["skipped_empty"] + stats["written"] == candidates
    assert stats["written"] < 40
