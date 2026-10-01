"""FIX2 and STRUCTURE V1 dBZ reconstruction for the gated RALA review cook.

Offline acceptance (James, 2026-10-01) is a dense neighborhood hybrid:

* Gaussian local-plane trend, σ = 0.55 cells, ``soft_mode=no_special``
  (numerical floor 1e-12 only).
* Residual IDW with CLUSTER bridges, ``continuous_add``, and
  ``d2_floor = (1/spc)²``. The live tile path locks ``spc`` to the core
  acceptance value 32, so the floor stays ``(1/32)²``. It does not build a
  CONUS-wide dense lattice.
* STRUCTURE V1 keeps that trend and scales residual amplitude: multi-member
  clusters ×1.55, singletons ×0.25 (``min_cluster_size_for_boost=2``).
  FIX2 leaves both scales at 1.
* Clamp to the contributing 5×5 min/max. NO-ECHO is hard: a query whose
  nearest native cell is not valid echo is not a sample.
* Display values are hard-quantized to 0.5 dBZ (half up) before the p3k LUT.

The per-pixel evaluation is the same function the offline dense grid samples.
Tiles call it at each pixel's native fractional index instead of materializing
that grid. Do not retune these locks.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

from mpwg_radar.products import CAT_MISSING, CAT_NO_ECHO, CAT_VALID

# Locked FIX2 / STRUCTURE V1. Do not retune.
GAUSS_SIGMA_CELLS = 0.55
TREND_SOFT_MODE = "no_special"
TREND_SOFT_FLOOR = 1e-12
RESIDUAL_SOFT_MODE = "continuous_add"
QUANT_STEP_DBZ = 0.5
CLUSTER_AMP_BOOST = 1.55
SINGLETON_AMP_SCALE = 0.25
MIN_CLUSTER_SIZE_FOR_BOOST = 2
# Core acceptance oversample. Residual floor is (1/spc)^2.
CORE_SAMPLES_PER_CELL = 32
HALF_WIDTH = 2
IDW_POWER = 2.0
KNOT_EPS = 1e-12
MIN_TAPS_FOR_PLANE = 3
SHEPARD_RADIUS_CELLS = 2.5
SUPPORT_RADIUS_CELLS = 2.0
AMP_FRAC = 0.50
NOISE_FLOOR_DBZ = 0.25
MIN_COHERENT_NEIGHBORS = 2
S_GATE = 0.40
S_POWER = 1.75
ELONGATION_ASPECT_MIN = 1.55
BRIDGE_8_AMP_FRAC = 0.50
BRIDGE_THIRDS_MIN_LENGTH = 1.2

KIND_FIX2 = "fix2"
KIND_STRUCTURE = "structure_v1"

# Query-eval search pad. Sources are stored once at their rounded cell.
# hw+0.75 reach plus rounding needs a wider index window than the offline
# multi-bucket lookup (which dedups). Distance gate stays hw+0.75.
_SOURCE_PAD = HALF_WIDTH + 2


def residual_d2_floor(samples_per_cell: int) -> float:
    """Tiny continuous-add floor. Scales with the locked oversample."""
    spc = int(samples_per_cell)
    if spc < 1:
        raise ValueError("samples_per_cell must be >= 1")
    return (1.0 / float(spc)) ** 2


def hard_quantize_dbz(values: np.ndarray, step: float = QUANT_STEP_DBZ) -> np.ndarray:
    """Half-up quantize to ``step`` dBZ. Non-finite samples stay NaN.

    Same half-up rule as the p3k LUT index (``floor(x/step + 0.5)``).
    """
    arr = np.asarray(values, dtype=np.float64)
    out = np.full(arr.shape, np.nan, dtype=np.float64)
    ok = np.isfinite(arr)
    if np.any(ok):
        out[ok] = np.floor(arr[ok] / float(step) + 0.5) * float(step)
    return out


def _corner_ok(cat: np.ndarray, dbz: np.ndarray, j: int, i: int) -> bool:
    ny, nx = cat.shape
    if j < 0 or i < 0 or j >= ny or i >= nx:
        return False
    return int(cat[j, i]) == CAT_VALID and bool(np.isfinite(dbz[j, i]))


def _apply_soft_d2(d2: float, soft_floor: float, soft_mode: str) -> float:
    if soft_mode == "continuous_max":
        return max(d2, soft_floor)
    if soft_mode == "continuous_add":
        return d2 + soft_floor
    if soft_mode == "discontinuous":
        return soft_floor if d2 < KNOT_EPS else d2
    if soft_mode == "no_special":
        return d2 + soft_floor
    raise ValueError(f"unknown soft_floor_mode={soft_mode}")


def make_locked_params(kind: str, samples_per_cell: int = CORE_SAMPLES_PER_CELL) -> Tuple[dict, dict]:
    """Return ``(trend_params, hybrid_params)`` for FIX2 or STRUCTURE V1."""
    token = str(kind).strip().lower().replace("-", "_")
    if token not in (KIND_FIX2, KIND_STRUCTURE):
        raise ValueError(
            f"Unknown structure kind {kind!r}. Use {KIND_STRUCTURE!r} or {KIND_FIX2!r}."
        )
    trend = {
        "name": "neighborhood-sample2d-5x5-plane-gauss_sig0p55_nospecial",
        "half_width": HALF_WIDTH,
        "min_taps_for_plane": MIN_TAPS_FOR_PLANE,
        "weight": "gaussian_sigma",
        "gaussian_sigma_cells": GAUSS_SIGMA_CELLS,
        "idw_power": IDW_POWER,
        "shepard_radius_cells": SHEPARD_RADIUS_CELLS,
        "knot_policy": "soft",
        "soft_floor_mode": TREND_SOFT_MODE,
        "soft_d2_floor": TREND_SOFT_FLOOR,
        "clamp": "contributing_minmax",
    }
    boost = CLUSTER_AMP_BOOST if token == KIND_STRUCTURE else 1.0
    singleton = SINGLETON_AMP_SCALE if token == KIND_STRUCTURE else 1.0
    hybrid = {
        "name": (
            "structure-v1-cluster-amp-boost-1p55-singleton-0p25"
            if token == KIND_STRUCTURE
            else "neighborhood-hybrid-cluster-coherence-q0p5-residual-add-tiny"
        ),
        "half_width": HALF_WIDTH,
        "support_radius_cells": SUPPORT_RADIUS_CELLS,
        "amp_frac": AMP_FRAC,
        "noise_floor_dbz": NOISE_FLOOR_DBZ,
        "min_coherent_neighbors": MIN_COHERENT_NEIGHBORS,
        "s_gate": S_GATE,
        "s_power": S_POWER,
        "elongation_check": True,
        "elongation_aspect_min": ELONGATION_ASPECT_MIN,
        "residual_idw_power": IDW_POWER,
        "residual_d2_floor": residual_d2_floor(samples_per_cell),
        "residual_soft_mode": RESIDUAL_SOFT_MODE,
        "cluster_amp_boost": boost,
        "singleton_amp_scale": singleton,
        "min_cluster_size_for_boost": MIN_CLUSTER_SIZE_FOR_BOOST,
        "bridge_8_amp_frac": BRIDGE_8_AMP_FRAC,
        "bridge_s_mode": "min",
        "bridge_thirds_min_length": BRIDGE_THIRDS_MIN_LENGTH,
    }
    return trend, hybrid


def _map_support_gain(s_frac: float, s_gate: float, s_power: float) -> float:
    s = float(s_frac)
    if s <= 0.0:
        return 0.0
    g = float(s_gate)
    if g <= 0.0:
        return s ** float(s_power)
    if s < g:
        t = s / g
        return float((t * t) * g * (s ** (float(s_power) - 1.0)))
    if g >= 1.0:
        return float(g)
    u = (s - g) / (1.0 - g)
    return float(g + (1.0 - g) * (u ** float(s_power)))


def _map_support_gain_vec(s: np.ndarray, s_gate: float, s_power: float) -> np.ndarray:
    src = np.asarray(s, dtype=np.float64)
    out = np.zeros(src.shape, dtype=np.float64)
    g = float(s_gate)
    p = float(s_power)
    pos = src > 0.0
    if g <= 0.0:
        out[pos] = src[pos] ** p
        return out
    below = pos & (src < g)
    if np.any(below):
        t = src[below] / g
        out[below] = (t * t) * g * (src[below] ** (p - 1.0))
    if g >= 1.0:
        return out
    above = pos & (src >= g)
    if np.any(above):
        u = (src[above] - g) / (1.0 - g)
        out[above] = g + (1.0 - g) * (u ** p)
    return out


def _elongation_aspect(offsets: Sequence[Tuple[int, int]]) -> float:
    if len(offsets) < 2:
        return 1.0
    xs = np.array([float(d[1]) for d in offsets], dtype=np.float64)
    ys = np.array([float(d[0]) for d in offsets], dtype=np.float64)
    xs = np.concatenate([xs, [0.0]])
    ys = np.concatenate([ys, [0.0]])
    x0 = xs - xs.mean()
    y0 = ys - ys.mean()
    cxx = float(np.mean(x0 * x0))
    cyy = float(np.mean(y0 * y0))
    cxy = float(np.mean(x0 * y0))
    tr = cxx + cyy
    det = cxx * cyy - cxy * cxy
    disc = max(0.0, tr * tr - 4.0 * det)
    root = math.sqrt(disc)
    l1 = 0.5 * (tr + root)
    l2 = 0.5 * (tr - root)
    if l1 <= 1e-18:
        return 1.0
    return float(math.sqrt(max(l1, 0.0) / max(l2, 1e-18)))


def _trend_scalar(
    rf: float,
    cf: float,
    dbz: np.ndarray,
    cat: np.ndarray,
    params: dict,
) -> Tuple[float, int, float, float]:
    """One-query FIX2 Gaussian plane. Returns ``(value, category, lo, hi)``."""
    hw = int(params.get("half_width", HALF_WIDTH))
    min_taps = int(params.get("min_taps_for_plane", MIN_TAPS_FOR_PLANE))
    power = float(params.get("idw_power", IDW_POWER))
    radius = float(params.get("shepard_radius_cells", SHEPARD_RADIUS_CELLS))
    soft_floor = float(params.get("soft_d2_floor", TREND_SOFT_FLOOR))
    soft_mode = str(params.get("soft_floor_mode", TREND_SOFT_MODE))
    ny, nx = dbz.shape
    jn = int(np.rint(rf))
    inn = int(np.rint(cf))
    jn = min(max(jn, 0), ny - 1)
    inn = min(max(inn, 0), nx - 1)

    if int(cat[jn, inn]) != CAT_VALID or not np.isfinite(dbz[jn, inn]):
        if int(cat[jn, inn]) == CAT_MISSING:
            return float("nan"), CAT_MISSING, float("nan"), float("nan")
        return float("nan"), CAT_NO_ECHO, float("nan"), float("nan")

    taps_x: List[float] = []
    taps_y: List[float] = []
    taps_z: List[float] = []
    taps_d2: List[float] = []
    for dj in range(-hw, hw + 1):
        for di in range(-hw, hw + 1):
            jr = jn + dj
            ir = inn + di
            if not _corner_ok(cat, dbz, jr, ir):
                continue
            dx = float(ir) - cf
            dy = float(jr) - rf
            d2 = _apply_soft_d2(dx * dx + dy * dy, soft_floor, soft_mode)
            taps_x.append(float(ir))
            taps_y.append(float(jr))
            taps_z.append(float(dbz[jr, ir]))
            taps_d2.append(d2)

    if not taps_z:
        v = float(dbz[jn, inn])
        return v, CAT_VALID, v, v
    lo = min(taps_z)
    hi = max(taps_z)
    n = len(taps_z)
    if n == 1:
        return taps_z[0], CAT_VALID, lo, hi

    z = np.asarray(taps_z, dtype=np.float64)
    x = np.asarray(taps_x, dtype=np.float64)
    y = np.asarray(taps_y, dtype=np.float64)
    sig = float(params.get("gaussian_sigma_cells", GAUSS_SIGMA_CELLS))
    sig = max(sig, 1e-6)
    if str(params.get("weight", "gaussian_sigma")) == "gaussian_sigma":
        w = np.exp(-0.5 * np.asarray(taps_d2, dtype=np.float64) / (sig * sig))
        w = np.maximum(w, 1e-15)
    else:
        w = 1.0 / (np.asarray(taps_d2, dtype=np.float64) ** (power / 2.0))

    if n >= min_taps:
        xr = x - cf
        yr = y - rf
        design = np.column_stack([np.ones(n), xr, yr])
        weighted = design * w[:, None]
        normal = design.T @ weighted
        rhs = design.T @ (w * z)
        try:
            if abs(np.linalg.det(normal)) > 1e-14:
                beta = np.linalg.solve(normal, rhs)
                val = float(beta[0])
            else:
                val = _shepard(taps_d2, w, z, radius)
        except np.linalg.LinAlgError:
            val = _shepard(taps_d2, w, z, radius)
    else:
        val = _shepard(taps_d2, w, z, radius)

    if val < lo:
        val = lo
    elif val > hi:
        val = hi
    return val, CAT_VALID, lo, hi


def _shepard(d2: Sequence[float], w: np.ndarray, z: np.ndarray, radius: float) -> float:
    r2 = float(radius) * float(radius)
    mask = np.asarray(d2, dtype=np.float64) <= r2 + 1e-15
    if not np.any(mask):
        mask = np.ones(len(z), dtype=bool)
    ww = w[mask]
    zz = z[mask]
    return float(np.sum(ww * zz) / ww.sum())


def _support_offsets(hw: int, radius: float) -> List[Tuple[int, int]]:
    r2 = float(radius) * float(radius)
    offsets = []
    for dj in range(-hw, hw + 1):
        for di in range(-hw, hw + 1):
            if dj == 0 and di == 0:
                continue
            if dj * dj + di * di <= r2 + 1e-15:
                offsets.append((dj, di))
    return offsets


def _support_field(
    dbz: np.ndarray,
    cat: np.ndarray,
    trend: np.ndarray,
    hybrid: dict,
) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Native T, residual R = v − T, and refined support S. Vectorized gates."""
    ny, nx = dbz.shape
    valid = (cat == CAT_VALID) & np.isfinite(dbz) & np.isfinite(trend)
    T = np.full((ny, nx), np.nan, dtype=np.float64)
    R = np.full((ny, nx), np.nan, dtype=np.float64)
    T[valid] = trend[valid]
    # Nearest-valid cells whose plane failed still hold native (residual 0).
    hold = (cat == CAT_VALID) & np.isfinite(dbz) & ~np.isfinite(trend)
    T[hold] = dbz[hold]
    R[valid] = dbz[valid] - T[valid]
    R[hold] = 0.0

    hw = int(hybrid.get("half_width", HALF_WIDTH))
    rad = float(hybrid.get("support_radius_cells", SUPPORT_RADIUS_CELLS))
    amp_frac = float(hybrid.get("amp_frac", AMP_FRAC))
    noise = float(hybrid.get("noise_floor_dbz", NOISE_FLOOR_DBZ))
    min_coh = int(hybrid.get("min_coherent_neighbors", MIN_COHERENT_NEIGHBORS))
    s_gate = float(hybrid.get("s_gate", S_GATE))
    s_power = float(hybrid.get("s_power", S_POWER))
    elong_on = bool(hybrid.get("elongation_check", True))
    elong_min = float(hybrid.get("elongation_aspect_min", ELONGATION_ASPECT_MIN))
    offsets = _support_offsets(hw, rad)

    agree = np.zeros((ny, nx), dtype=np.int16)
    total = np.zeros((ny, nx), dtype=np.int16)
    # Moments of agreeing neighbor offsets (origin added later).
    sum_x = np.zeros((ny, nx), dtype=np.float64)
    sum_y = np.zeros((ny, nx), dtype=np.float64)
    sum_xx = np.zeros((ny, nx), dtype=np.float64)
    sum_yy = np.zeros((ny, nx), dtype=np.float64)
    sum_xy = np.zeros((ny, nx), dtype=np.float64)
    finite_r = np.isfinite(R)

    for dj, di in offsets:
        if dj >= 0:
            r_src = R[dj:, :]
            f_src = finite_r[dj:, :]
            r_dst_sl = (slice(0, ny - dj), slice(None))
        else:
            r_src = R[:dj, :]
            f_src = finite_r[:dj, :]
            r_dst_sl = (slice(-dj, None), slice(None))
        if di >= 0:
            r_src = r_src[:, di:]
            f_src = f_src[:, di:]
            c_src_sl = (r_dst_sl[0], slice(0, nx - di))
            c_nbr_sl = (r_dst_sl[0], slice(di, None))
        else:
            r_src = r_src[:, :di]
            f_src = f_src[:, :di]
            c_src_sl = (r_dst_sl[0], slice(-di, None))
            c_nbr_sl = (r_dst_sl[0], slice(0, nx + di))
        # Re-slice source rows to the column window.
        if di >= 0:
            r_nbr = R[dj:, di:] if dj >= 0 else R[:dj, di:]
            f_nbr = finite_r[dj:, di:] if dj >= 0 else finite_r[:dj, di:]
        else:
            r_nbr = R[dj:, :di] if dj >= 0 else R[:dj, :di]
            f_nbr = finite_r[dj:, :di] if dj >= 0 else finite_r[:dj, :di]
        r0 = R[c_src_sl]
        both = finite_r[c_src_sl] & f_nbr & (cat[c_src_sl] == CAT_VALID)
        # cat on the neighbor
        if dj >= 0 and di >= 0:
            cat_n = cat[dj:, di:]
        elif dj >= 0 and di < 0:
            cat_n = cat[dj:, :di]
        elif dj < 0 and di >= 0:
            cat_n = cat[:dj, di:]
        else:
            cat_n = cat[:dj, :di]
        both = both & (cat_n == CAT_VALID)
        total[c_src_sl] += both.astype(np.int16)
        rn = r_nbr
        noise_agree = both & (np.abs(r0) < noise) & (np.abs(rn) < noise)
        same = ((r0 >= 0) & (rn >= 0)) | ((r0 < 0) & (rn < 0))
        amp_ok = np.abs(rn) >= amp_frac * np.maximum(np.abs(r0), noise)
        coherent = noise_agree | (both & same & amp_ok & ~noise_agree)
        agree[c_src_sl] += coherent.astype(np.int16)
        if elong_on:
            sum_x[c_src_sl] += np.where(coherent, float(di), 0.0)
            sum_y[c_src_sl] += np.where(coherent, float(dj), 0.0)
            sum_xx[c_src_sl] += np.where(coherent, float(di * di), 0.0)
            sum_yy[c_src_sl] += np.where(coherent, float(dj * dj), 0.0)
            sum_xy[c_src_sl] += np.where(coherent, float(di * dj), 0.0)

    S = np.zeros((ny, nx), dtype=np.float64)
    has_nbr = (total > 0) & finite_r
    enough = has_nbr & (agree >= min_coh)
    s_frac = np.zeros_like(S)
    s_frac[enough] = agree[enough] / total[enough]
    mapped = _map_support_gain_vec(s_frac, s_gate, s_power)
    if elong_on:
        n_pts = agree.astype(np.float64) + 1.0  # coherent offsets + origin
        use = enough & (agree >= 2)
        mean_x = np.zeros_like(S)
        mean_y = np.zeros_like(S)
        mean_x[use] = sum_x[use] / n_pts[use]
        mean_y[use] = sum_y[use] / n_pts[use]
        cxx = np.zeros_like(S)
        cyy = np.zeros_like(S)
        cxy = np.zeros_like(S)
        cxx[use] = sum_xx[use] / n_pts[use] - mean_x[use] ** 2
        cyy[use] = sum_yy[use] / n_pts[use] - mean_y[use] ** 2
        cxy[use] = sum_xy[use] / n_pts[use] - mean_x[use] * mean_y[use]
        tr = cxx + cyy
        det = cxx * cyy - cxy * cxy
        disc = np.maximum(0.0, tr * tr - 4.0 * det)
        root = np.sqrt(disc)
        l1 = 0.5 * (tr + root)
        l2 = 0.5 * (tr - root)
        aspect = np.ones_like(S)
        good = use & (l1 > 1e-18)
        aspect[good] = np.sqrt(np.maximum(l1[good], 0.0) / np.maximum(l2[good], 1e-18))
        lift = use & (aspect >= elong_min)
        mapped = np.where(lift, np.maximum(mapped, s_frac), mapped)
    S[enough] = np.clip(mapped[enough], 0.0, 1.0)
    return T, R, S


