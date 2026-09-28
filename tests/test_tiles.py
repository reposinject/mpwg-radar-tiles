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


def test_native_z9_tile_is_512_not_an_upscale(tmp_path: Path):
    """z9 is another 512px XYZ sample. Clients must not stretch a z8 PNG."""
    frame = apply_mode(synthetic_central_texas(), "clean")
    pal = load_palette()
    stats = write_tiles(
        frame,
        pal,
        tmp_path,
        bbox=CENTRAL_TEXAS,
        min_zoom=9,
        max_zoom=9,
        tile_size=512,
        skip_empty=True,
    )
    assert stats["max_zoom"] == 9
    assert stats["written"] >= 1
    pngs = list(tmp_path.rglob("*.png"))
    assert pngs
    assert all(png.relative_to(tmp_path).parts[0] == "9" for png in pngs)
    with Image.open(pngs[0]) as im:
        assert im.size == (512, 512)
        assert im.mode == "RGBA"


def test_empty_parent_skip_covers_conus_z9_candidates(tmp_path: Path):
    """CONUS z6–9 is 8902 candidates. Echo outside the synthetic storm is not rendered."""
    frame = apply_mode(synthetic_central_texas(), "clean")
    pal = load_palette()
    stats = write_tiles(
        frame,
        pal,
        tmp_path,
        bbox=CONUS,
        min_zoom=6,
        max_zoom=9,
        tile_size=512,
        skip_empty=True,
    )
    candidates = count_tiles(CONUS, 6, 9)
    assert candidates == 8902
    assert stats["skipped_empty"] + stats["written"] == candidates
    # Echo is the Central Texas synthetic storm. That bbox is 80 tiles at
    # z6–9, so the other ~8800 CONUS candidates must be skips.
    assert stats["written"] <= 80
    assert stats["skipped_empty"] > 8000
    assert stats["max_zoom"] == 9
    z9 = [p for p in tmp_path.rglob("*.png") if p.relative_to(tmp_path).parts[0] == "9"]
    assert z9
    with Image.open(z9[0]) as im:
        assert im.size == (512, 512)


def test_parallel_tile_workers_match_serial_pixels(tmp_path: Path):
    frame = apply_mode(synthetic_central_texas(), "clean")
    pal = load_palette()
    serial = tmp_path / "serial"
    parallel = tmp_path / "parallel"
    kwargs = dict(
        bbox=CENTRAL_TEXAS,
        min_zoom=6,
        max_zoom=6,
        tile_size=512,
        skip_empty=True,
    )
    one = write_tiles(frame, pal, serial, workers=1, **kwargs)
    two = write_tiles(frame, pal, parallel, workers=2, **kwargs)
    assert one["written"] == two["written"]
    assert one["written"] >= 1
    serial_pngs = sorted(p.relative_to(serial).as_posix() for p in serial.rglob("*.png"))
    parallel_pngs = sorted(p.relative_to(parallel).as_posix() for p in parallel.rglob("*.png"))
    assert serial_pngs == parallel_pngs
    for rel in serial_pngs:
        with Image.open(serial / rel) as left, Image.open(parallel / rel) as right:
            assert left.size == right.size == (512, 512)
            np.testing.assert_array_equal(np.array(left), np.array(right))
