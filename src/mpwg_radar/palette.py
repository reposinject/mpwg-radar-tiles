"""dBZ → RGBA colorization, kept separate from the physical grid.

The cooker always stores/resamples reflectivity in dBZ. This module is the
only place that applies a palette. Composite uses the MPWG Clean palette
(James, Sep 2026): values below 15 dBZ are transparent. RALA uses palette
revision 2026-09-rala-p3h. Stops are a piecewise RGBA ramp on actual dBZ
(0.1 dBZ LUT, half-up index, no rescale). p3h keeps the p3g mid and high
bands and retunes only the lowest valid returns: faint blue-gray, pale
blue, cool blue, cyan, then blue-green, then weak green into the existing
rich green, bright yellow into strong gold, gold into heavy orange, vivid
red into a dark red core, then pink into magenta into a hot extreme.
Ordinary green starts later than p3g. Alpha stays on the quiet curve
through 10 dBZ and is fully opaque from 20 up. colorize() does not mutate
the input array. The calibration strip and the dBZ probe call that same
colorize().
Transparency for no-echo and missing is a category mask, not a dBZ cutoff,
except for the palette display_min.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np
from PIL import Image

from mpwg_radar.products import CAT_MISSING, CAT_NO_ECHO, CAT_VALID

RGBA = Tuple[int, int, int, int]

# Calibration probes. color-diag runs every one of these through colorize().
DIAGNOSTIC_DBZ: Tuple[float, ...] = (
    0.1,
    1,
    2,
    5,
    7.5,
    10,
    12.5,
    15,
    17.5,
    20,
    22.5,
    25,
    27.5,
    30,
    32.5,
    35,
    37.5,
    40,
    42.5,
    45,
    47.5,
    50,
    52.5,
    55,
    57.5,
    60,
    62.5,
    65,
    70,
)

# Pairs that must not share a color. Each pair sits inside one 10 dBZ decade.
FAMILY_PROOF_PAIRS: Tuple[Tuple[float, float], ...] = (
    (22.0, 29.0),
    (31.0, 39.0),
    (41.0, 49.0),
)

# p3h retunes only the lowest valid RALA colors. Masked-splat color sigma
# stays at the p3g value (0.44 cell) in tiles.py.
RALA_PALETTE_VERSION = "2026-09-rala-p3h"

# RGB control points. Alpha is not stored here; rala_opacity() supplies it.
# p3g cooled the floor through 10 dBZ, but 12.5–17.5 was already ordinary
# green, so a drizzle shield still read as rain. These weak taps hold pale
# blue, cool blue, cyan, and blue-green further up. Ordinary green arrives
# only as the ramp meets the locked 20 dBZ deep green. From 20 up the
# colors are the p3g/p3f table: deep green, bright yellow, strong gold,
# heavy orange, vivid red, and a dark pure-red core. Magenta stays hot past 65.
_RALA_SAMPLE_RGB: Tuple[Tuple[float, Tuple[int, int, int]], ...] = (
    (0.0, (118, 156, 196)),
    (2.0, (84, 138, 206)),
    (5.0, (42, 112, 214)),
    (7.5, (28, 156, 208)),
    (10.0, (18, 172, 198)),
    (12.5, (16, 174, 170)),
    (15.0, (14, 166, 128)),
    (20.0, (8, 142, 18)),
    (25.0, (0, 176, 0)),
    (30.0, (20, 198, 0)),
    (32.0, (255, 246, 0)),
    (40.0, (255, 108, 0)),
    (48.0, (255, 40, 0)),
    (50.0, (255, 0, 0)),
    (56.0, (176, 0, 0)),
    (60.0, (164, 0, 48)),
    (65.0, (255, 0, 255)),
    (70.0, (255, 12, 255)),
)
# Bookends. -32 is a faint blue-gray wisp (same quiet alpha as p3g). 75 is white.
_RALA_FLOOR_DBZ = -32.0
_RALA_FLOOR_RGB = (48, 62, 80)
_RALA_WHITE_DBZ = 75.0
_RALA_WHITE_RGB = (255, 255, 255)
# Opacity: the p3e gamma curve through 10 dBZ, so weak returns stay quiet.
# A smoothstep then reaches 255 at 20. Yellow, gold, orange, red, and
# magenta (and the deep greens they sit in) are fully opaque. No step at 10.
_RALA_ALPHA_FLOOR = 56
_RALA_ALPHA_QUIET_DBZ = 10.0
_RALA_ALPHA_QUIET_REF_DBZ = 24.8
_RALA_ALPHA_GAMMA = 2.0
_RALA_ALPHA_OPAQUE_DBZ = 20.0


@dataclass(frozen=True)
class PaletteStop:
    dbz: float
    rgba: RGBA
    label: str = ""
    hex: str = ""


@dataclass
class Palette:
    id: str
    name: str
    stops: List[PaletteStop]
    below_min: RGBA = (0, 0, 0, 0)
    author: str = ""
    version: str = ""
    description: str = ""
    display_min_dbz: Optional[float] = None

    def __post_init__(self) -> None:
        self.stops = sorted(self.stops, key=lambda s: s.dbz)
        if self.display_min_dbz is None:
            self.display_min_dbz = self.stops[0].dbz if self.stops else 0.0
        self._lut_step = 0.1
        # Cover the display floor. Clean stays at -20; RALA starts at -32.
        self._lut_dbz0 = min(-20.0, float(self.min_dbz))
        self._lut = self._build_lut()
        self._snap_stop_colors()

    @property
    def min_dbz(self) -> float:
        return float(self.display_min_dbz) if self.display_min_dbz is not None else 0.0

    def colorize(
        self, dbz: np.ndarray, category: Optional[np.ndarray] = None
    ) -> np.ndarray:
        """Map a dBZ array to uint8 RGBA. Input is not modified.

        Only CAT_VALID cells with finite dBZ at/above display_min_dbz get
        color. NaNs, no-echo, and missing/no-coverage stay transparent and
        are never treated as 0 dBZ. Between anchor stops, RGBA is linearly
        interpolated against the actual dBZ value (0.1 dBZ LUT, half-up
        index). Values are not quantized to 5 or 10 dBZ buckets. Samples
        below the first stop but still at/above display_min clamp to that
        stop. There is no normalize/rescale step.
        """
        flat = np.asarray(dbz, dtype=np.float32)
        out = np.zeros(flat.shape + (4,), dtype=np.uint8)
        valid = np.isfinite(flat)
        if category is not None:
            cat = np.asarray(category)
            if cat.shape != flat.shape:
                raise ValueError("category shape must match dbz")
            valid = valid & (cat == CAT_VALID)
        if not np.any(valid):
            return out
        # Non-finite inputs must not reach the index math. They are not valid.
        safe = np.where(valid, flat, self._lut_dbz0)
        idx = self.lut_index(safe)
        colored = self._lut[idx]
        out[valid] = colored[valid]
        below = valid & (flat < self.min_dbz)
        out[below] = np.array(self.below_min, dtype=np.uint8)
        return out

    def lut_index(self, dbz: np.ndarray) -> np.ndarray:
        """0.1 dBZ bucket index. Half-up, float64, same math colorize() uses."""
        x = (np.asarray(dbz, dtype=np.float64) - float(self._lut_dbz0)) / float(
            self._lut_step
        )
        idx = np.floor(x + 0.5).astype(np.int64)
        return np.clip(idx, 0, len(self._lut) - 1).astype(np.int32)

    def bucket_dbz(self, index: int) -> float:
        """dBZ the LUT stores at `index` (the knot colorize() will read)."""
        return float(self._lut_dbz0) + int(index) * float(self._lut_step)

    def with_display_min(self, display_min_dbz: float) -> "Palette":
        """Return a copy with a different display cutoff (LUT rebuilt)."""
        return Palette(
            id=self.id,
            name=self.name,
            stops=list(self.stops),
            below_min=self.below_min,
            author=self.author,
            version=self.version,
            description=self.description,
            display_min_dbz=float(display_min_dbz),
        )

    def colorbar(
        self,
        width: int = 512,
        height: int = 48,
        dbz_min: Optional[float] = None,
        dbz_max: float = 75,
    ) -> Image.Image:
        lo = self.min_dbz if dbz_min is None else dbz_min
        ramp = np.linspace(lo, dbz_max, width, dtype=np.float32)
        grid = np.repeat(ramp[np.newaxis, :], height, axis=0)
        rgba = self.colorize(grid)
        return Image.fromarray(rgba)

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "author": self.author,
            "version": self.version,
            "description": self.description,
            "display_min_dbz": self.min_dbz,
            "stops": [
                {
                    "dbz": s.dbz,
                    "rgba": list(s.rgba),
                    "hex": s.hex,
                    "label": s.label,
                }
                for s in self.stops
            ],
        }

    def _interpolate_rgba(self, dbz: float) -> np.ndarray:
        """Linear RGB interpolation between neighboring anchors at `dbz`."""
        xs = [s.dbz for s in self.stops]
        ys = np.array([s.rgba for s in self.stops], dtype=np.float32)
        cutoff = self.min_dbz
        if dbz < cutoff:
            return np.array(self.below_min, dtype=np.uint8)
        if dbz <= xs[0]:
            return _round_u8(ys[0])
        if dbz >= xs[-1]:
            return _round_u8(ys[-1])
        j = int(np.searchsorted(xs, dbz, side="right") - 1)
        span = xs[j + 1] - xs[j]
        t = 0.0 if span == 0 else (dbz - xs[j]) / span
        return _round_u8(ys[j] + t * (ys[j + 1] - ys[j]))

    def _build_lut(self) -> np.ndarray:
        dbz_max = 80.0
        n = int(round((dbz_max - self._lut_dbz0) / self._lut_step)) + 1
        lut = np.zeros((n, 4), dtype=np.uint8)
        for i in range(n):
            dbz = self._lut_dbz0 + i * self._lut_step
            lut[i] = self._interpolate_rgba(dbz)
        return lut

    def _snap_stop_colors(self) -> None:
        """Force each stop's own bucket to that stop's RGBA.

        Half-up indexing plus binary dBZ (10.3, 24.8, …) can land a stop
        one tenth off the knot that interpolation would have written. The
        bucket colorize() actually reads for that dBZ is the stop color.
        """
        owners: Dict[int, float] = {}
        for stop in self.stops:
            idx = int(self.lut_index(np.array([stop.dbz], dtype=np.float64))[0])
            prev = owners.get(idx)
            if prev is not None and abs(prev - stop.dbz) > 1e-6:
                raise ValueError(
                    f"Palette {self.id} stops {prev} and {stop.dbz} share LUT "
                    f"index {idx} at step {self._lut_step}"
                )
            owners[idx] = float(stop.dbz)
            self._lut[idx] = np.array(stop.rgba, dtype=np.uint8)


def _load_json(payload: dict) -> Palette:
    stops = [
        PaletteStop(
            dbz=float(stop["dbz"]),
            rgba=tuple(int(c) for c in stop["rgba"]),  # type: ignore[arg-type]
            label=str(stop.get("label", "")),
            hex=str(stop.get("hex", "")),
        )
        for stop in payload["stops"]
    ]
    below = tuple(int(c) for c in payload.get("below_min", [0, 0, 0, 0]))
    display_min = payload.get("display_min_dbz")
    return Palette(
        id=payload.get("id", "custom"),
        name=payload.get("name", "custom"),
        stops=stops,
        below_min=below,  # type: ignore[arg-type]
        author=str(payload.get("author", "")),
        version=str(payload.get("version", "")),
        description=str(payload.get("description", "")),
        display_min_dbz=float(display_min) if display_min is not None else None,
    )


def _round_u8(values: np.ndarray) -> np.ndarray:
    """Half-up to uint8. Truncation was flattening shallow segments by a step."""
    arr = np.asarray(values, dtype=np.float64)
    return np.clip(np.floor(arr + 0.5), 0, 255).astype(np.uint8)


def _rala_quiet_alpha(dbz: float) -> float:
    """p3e gamma curve. Used as-is through 10 dBZ, and as the smoothstep floor."""
    if dbz >= _RALA_ALPHA_QUIET_REF_DBZ:
        return 255.0
    if dbz <= _RALA_FLOOR_DBZ:
        return float(_RALA_ALPHA_FLOOR)
    t = (float(dbz) - _RALA_FLOOR_DBZ) / (
        _RALA_ALPHA_QUIET_REF_DBZ - _RALA_FLOOR_DBZ
    )
    span = 255 - _RALA_ALPHA_FLOOR
    return _RALA_ALPHA_FLOOR + span * (t ** _RALA_ALPHA_GAMMA)


def rala_opacity(dbz: float) -> int:
    """Continuous alpha for a valid RALA dBZ. No step at 10.

    Through 10 dBZ this is the quiet curve ``56 + 199 * t^2`` (t from -32
    to 24.8). From 10 to 20 a smoothstep runs that value up to 255, and
    everything above 20 is opaque. Single-digit returns stay visible and
    quieter than the storm greens. No-echo and missing do not use this;
    the category mask forces 0.
    """
    if dbz >= _RALA_ALPHA_OPAQUE_DBZ:
        return 255
    if dbz <= _RALA_FLOOR_DBZ:
        return _RALA_ALPHA_FLOOR
    if dbz <= _RALA_ALPHA_QUIET_DBZ:
        return int(round(_rala_quiet_alpha(dbz)))
    span_dbz = _RALA_ALPHA_OPAQUE_DBZ - _RALA_ALPHA_QUIET_DBZ
    t = (float(dbz) - _RALA_ALPHA_QUIET_DBZ) / span_dbz
    smooth = t * t * (3.0 - 2.0 * t)
    a0 = _rala_quiet_alpha(_RALA_ALPHA_QUIET_DBZ)
    return int(round(a0 + (255.0 - a0) * smooth))


def _lerp_rgba(a: RGBA, b: RGBA, t: float) -> RGBA:
    return tuple(
        int(round(a[i] + t * (b[i] - a[i]))) for i in range(4)
    )  # type: ignore[return-value]


def _rala_family_label(dbz: float) -> str:
    if dbz <= _RALA_FLOOR_DBZ:
        return f"{_fmt_dbz(dbz)} dBZ faint blue-gray wisp"
    if dbz < 0.0:
        return f"{_fmt_dbz(dbz)} dBZ faint blue-gray"
    if dbz < 2.0:
        return f"{_fmt_dbz(dbz)} dBZ pale blue"
    if dbz < 5.0:
        return f"{_fmt_dbz(dbz)} dBZ light blue"
    if dbz <= 7.5:
        return f"{_fmt_dbz(dbz)} dBZ cool blue"
    if dbz <= 12.5:
        return f"{_fmt_dbz(dbz)} dBZ cyan"
    if dbz < 17.5:
        return f"{_fmt_dbz(dbz)} dBZ blue-green"
    if dbz <= 20.0:
        return f"{_fmt_dbz(dbz)} dBZ weak green"
    if dbz <= 30.0:
        return f"{_fmt_dbz(dbz)} dBZ rich green"
    if dbz <= 40.0:
        return f"{_fmt_dbz(dbz)} dBZ yellow to gold"
    if dbz < 50.0:
        return f"{_fmt_dbz(dbz)} dBZ gold to orange"
    if dbz <= 60.0:
        return f"{_fmt_dbz(dbz)} dBZ red to deep red"
    if dbz <= 65.0:
        return f"{_fmt_dbz(dbz)} dBZ pink to magenta"
    if dbz <= 70.0:
        return f"{_fmt_dbz(dbz)} dBZ magenta extreme"
    if dbz < _RALA_WHITE_DBZ:
        return f"{_fmt_dbz(dbz)} dBZ magenta toward white"
    return f"{_fmt_dbz(dbz)}+ dBZ white extreme"


def _fmt_dbz(dbz: float) -> str:
    rounded = round(float(dbz), 4)
    if abs(rounded - round(rounded)) < 1e-9:
        return str(int(round(rounded)))
    text = f"{rounded:.4f}".rstrip("0").rstrip(".")
    return text


def _rala_control_points() -> List[Tuple[float, RGBA]]:
    points: List[Tuple[float, RGBA]] = [
        (
            _RALA_FLOOR_DBZ,
            _RALA_FLOOR_RGB + (rala_opacity(_RALA_FLOOR_DBZ),),  # type: ignore[operator]
        )
    ]
    for dbz, rgb in _RALA_SAMPLE_RGB:
        points.append((dbz, rgb + (rala_opacity(dbz),)))  # type: ignore[operator]
    points.append(
        (
            _RALA_WHITE_DBZ,
            _RALA_WHITE_RGB + (rala_opacity(_RALA_WHITE_DBZ),),  # type: ignore[operator]
        )
    )
    return points


def _color_on_controls(dbz: float, controls: Sequence[Tuple[float, RGBA]]) -> RGBA:
    if dbz <= controls[0][0]:
        return controls[0][1]
    if dbz >= controls[-1][0]:
        return controls[-1][1]
    for (x0, c0), (x1, c1) in zip(controls, controls[1:]):
        if dbz <= x1 + 1e-9:
            span = x1 - x0
            t = 0.0 if span == 0 else (dbz - x0) / span
            return _lerp_rgba(c0, c1, t)
    return controls[-1][1]


def derive_rala_p3h_stops() -> List[PaletteStop]:
    """Dense p3h stops. Anchor RGB is the control table; in-between stops are computed.

    Stops sit on every 2.5 dBZ from 0 through 75, plus the calibration
    anchors, the 20 dBZ opaque knot, the -32 wisp, and white at 75.
    Every segment is linear RGBA, which is the same interpolation
    ``Palette.colorize`` uses for tiles, the calibration strip, and the
    dBZ probe. Alpha on each stop is ``rala_opacity`` of that dBZ, then
    linear between stops.
    """
    controls = _rala_control_points()
    grid = {_RALA_FLOOR_DBZ, _RALA_WHITE_DBZ, _RALA_ALPHA_OPAQUE_DBZ}
    grid.update(i * 2.5 for i in range(0, 31))  # 0, 2.5, …, 75
    grid.update(dbz for dbz, _rgb in _RALA_SAMPLE_RGB)
    stops: List[PaletteStop] = []
    for dbz in sorted(grid):
        rgba = _color_on_controls(float(dbz), controls)
        # Opacity is the ramp at this stop, not a second lerp of the controls,
        # so a 2.5 dBZ knot matches rala_opacity exactly. Between knots the
        # production LUT lerps these alphas.
        rgba = (rgba[0], rgba[1], rgba[2], rala_opacity(float(dbz)))
        hex_color = "#{:02X}{:02X}{:02X}".format(*rgba[:3])
        stops.append(
            PaletteStop(
                dbz=float(dbz),
                rgba=rgba,
                label=_rala_family_label(float(dbz)),
                hex=hex_color,
            )
        )
    return stops


def rala_p3h_document() -> dict:
    stops = derive_rala_p3h_stops()
    return {
        "id": "mpwg-rala-2026-09",
        "name": "MPWG RALA",
        "author": "James",
        "version": RALA_PALETTE_VERSION,
        "description": (
            "Phase 3h cooker LUT (2026-09-rala-p3h). Piecewise RGBA on actual "
            "dBZ, 0.1 dBZ LUT, half-up, no rescale. Anchors near 2, 10, 20, "
            "25, 32, 40, 48, 56, and 65 dBZ. Weak returns run faint blue-gray, "
            "pale blue, cool blue, cyan, then blue-green, and ordinary green "
            "starts later than p3g. From 20 dBZ up the stops match p3g: "
            "opaque deep green, 30–40 a bright yellow into strong gold, "
            "40–50 gold into heavy orange, 50–60 vivid red into a dark red "
            "core, and 60–65+ pink into magenta into an extreme that holds "
            "past 65 before white at 75. Alpha follows 56 + 199*t^2 through "
            "10 dBZ, then a smoothstep to 255 at 20, so yellow, gold, "
            "orange, red, and magenta are fully opaque. No cutoff at 10. "
            "No-echo and missing stay alpha 0 via the category mask. The "
            "weak cyan is muted, not a bright clear-air aqua. "
            "display_min_dbz=-32. Composite keeps mpwg-clean-2026-09. The "
            "calibration strip and the dBZ probe call palette.colorize, the "
            "same LUT the tiles use."
        ),
        "units": "dBZ",
        "display_min_dbz": -32,
        "below_min": [0, 0, 0, 0],
        "stops": [
            {
                "dbz": _json_dbz(stop.dbz),
                "hex": stop.hex,
                "rgba": list(stop.rgba),
                "label": stop.label,
            }
            for stop in stops
        ],
    }


def _json_dbz(dbz: float):
    rounded = round(float(dbz), 4)
    if abs(rounded - round(rounded)) < 1e-9:
        return int(round(rounded))
    return rounded


def _category_name(cat: int) -> str:
    if cat == CAT_VALID:
        return "valid"
    if cat == CAT_NO_ECHO:
        return "no-echo"
    if cat == CAT_MISSING:
        return "missing"
    return str(cat)


def trace_colorize(
    palette: Palette,
    dbz: float,
    category: Optional[int] = None,
) -> dict:
    """One value through the production colorize path.

    ``decoded`` is the float32 grid value after sentinel classification
    (the number colorize receives for a real cell). ``normalized`` is that
    same number: colorize does not rescale and does not bin to 5 or 10 dBZ.
    ``lut_index`` / ``bucket_dbz`` are the 0.1 dBZ LUT slot colorize reads.
    RGBA is ``palette.colorize`` itself, not a second implementation.
    """
    from mpwg_radar.grib import classify_dbz

    raw = float(dbz)
    decoded_arr, decoded_cat = classify_dbz(np.array([raw], dtype=np.float32))
    use_cat = int(decoded_cat[0] if category is None else category)
    decoded: Optional[float]
    if np.isfinite(decoded_arr[0]):
        decoded = float(decoded_arr[0])
    else:
        decoded = None
    cat_grid = np.array([[use_cat]], dtype=np.uint8)
    rgba_px = palette.colorize(np.array([[raw]], dtype=np.float32), category=cat_grid)[0, 0]
    rgba = tuple(int(c) for c in rgba_px)
    painted = use_cat == CAT_VALID and decoded is not None and decoded >= palette.min_dbz
    if painted:
        idx = int(palette.lut_index(np.array([decoded], dtype=np.float64))[0])
        bucket = palette.bucket_dbz(idx)
        normalized: Optional[float] = decoded
    else:
        idx = None
        bucket = None
        normalized = None
    return {
        "input_dbz": raw,
        "decoded": decoded,
        "normalized": normalized,
        "category": _category_name(use_cat),
        "lut_index": idx,
        "bucket_dbz": bucket,
        "rgb": rgba[:3],
        "alpha": rgba[3],
        "rgba": rgba,
        "hex": "#{:02X}{:02X}{:02X}".format(*rgba[:3]),
    }


def _fmt_num(value: Optional[float], places: int = 4) -> str:
    if value is None:
        return "—"
    return f"{value:.{places}f}"


def color_diag_report(
    palette: Palette,
    values: Sequence[float] = DIAGNOSTIC_DBZ,
) -> str:
    """Markdown audit: stops, probe trace, and the 10 dBZ family pairs."""
    lines = [
        f"palette `{palette.id}` version `{palette.version}`",
        f"lut_step `{palette._lut_step}` dBZ, lut_dbz0 `{palette._lut_dbz0}`, "
        f"display_min_dbz `{palette.min_dbz}`",
        "normalize: identity (float32 grid value; no rescale; no 5/10 dBZ bin)",
        "lut_index = floor((decoded - lut_dbz0) / lut_step + 0.5)",
        "RGBA is palette.colorize (category mask). NO-ECHO and missing skip the LUT.",
        "",
        "### Stop table",
        "",
        "| dBZ | hex | R | G | B | A | label |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for stop in palette.stops:
        r, g, b, a = stop.rgba
        lines.append(
            f"| {_fmt_dbz(stop.dbz)} | `{stop.hex}` | {r} | {g} | {b} | {a} | {stop.label} |"
        )
    lines.extend(
        [
            "",
            "### Sample diagnostic",
            "",
            "| input dBZ | decoded | normalized | category | lut_index | bucket dBZ | R | G | B | A | RGBA |",
            "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
        ]
    )
    for value in values:
        row = trace_colorize(palette, float(value))
        lines.append(_trace_row(row))
    lines.extend(
        [
            "",
            "### Category mask (not a dBZ cutoff)",
            "",
            "| input dBZ | decoded | normalized | category | lut_index | bucket dBZ | R | G | B | A | RGBA |",
            "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
        ]
    )
    for raw, cat in ((-99.0, None), (-999.0, None), (5.0, CAT_NO_ECHO), (-20.0, CAT_VALID)):
        row = trace_colorize(palette, raw, category=cat)
        lines.append(_trace_row(row))
    lines.extend(
        [
            "",
            "### 10 dBZ family proof",
            "",
            "| pair | RGBA a | RGBA b | max channel Δ | lut index Δ |",
            "| --- | --- | --- | --- | --- |",
        ]
    )
    for lo, hi in FAMILY_PROOF_PAIRS:
        a = trace_colorize(palette, lo)
        b = trace_colorize(palette, hi)
        delta = max(abs(a["rgba"][i] - b["rgba"][i]) for i in range(3))
        index_delta = int(b["lut_index"]) - int(a["lut_index"])
        lines.append(
            f"| { _fmt_dbz(lo) } vs { _fmt_dbz(hi) } | `{a['rgba']}` | `{b['rgba']}` | {delta} | {index_delta} |"
        )
    lines.append("")
    return "\n".join(lines)


def _trace_row(row: dict) -> str:
    idx = "—" if row["lut_index"] is None else str(row["lut_index"])
    r, g, b = row["rgb"]
    rgba = ", ".join(str(c) for c in row["rgba"])
    return (
        f"| {_fmt_num(row['input_dbz'], 1)} | {_fmt_num(row['decoded'])} | "
        f"{_fmt_num(row['normalized'])} | {row['category']} | {idx} | "
        f"{_fmt_num(row['bucket_dbz'], 1)} | {r} | {g} | {b} | {row['alpha']} | {rgba} |"
    )


def load_palette(name: str = "mpwg-clean-2026-09") -> Palette:
    if name.endswith(".json") and Path(name).is_file():
        return _load_json(json.loads(Path(name).read_text()))
    stem = Path(name).stem
    pkg = resources.files("mpwg_radar").joinpath("palettes")
    path = pkg.joinpath(f"{stem}.json")
    with path.open("r", encoding="utf-8") as fh:
        return _load_json(json.loads(fh.read()))