def _support_field_loops(
    dbz: np.ndarray,
    cat: np.ndarray,
    trend_at_valid: np.ndarray,
    hybrid: dict,
) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Loop port of refined support, for the vectorized-gate check."""
    ny, nx = dbz.shape
    T = np.full((ny, nx), np.nan, dtype=np.float64)
    R = np.full((ny, nx), np.nan, dtype=np.float64)
    S = np.zeros((ny, nx), dtype=np.float64)
    hw = int(hybrid.get("half_width", HALF_WIDTH))
    rad = float(hybrid.get("support_radius_cells", SUPPORT_RADIUS_CELLS))
    amp_frac = float(hybrid.get("amp_frac", AMP_FRAC))
    noise = float(hybrid.get("noise_floor_dbz", NOISE_FLOOR_DBZ))
    min_coh = int(hybrid.get("min_coherent_neighbors", MIN_COHERENT_NEIGHBORS))
    s_gate = float(hybrid.get("s_gate", S_GATE))
    s_power = float(hybrid.get("s_power", S_POWER))
    elong_on = bool(hybrid.get("elongation_check", True))
    elong_min = float(hybrid.get("elongation_aspect_min", ELONGATION_ASPECT_MIN))
    r2_lim = rad * rad
    for j in range(ny):
        for i in range(nx):
            if int(cat[j, i]) != CAT_VALID or not np.isfinite(dbz[j, i]):
                continue
            t_val = float(trend_at_valid[j, i]) if np.isfinite(trend_at_valid[j, i]) else float(dbz[j, i])
            T[j, i] = t_val
            R[j, i] = float(dbz[j, i]) - t_val
    for j in range(ny):
        for i in range(nx):
            if not np.isfinite(R[j, i]):
                continue
            r0 = float(R[j, i])
            agree = 0
            total = 0
            coherent_offsets: List[Tuple[int, int]] = []
            for dj in range(-hw, hw + 1):
                for di in range(-hw, hw + 1):
                    if dj == 0 and di == 0:
                        continue
                    if dj * dj + di * di > r2_lim + 1e-15:
                        continue
                    jr, ir = j + dj, i + di
                    if not (0 <= jr < ny and 0 <= ir < nx):
                        continue
                    if int(cat[jr, ir]) != CAT_VALID or not np.isfinite(R[jr, ir]):
                        continue
                    total += 1
                    rn = float(R[jr, ir])
                    if abs(r0) < noise and abs(rn) < noise:
                        agree += 1
                        coherent_offsets.append((dj, di))
                        continue
                    same_sign = (r0 >= 0 and rn >= 0) or (r0 < 0 and rn < 0)
                    if not same_sign:
                        continue
                    if abs(rn) >= amp_frac * max(abs(r0), noise):
                        agree += 1
                        coherent_offsets.append((dj, di))
            if total == 0 or agree < min_coh:
                S[j, i] = 0.0
                continue
            s_frac = float(agree) / float(total)
            s_mapped = _map_support_gain(s_frac, s_gate, s_power)
            if elong_on and agree >= min_coh:
                aspect = _elongation_aspect(coherent_offsets)
                if aspect >= elong_min:
                    s_mapped = max(s_mapped, s_frac)
            S[j, i] = float(min(1.0, max(0.0, s_mapped)))
    return T, R, S


def _residual_sign(value: float) -> int:
    return 1 if float(value) >= 0.0 else -1


def _label_residual_clusters(
    residual: np.ndarray,
    support: np.ndarray,
    cat: np.ndarray,
) -> Tuple[np.ndarray, List[List[Tuple[int, int]]]]:
    ny, nx = residual.shape
    labels = np.full((ny, nx), -1, dtype=np.int32)
    clusters: List[List[Tuple[int, int]]] = []
    neigh8 = (
        (-1, -1), (-1, 0), (-1, 1),
        (0, -1), (0, 1),
        (1, -1), (1, 0), (1, 1),
    )

    def is_member(j: int, i: int) -> bool:
        return (
            int(cat[j, i]) == CAT_VALID
            and np.isfinite(residual[j, i])
            and float(support[j, i]) > 0.0
        )

    for j in range(ny):
        for i in range(nx):
            if not is_member(j, i) or labels[j, i] >= 0:
                continue
            sign0 = _residual_sign(float(residual[j, i]))
            stack = [(j, i)]
            labels[j, i] = len(clusters)
            members = [(j, i)]
            while stack:
                cj, ci = stack.pop()
                for dj, di in neigh8:
                    nj, ni = cj + dj, ci + di
                    if not (0 <= nj < ny and 0 <= ni < nx):
                        continue
                    if labels[nj, ni] >= 0 or not is_member(nj, ni):
                        continue
                    if _residual_sign(float(residual[nj, ni])) != sign0:
                        continue
                    labels[nj, ni] = len(clusters)
                    members.append((nj, ni))
                    stack.append((nj, ni))
            clusters.append(members)
    return labels, clusters


def _blend_bridge_r(left: float, right: float) -> float:
    wa = abs(float(left))
    wb = abs(float(right))
    if wa + wb <= 1e-15:
        return 0.5 * (float(left) + float(right))
    return float((wa * float(left) + wb * float(right)) / (wa + wb))


def _build_cluster_sources(
    residual: np.ndarray,
    support: np.ndarray,
    cat: np.ndarray,
    labels: np.ndarray,
    clusters: List[List[Tuple[int, int]]],
    hybrid: dict,
) -> List[Tuple[float, float, float]]:
    amp_frac_diag = float(hybrid.get("bridge_8_amp_frac", BRIDGE_8_AMP_FRAC))
    noise = float(hybrid.get("noise_floor_dbz", NOISE_FLOOR_DBZ))
    s_mode = str(hybrid.get("bridge_s_mode", "min"))
    thirds_min = float(hybrid.get("bridge_thirds_min_length", BRIDGE_THIRDS_MIN_LENGTH))
    sources: List[Tuple[float, float, float]] = []
    ny, nx = residual.shape
    for j in range(ny):
        for i in range(nx):
            if int(cat[j, i]) != CAT_VALID or not np.isfinite(residual[j, i]):
                continue
            sources.append((float(j), float(i), float(support[j, i]) * float(residual[j, i])))

    offsets_4 = ((0, 1), (1, 0))
    offsets_8 = ((1, 1), (1, -1))
    for members in clusters:
        if len(members) < 2:
            continue
        member_set = set(members)
        for j, i in members:
            for dj, di in offsets_4 + offsets_8:
                nj, ni = j + dj, i + di
                if (nj, ni) not in member_set:
                    continue
                if labels[j, i] < 0 or labels[j, i] != labels[nj, ni]:
                    continue
                is_diag = abs(dj) + abs(di) == 2
                if is_diag:
                    ra = abs(float(residual[j, i]))
                    rb = abs(float(residual[nj, ni]))
                    if min(ra, rb) < amp_frac_diag * max(ra, rb, noise):
                        continue
                r_bridge = _blend_bridge_r(float(residual[j, i]), float(residual[nj, ni]))
                sa = float(support[j, i])
                sb = float(support[nj, ni])
                s_bridge = 0.5 * (sa + sb) if s_mode == "mean" else min(sa, sb)
                amp_b = float(s_bridge) * float(r_bridge)
                length = math.sqrt(float(dj * dj + di * di))
                fracs = (1.0 / 3.0, 0.5, 2.0 / 3.0) if length >= thirds_min else (0.5,)
                for t in fracs:
                    sources.append((float(j) + float(t) * float(dj), float(i) + float(t) * float(di), amp_b))
    return sources


def _boost_sources(
    sources: List[Tuple[float, float, float]],
    labels: np.ndarray,
    clusters: List[List[Tuple[int, int]]],
    hybrid: dict,
) -> List[Tuple[float, float, float]]:
    multi_boost = float(hybrid.get("cluster_amp_boost", 1.0))
    singleton_scale = float(hybrid.get("singleton_amp_scale", 1.0))
    if multi_boost == 1.0 and singleton_scale == 1.0:
        return sources
    min_multi = int(hybrid.get("min_cluster_size_for_boost", MIN_CLUSTER_SIZE_FOR_BOOST))
    sizes = {idx: len(members) for idx, members in enumerate(clusters)}
    out: List[Tuple[float, float, float]] = []
    ny, nx = labels.shape
    for y, x, amp in sources:
        y_i = int(np.rint(y))
        x_i = int(np.rint(x))
        is_native = abs(y - y_i) < 1e-9 and abs(x - x_i) < 1e-9
        if is_native and 0 <= y_i < ny and 0 <= x_i < nx:
            lab = int(labels[y_i, x_i])
            if lab >= 0 and sizes.get(lab, 1) >= min_multi:
                scale = multi_boost
            else:
                scale = singleton_scale
        else:
            scale = multi_boost
        out.append((float(y), float(x), float(amp) * scale))
    return out


def _bucket_sources(
    sources: Sequence[Tuple[float, float, float]],
) -> Dict[Tuple[int, int], np.ndarray]:
    """One bucket per source, at its rounded cell. Values are source indices."""
    buckets: Dict[Tuple[int, int], List[int]] = {}
    for idx, (y, x, _amp) in enumerate(sources):
        key = (int(np.rint(y)), int(np.rint(x)))
        buckets.setdefault(key, []).append(idx)
    return {key: np.asarray(idxs, dtype=np.int32) for key, idxs in buckets.items()}


@dataclass
class StructureField:
    """Native FIX2/STRUCTURE state for one frame. Read-only after prepare."""

    kind: str
    dbz: np.ndarray
    cat: np.ndarray
    trend_params: dict
    hybrid_params: dict
    samples_per_cell: int
    src_y: np.ndarray
    src_x: np.ndarray
    src_amp: np.ndarray
    buckets: Dict[Tuple[int, int], np.ndarray]


def _trend_at_valid_cells(
    dbz: np.ndarray,
    cat: np.ndarray,
    trend_params: dict,
    chunk: int = 65536,
) -> np.ndarray:
    """Plane value at each native cell center. NaN where the cell is not echo."""
    ny, nx = dbz.shape
    out = np.full((ny, nx), np.nan, dtype=np.float64)
    valid = np.argwhere((cat == CAT_VALID) & np.isfinite(dbz))
    if valid.size == 0:
        return out
    for start in range(0, valid.shape[0], chunk):
        block = valid[start : start + chunk]
        rf = block[:, 0].astype(np.float64)
        cf = block[:, 1].astype(np.float64)
        values, vcat, _lo, _hi = _trend_batch(rf, cf, dbz, cat, trend_params)
        ok = vcat == CAT_VALID
        if np.any(ok):
            js = block[ok, 0]
            iss = block[ok, 1]
            out[js, iss] = values[ok]
    return out


def prepare_structure_field(
    dbz: np.ndarray,
    cat: np.ndarray,
    kind: str,
    samples_per_cell: int = CORE_SAMPLES_PER_CELL,
) -> StructureField:
    """Build cluster residual sources for one native grid.

    ``samples_per_cell`` only sets the residual floor ``(1/spc)²``. The live
    cooker passes the core lock (32). Callers that reconstruct a small dense
    grid pass the same ``spc`` they sample with.
    """
    src = np.asarray(dbz, dtype=np.float64)
    categories = np.asarray(cat)
    if categories.shape != src.shape:
        raise ValueError("category shape must match dbz")
    token = str(kind).strip().lower().replace("-", "_")
    trend_params, hybrid = make_locked_params(token, samples_per_cell)
    trend = _trend_at_valid_cells(src, categories, trend_params)
    _T, residual, support = _support_field(src, categories, trend, hybrid)
    labels, clusters = _label_residual_clusters(residual, support, categories)
    sources = _build_cluster_sources(residual, support, categories, labels, clusters, hybrid)
    sources = _boost_sources(sources, labels, clusters, hybrid)
    if sources:
        src_y = np.asarray([p[0] for p in sources], dtype=np.float64)
        src_x = np.asarray([p[1] for p in sources], dtype=np.float64)
        src_amp = np.asarray([p[2] for p in sources], dtype=np.float64)
    else:
        src_y = np.zeros(0, dtype=np.float64)
        src_x = np.zeros(0, dtype=np.float64)
        src_amp = np.zeros(0, dtype=np.float64)
    return StructureField(
        kind=token,
        dbz=src,
        cat=categories,
        trend_params=trend_params,
        hybrid_params=hybrid,
        samples_per_cell=int(samples_per_cell),
        src_y=src_y,
        src_x=src_x,
        src_amp=src_amp,
        buckets=_bucket_sources(sources),
    )


def _trend_batch(
    rf: np.ndarray,
    cf: np.ndarray,
    dbz: np.ndarray,
    cat: np.ndarray,
    params: dict,
) -> Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Vectorized FIX2 plane. ``rf`` and ``cf`` are flat float64 arrays."""
    n_q = int(rf.shape[0])
    values = np.full(n_q, np.nan, dtype=np.float64)
    out_cat = np.full(n_q, CAT_NO_ECHO, dtype=np.uint8)
    lo = np.full(n_q, np.nan, dtype=np.float64)
    hi = np.full(n_q, np.nan, dtype=np.float64)
    if n_q == 0:
        return values, out_cat, lo, hi
    ny, nx = dbz.shape
    hw = int(params.get("half_width", HALF_WIDTH))
    min_taps = int(params.get("min_taps_for_plane", MIN_TAPS_FOR_PLANE))
    radius = float(params.get("shepard_radius_cells", SHEPARD_RADIUS_CELLS))
    soft_floor = float(params.get("soft_d2_floor", TREND_SOFT_FLOOR))
    sig = max(float(params.get("gaussian_sigma_cells", GAUSS_SIGMA_CELLS)), 1e-6)
    jn = np.clip(np.rint(rf).astype(np.int32), 0, ny - 1)
    inn = np.clip(np.rint(cf).astype(np.int32), 0, nx - 1)
    nearest_cat = cat[jn, inn]
    nearest_z = dbz[jn, inn]
    echo = (nearest_cat == CAT_VALID) & np.isfinite(nearest_z)
    missing = nearest_cat == CAT_MISSING
    out_cat[missing & ~echo] = CAT_MISSING
    out_cat[echo] = CAT_VALID
    if not np.any(echo):
        return values, out_cat, lo, hi

    z_taps = []
    d2_taps = []
    xr_taps = []
    yr_taps = []
    ok_taps = []
    for dj in range(-hw, hw + 1):
        for di in range(-hw, hw + 1):
            jr = jn + dj
            ir = inn + di
            inside = (jr >= 0) & (jr < ny) & (ir >= 0) & (ir < nx)
            jr_c = np.clip(jr, 0, ny - 1)
            ir_c = np.clip(ir, 0, nx - 1)
            z = dbz[jr_c, ir_c]
            ok = inside & echo & (cat[jr_c, ir_c] == CAT_VALID) & np.isfinite(z)
            dx = ir.astype(np.float64) - cf
            dy = jr.astype(np.float64) - rf
            d2 = dx * dx + dy * dy + soft_floor
            z_taps.append(np.where(ok, z, 0.0))
            d2_taps.append(np.where(ok, d2, 1.0))
            xr_taps.append(np.where(ok, dx, 0.0))
            yr_taps.append(np.where(ok, dy, 0.0))
            ok_taps.append(ok)

    z_stack = np.stack(z_taps, axis=1)
    d2_stack = np.stack(d2_taps, axis=1)
    xr_stack = np.stack(xr_taps, axis=1)
    yr_stack = np.stack(yr_taps, axis=1)
    ok_stack = np.stack(ok_taps, axis=1)
    weight = np.exp(-0.5 * d2_stack / (sig * sig))
    weight = np.where(ok_stack, np.maximum(weight, 1e-15), 0.0)
    n_taps = ok_stack.sum(axis=1)
    lo_v = np.min(np.where(ok_stack, z_stack, np.inf), axis=1)
    hi_v = np.max(np.where(ok_stack, z_stack, -np.inf), axis=1)

    # Normal equations for [1, xr, yr].
    sw = weight.sum(axis=1)
    swx = (weight * xr_stack).sum(axis=1)
    swy = (weight * yr_stack).sum(axis=1)
    swxx = (weight * xr_stack * xr_stack).sum(axis=1)
    swxy = (weight * xr_stack * yr_stack).sum(axis=1)
    swyy = (weight * yr_stack * yr_stack).sum(axis=1)
    swz = (weight * z_stack).sum(axis=1)
    swxz = (weight * xr_stack * z_stack).sum(axis=1)
    swyz = (weight * yr_stack * z_stack).sum(axis=1)
    normal = np.stack(
        [
            np.stack([sw, swx, swy], axis=1),
            np.stack([swx, swxx, swxy], axis=1),
            np.stack([swy, swxy, swyy], axis=1),
        ],
        axis=1,
    )
    rhs = np.stack([swz, swxz, swyz], axis=1)
    det = (
        normal[:, 0, 0] * (normal[:, 1, 1] * normal[:, 2, 2] - normal[:, 1, 2] * normal[:, 2, 1])
        - normal[:, 0, 1] * (normal[:, 1, 0] * normal[:, 2, 2] - normal[:, 1, 2] * normal[:, 2, 0])
        + normal[:, 0, 2] * (normal[:, 1, 0] * normal[:, 2, 1] - normal[:, 1, 1] * normal[:, 2, 0])
    )
    plane_ok = echo & (n_taps >= min_taps) & (np.abs(det) > 1e-14)
    plane_val = np.full(n_q, np.nan, dtype=np.float64)
    if np.any(plane_ok):
        mats = np.ascontiguousarray(normal[plane_ok])
        vecs = np.ascontiguousarray(rhs[plane_ok])
        # numpy 2 solves (..., M, M) against (..., M) or (..., M, K).
        if vecs.ndim == 1:
            vecs = vecs.reshape(1, -1)
        solved = np.linalg.solve(mats, vecs[..., None])[..., 0]
        if solved.ndim == 0:
            plane_val[plane_ok] = float(solved)
        else:
            plane_val[plane_ok] = solved[:, 0] if solved.ndim == 2 else solved

    r2 = radius * radius
    in_r = ok_stack & (d2_stack <= r2 + 1e-15)
    has_r = in_r.any(axis=1)
    use_mask = np.where(has_r[:, None], in_r, ok_stack)
    ww = np.where(use_mask, weight, 0.0)
    shepard = (ww * z_stack).sum(axis=1) / np.maximum(ww.sum(axis=1), 1e-30)
    val = np.where(plane_ok & np.isfinite(plane_val), plane_val, shepard)
    single = echo & (n_taps == 1)
    val = np.where(single, lo_v, val)
    none = echo & (n_taps == 0)
    val = np.where(none, nearest_z, val)
    val = np.where(echo & (val < lo_v) & np.isfinite(lo_v), lo_v, val)
    val = np.where(echo & (val > hi_v) & np.isfinite(hi_v), hi_v, val)
    values[echo] = val[echo]
    lo[echo] = lo_v[echo]
    hi[echo] = hi_v[echo]
    # n_taps==0 has no finite lo/hi from the min/max trick.
    lo[none] = nearest_z[none]
    hi[none] = nearest_z[none]
    return values, out_cat, lo, hi


