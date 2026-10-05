"""OLD vs compiled STRUCTURE sampler on frame 20261001T190842Z.

Offline only. Does not set MPWG_RALA_DBZ_INTERP and does not upload.
Tile stage excludes PNG encode, matching the 7012.6s profile bucket.

Usage:
  PYTHONPATH=src python3 scripts/bench_structure_sample.py /path/to/frame.grib2.gz
"""

from __future__ import annotations

import sys
import time
from concurrent.futures import ThreadPoolExecutor

import numpy as np

from mpwg_radar.geo import CONUS, iter_tiles
from mpwg_radar.grib import decode_grib2
from mpwg_radar.palette import load_palette
from mpwg_radar.products import CAT_VALID, RALA
from mpwg_radar.qc import apply_mode
from mpwg_radar.structure_recon import (
    _sample_structure_chunked,
    hard_quantize_dbz,
    prepare_structure_field,
    sample_structure_dbz,
)
from mpwg_radar.tiles import (
    _axis_window,
    _grid_fractional,
    _query_lonlat,
    render_tile,
    sample_masked_splat,
    tile_has_echo,
)

GRIB = sys.argv[1] if len(sys.argv) > 1 else (
    "/tmp/mrms/MRMS_ReflectivityAtLowestAltitude_00.50_20261001-190842.grib2.gz"
)
TILE = 512