def _source_slots(
    field: StructureField,
    jn: np.ndarray,
    inn: np.ndarray,
) -> Optional[Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, int, int]]:
    """Pack sources touched by these nearest-cell indices into a dense window."""
    if field.src_y.size == 0 or jn.size == 0:
        return None
    pad = _SOURCE_PAD
    j0 = int(jn.min()) - pad
    j1 = int(jn.max()) + pad + 1
    i0 = int(inn.min()) - pad
    i1 = int(inn.max()) + pad + 1
    height = j1 - j0
    width = i1 - i0
    if height <= 0 or width <= 0:
        return None
    occupied: List[Tuple[int, int, np.ndarray]] = []
    cap = 0
    for bj in range(j0, j1):
        for bi in range(i0, i1):
            idxs = field.buckets.get((bj, bi))
            if idxs is None or idxs.size == 0:
                continue
            occupied.append((bj - j0, bi - i0, idxs))
            if idxs.size > cap:
                cap = int(idxs.size)
    if cap == 0:
        return None
    slot_y = np.full((height, width, cap), np.nan, dtype=np.float64)
    slot_x = np.full((height, width, cap), np.nan, dtype=np.float64)
    slot_a = np.zeros((height, width, cap), dtype=np.float64)
    slot_ok = np.zeros((height, width, cap), dtype=bool)
    for lj, li, idxs in occupied:
        k = int(idxs.shape[0])
        slot_y[lj, li, :k] = field.src_y[idxs]
        slot_x[lj, li, :k] = field.src_x[idxs]
        slot_a[lj, li, :k] = field.src_amp[idxs]
        slot_ok[lj, li, :k] = True
    return slot_y, slot_x, slot_a, slot_ok, j0, i0


def _residual_from_slots(
    field: StructureField,
    rf: np.ndarray,
    cf: np.ndarray,
    jn: np.ndarray,
    inn: np.ndarray,
    slots: Optional[Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, int, int]],
) -> np.ndarray:
    """IDW of boosted cluster sources. Flat queries in, flat residual out."""
    n_q = int(rf.shape[0])
    out = np.zeros(n_q, dtype=np.float64)
    if n_q == 0 or slots is None:
        return out
    slot_y, slot_x, slot_a, slot_ok, j0, i0 = slots
    hw = int(field.hybrid_params.get("half_width", HALF_WIDTH))
    power = float(field.hybrid_params.get("residual_idw_power", IDW_POWER))
    d2_floor = float(
        field.hybrid_params.get(
            "residual_d2_floor", residual_d2_floor(CORE_SAMPLES_PER_CELL)
        )
    )
    reach = float(hw) + 0.75
    pad = _SOURCE_PAD
    lj = jn - j0
    li = inn - i0
    num = np.zeros(n_q, dtype=np.float64)
    den = np.zeros(n_q, dtype=np.float64)
    half = power / 2.0
    # Offset loop stays tighter than one giant gather on a 2 GB cook host.
    for dj in range(-pad, pad + 1):
        for di in range(-pad, pad + 1):
            yy = slot_y[lj + dj, li + di, :]
            xx = slot_x[lj + dj, li + di, :]
            aa = slot_a[lj + dj, li + di, :]
            ok = slot_ok[lj + dj, li + di, :]
            dy = yy - rf[:, None]
            dx = xx - cf[:, None]
            near = ok & (np.abs(dx) <= reach) & (np.abs(dy) <= reach)
            d2 = dx * dx + dy * dy + d2_floor
            weight = np.where(near, 1.0 / np.power(d2, half), 0.0)
            num += (weight * aa).sum(axis=1)
            den += weight.sum(axis=1)
    good = den > 0.0
    out[good] = num[good] / den[good]
    return out