def jobs_for(frame):
    empty_parents = set()
    jobs = []
    early_parent = early_echo = 0
    for z, x, y in iter_tiles(CONUS, 6, 9):
        parent = (z - 1, x // 2, y // 2)
        if z > 6 and parent in empty_parents:
            early_parent += 1
            continue
        if not tile_has_echo(frame, z, x, y):
            empty_parents.add((z, x, y))
            early_echo += 1
            continue
        jobs.append((z, x, y))
    return jobs, early_parent, early_echo


def echo_cells(frame, item):
    z, x, y = item
    from mpwg_radar.geo import tile_bounds

    b = tile_bounds(z, x, y)
    rows = _axis_window(frame.lat, b.south, b.north)
    cols = _axis_window(frame.lon, b.west, b.east)
    return int(np.count_nonzero(frame.category[rows, cols] == CAT_VALID))


def compare_tile(frame, field, palette, item):
    z, x, y = item
    qlon, qlat = _query_lonlat(z, x, y, TILE)
    jf, iff = _grid_fractional(frame.lat, frame.lon, qlat, qlon)
    t0 = time.perf_counter()
    old_dbz, old_cat = _sample_structure_chunked(field, jf, iff, quantize=False)
    old_s = time.perf_counter() - t0
    t0 = time.perf_counter()
    new_dbz, new_cat = sample_structure_dbz(field, jf, iff, quantize=False)
    new_s = time.perf_counter() - t0
    both = np.isfinite(old_dbz) | np.isfinite(new_dbz)
    max_abs = float(np.max(np.abs(old_dbz[both] - new_dbz[both]))) if np.any(both) else 0.0
    finite_mismatch = int(np.count_nonzero(np.isfinite(old_dbz) != np.isfinite(new_dbz)))
    cat_mismatch = int(np.count_nonzero(old_cat != new_cat))
    q_old = hard_quantize_dbz(old_dbz, 0.5)
    q_new = hard_quantize_dbz(new_dbz, 0.5)
    q_old[old_cat != CAT_VALID] = np.nan
    q_new[new_cat != CAT_VALID] = np.nan
    q_flip = int(np.count_nonzero(~np.isclose(q_old, q_new, atol=1e-9, equal_nan=True)))

    sampled, sampled_cat, edge = sample_masked_splat(
        frame.dbz, frame.lat, frame.lon, qlat, qlon, frame.category
    )

    def paint(held, held_cat):
        use = np.asarray(held, dtype=np.float32).copy()
        use[(sampled_cat != CAT_VALID) | (held_cat != CAT_VALID)] = np.float32(np.nan)
        rgba = palette.colorize(use, category=sampled_cat)
        faded = rgba[..., 3].astype(np.float32) * edge
        rgba = rgba.copy()
        rgba[..., 3] = np.clip(np.rint(faded), 0, 255).astype(np.uint8)
        return rgba

    # Categories for the mask come from the splat plus the sampler.
    rgba_old = paint(q_old, old_cat)
    rgba_new = paint(q_new, new_cat)
    px_diff = int(np.count_nonzero(np.any(rgba_old != rgba_new, axis=-1)))
    old_echo = rgba_old[..., 3] > 0
    new_echo = rgba_new[..., 3] > 0
    noecho_gain = int(np.count_nonzero(new_echo & ~old_echo))
    noecho_loss = int(np.count_nonzero(old_echo & ~new_echo))

    # Once-per-frame native-cell eval vs true fractional query (quantized).
    jn = np.rint(jf)
    inn = np.rint(iff)
    center_dbz, _center_cat = sample_structure_dbz(field, jn, inn, quantize=True)
    center_flip = int(np.count_nonzero(~np.isclose(center_dbz, q_new, atol=1e-9, equal_nan=True)))

    t0 = time.perf_counter()
    render_tile(
        frame, palette, z, x, y, TILE,
        sample_mode="masked-splat", dbz_interp="structure_v1", structure_field=field,
    )
    render_new = time.perf_counter() - t0
    return {
        "item": item,
        "old_sample_s": old_s,
        "new_sample_s": new_s,
        "render_new_s": render_new,
        "prequant_max_abs": max_abs,
        "finite_mismatch": finite_mismatch,
        "cat_mismatch": cat_mismatch,
        "quant_flips": q_flip,
        "rgba_pixels_differ": px_diff,
        "noecho_gain": noecho_gain,
        "noecho_loss": noecho_loss,
        "center_quant_flips": center_flip,
        "echo_pixels": int(np.count_nonzero(new_echo)),
    }


def main():
    t0 = time.perf_counter()
    frame = decode_grib2(GRIB, bbox=CONUS, product=RALA.mrms_name)
    frame = apply_mode(
        frame, "clean", min_dbz=None, apply_dbz_floor=False,
        apply_despeckle=False, edge_aware=False, apply_grid_smooth=False,
    )
    field = prepare_structure_field(frame.dbz, frame.category, "structure_v1")
    print(f"prepare {time.perf_counter()-t0:.2f}s frame={frame.frame_id}", flush=True)
    jobs, early_parent, early_echo = jobs_for(frame)
    print(
        f"jobs={len(jobs)} early_parent={early_parent} early_tile_has_echo={early_echo} "
        f"early_total={early_parent+early_echo}",
        flush=True,
    )
    by_z = {z: [j for j in jobs if j[0] == z] for z in (6, 7, 8, 9)}
    for z, items in by_z.items():
        print(f"  z{z} jobs={len(items)}", flush=True)
    z9 = by_z[9]
    counts = np.array([echo_cells(frame, j) for j in z9])
    order = np.argsort(counts)
    picks = []
    for z, items in by_z.items():
        if not items:
            continue
        if z == 9:
            continue
        scored = sorted(items, key=lambda it: echo_cells(frame, it))
        picks.append(("z%d-p50" % z, scored[len(scored) // 2]))
        picks.append(("z%d-max" % z, scored[-1]))
    for name, q in (("z9-min", 0.0), ("z9-p10", 0.10), ("z9-p50", 0.50), ("z9-p90", 0.90), ("z9-max", 1.0)):
        i = int(round(q * (len(order) - 1)))
        picks.append((name, z9[int(order[i])]))

    palette = load_palette(RALA.palette_id)
    # Warm the compiled kernel on one pixel so tile times exclude compile.
    sample_structure_dbz(field, np.array([1.0]), np.array([1.0]), quantize=False)
    rows = []
    for name, item in picks:
        row = compare_tile(frame, field, palette, item)
        row["name"] = name
        row["cells"] = echo_cells(frame, item)
        rows.append(row)
        print(
            f"{name} {item} cells={row['cells']} echo_px={row['echo_pixels']} "
            f"old={row['old_sample_s']:.3f}s new={row['new_sample_s']:.3f}s "
            f"render_new={row['render_new_s']:.3f}s "
            f"premax={row['prequant_max_abs']:.3e} qflip={row['quant_flips']} "
            f"rgba={row['rgba_pixels_differ']} noecho+={row['noecho_gain']} "
            f"noecho-={row['noecho_loss']} cat={row['cat_mismatch']} "
            f"center_flip={row['center_quant_flips']}",
            flush=True,
        )

    old_mean = float(np.mean([r["old_sample_s"] for r in rows]))
    new_mean = float(np.mean([r["new_sample_s"] for r in rows]))
    render_mean = float(np.mean([r["render_new_s"] for r in rows]))
    print(
        f"STRATIFIED n={len(rows)} old_mean={old_mean:.3f}s new_mean={new_mean:.3f}s "
        f"render_new_mean={render_mean:.3f}s ratio_sample={new_mean/old_mean:.4f}",
        flush=True,
    )
    print(
        f"EXTRAP_TILE_STAGE_FROM_SAMPLE_RATIO {7012.58 * (new_mean / old_mean):.1f}s "
        f"(old profile 7012.58 * new/old sample means)",
        flush=True,
    )

    # Full rendered-job wall, workers=2, NEW path, no PNG.
    def _one(item):
        z, x, y = item
        t = time.perf_counter()
        _image, has_echo = render_tile(
            frame, palette, z, x, y, TILE,
            sample_mode="masked-splat", dbz_interp="structure_v1", structure_field=field,
        )
        return time.perf_counter() - t, has_echo

    t0 = time.perf_counter()
    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="rala-tile") as pool:
        timed = list(pool.map(_one, jobs))
    wall = time.perf_counter() - t0
    cpu = float(sum(t for t, _h in timed))
    late = sum(1 for _t, h in timed if not h)
    print(
        f"FULL_NEW workers=2 jobs={len(jobs)} wall={wall:.2f}s cpu_sum={cpu:.1f}s "
        f"late_no_alpha={late} mean_cpu={cpu/len(jobs):.3f}s",
        flush=True,
    )
    # Scale this host to the cooker using stratified old sample time.
    # Cooker tile stage 7012.58s at workers=2 over the same job count.
    host_old_wall_est = old_mean * len(jobs) / 2.0
    scale = 7012.58 / host_old_wall_est
    print(
        f"HOST_OLD_WALL_EST {host_old_wall_est:.1f}s scale_to_cooker={scale:.3f} "
        f"EXTRAP_FULL_NEW_WALL {wall * scale:.1f}s",
        flush=True,
    )
    worst = max(rows, key=lambda r: r["quant_flips"])
    print(f"WORST_QFLIP {worst['name']} {worst['quant_flips']}", flush=True)


if __name__ == "__main__":
    main()