def sample_structure_dbz(
    field: StructureField,
    rf: np.ndarray,
    cf: np.ndarray,
    *,
    quantize: bool = True,
    chunk: int = 8192,
) -> Tuple[np.ndarray, np.ndarray]:
    """Evaluate FIX2/STRUCTURE at native fractional indices.

    ``rf`` / ``cf`` follow the stored grid (row 0 is ``dbz[0]``). Returns
    ``(dbz, category)``. Display cooks pass ``quantize=True`` (0.5 dBZ).
    NO-ECHO and missing nearest cells stay non-valid and NaN.
    """
    row = np.asarray(rf, dtype=np.float64)
    col = np.asarray(cf, dtype=np.float64)
    if row.shape != col.shape:
        raise ValueError("rf and cf shapes must match")
    flat_r = row.ravel()
    flat_c = col.ravel()
    values = np.full(flat_r.shape, np.nan, dtype=np.float64)
    cats = np.full(flat_r.shape, CAT_NO_ECHO, dtype=np.uint8)
    ny, nx = field.dbz.shape
    jn_all = np.clip(np.rint(flat_r).astype(np.int32), 0, max(ny - 1, 0))
    inn_all = np.clip(np.rint(flat_c).astype(np.int32), 0, max(nx - 1, 0))
    slots = _source_slots(field, jn_all, inn_all) if flat_r.size else None
    for start in range(0, flat_r.size, chunk):
        sl = slice(start, start + chunk)
        trend, tcat, lo, hi = _trend_batch(
            flat_r[sl], flat_c[sl], field.dbz, field.cat, field.trend_params
        )
        residual = _residual_from_slots(
            field, flat_r[sl], flat_c[sl], jn_all[sl], inn_all[sl], slots
        )
        val = trend + residual
        echo = tcat == CAT_VALID
        val = np.where(echo & np.isfinite(lo) & (val < lo), lo, val)
        val = np.where(echo & np.isfinite(hi) & (val > hi), hi, val)
        val = np.where(echo, val, np.nan)
        values[sl] = val
        cats[sl] = tcat
    if quantize:
        values = hard_quantize_dbz(values, QUANT_STEP_DBZ)
        values[cats != CAT_VALID] = np.nan
    return values.reshape(row.shape), cats.reshape(row.shape)


def offline_continuous_at(
    field: StructureField,
    rf: float,
    cf: float,
) -> Tuple[float, int]:
    """Literal one-point port of the offline FIX2 query, for the batch check."""
    trend, cat, lo, hi = _trend_scalar(rf, cf, field.dbz, field.cat, field.trend_params)
    if cat != CAT_VALID or not np.isfinite(trend):
        return float("nan"), int(cat)
    residual = _idw_scalar(field, rf, cf)
    val = float(trend) + float(residual)
    if np.isfinite(lo) and val < lo:
        val = float(lo)
    elif np.isfinite(hi) and val > hi:
        val = float(hi)
    return val, CAT_VALID


def _idw_scalar(field: StructureField, rf: float, cf: float) -> float:
    """Offline multi-bucket IDW with dedup (continuous_add floor)."""
    hybrid = field.hybrid_params
    hw = int(hybrid.get("half_width", HALF_WIDTH))
    power = float(hybrid.get("residual_idw_power", IDW_POWER))
    d2_floor = float(hybrid.get("residual_d2_floor", residual_d2_floor(CORE_SAMPLES_PER_CELL)))
    soft_mode = str(hybrid.get("residual_soft_mode", RESIDUAL_SOFT_MODE))
    ny, nx = field.dbz.shape
    jn = min(max(int(np.rint(rf)), 0), ny - 1)
    inn = min(max(int(np.rint(cf)), 0), nx - 1)
    # Rebuild the offline multi-key buckets from the source list once per call.
    # Tests use this on a handful of points, not on tiles.
    multi: Dict[Tuple[int, int], List[Tuple[float, float, float]]] = {}
    for y, x, amp in zip(field.src_y, field.src_x, field.src_amp):
        keys = {
            (int(math.floor(y)), int(math.floor(x))),
            (int(math.floor(y)), int(math.ceil(x))),
            (int(math.ceil(y)), int(math.floor(x))),
            (int(math.ceil(y)), int(math.ceil(x))),
            (int(np.rint(y)), int(np.rint(x))),
        }
        for key in keys:
            multi.setdefault(key, []).append((float(y), float(x), float(amp)))
    num = 0.0
    den = 0.0
    seen = set()
    for dj in range(-hw - 1, hw + 2):
        for di in range(-hw - 1, hw + 2):
            for sy, sx, amp in multi.get((jn + dj, inn + di), []):
                tid = (round(sy, 6), round(sx, 6), round(amp, 9))
                if tid in seen:
                    continue
                dx = float(sx) - cf
                dy = float(sy) - rf
                if abs(dx) > hw + 0.75 or abs(dy) > hw + 0.75:
                    continue
                seen.add(tid)
                d2 = dx * dx + dy * dy
                if soft_mode == "continuous_add":
                    d2 = d2 + d2_floor
                elif soft_mode == "continuous_max":
                    d2 = max(d2, d2_floor)
                elif d2 < KNOT_EPS:
                    d2 = d2_floor
                w = 1.0 / (d2 ** (power / 2.0))
                num += w * float(amp)
                den += w
    if den <= 0.0:
        return 0.0
    return float(num / den)


def reconstruct_dense(
    dbz: np.ndarray,
    cat: np.ndarray,
    kind: str,
    samples_per_cell: int = 4,
    *,
    quantize: bool = False,
    field: Optional[StructureField] = None,
) -> Tuple[np.ndarray, np.ndarray, StructureField]:
    """Dense lattice used by the acceptance scripts, for fixtures and checks.

    Shape is ``((ny-1)*spc+1, (nx-1)*spc+1)``. Live tiles do not call this
    on a CONUS grid.
    """
    spc = int(samples_per_cell)
    prepared = field or prepare_structure_field(dbz, cat, kind, samples_per_cell=spc)
    ny, nx = prepared.dbz.shape
    out_h = (ny - 1) * spc + 1
    out_w = (nx - 1) * spc + 1
    rf = (np.arange(out_h, dtype=np.float64) / float(spc))[:, None]
    cf = (np.arange(out_w, dtype=np.float64) / float(spc))[None, :]
    rf = np.broadcast_to(rf, (out_h, out_w))
    cf = np.broadcast_to(cf, (out_h, out_w))
    values, out_cat = sample_structure_dbz(prepared, rf, cf, quantize=quantize)
    return values, out_cat, prepared


def count_no_echo_violations(
    out_cat: np.ndarray,
    native_cat: np.ndarray,
    samples_per_cell: int,
) -> int:
    """VALID dense samples whose nearest native cell is NO-ECHO.

    The offline lock is zero. Missing/no-coverage is not counted as NO-ECHO.
    """
    spc = float(samples_per_cell)
    ny, nx = native_cat.shape
    jj = np.arange(out_cat.shape[0], dtype=np.float64)
    ii = np.arange(out_cat.shape[1], dtype=np.float64)
    jn = np.clip(np.rint(jj / spc).astype(np.int32), 0, ny - 1)
    inn = np.clip(np.rint(ii / spc).astype(np.int32), 0, nx - 1)
    nearest = native_cat[jn[:, None], inn[None, :]]
    leak = (out_cat == CAT_VALID) & (nearest == CAT_NO_ECHO)
    return int(np.count_nonzero(leak))
