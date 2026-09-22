"""dBZ → RGBA colorization, kept separate from the physical grid.

The cooker always stores/resamples reflectivity in dBZ. This module is the
only place that applies a palette. Composite uses the MPWG Clean palette
(James, Sep 2026): values below 15 dBZ are transparent. RALA uses a piecewise
RadarScope-matched ramp (version 2026-09-rala-p3b): linear RGBA between the
calibration anchors, display_min_dbz=-32 so valid weak returns stay visible,
yellow held through the low 40s, solid red from about 50 dBZ, magenta from
about 56 dBZ, and no cyan/aqua stop. colorize() does not mutate the input array.
Transparency for no-echo and missing is a category mask, not a dBZ cutoff,
except for the palette display_min.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import List, Optional, Tuple

import numpy as np
from PIL import Image

from mpwg_radar.products import CAT_VALID

RGBA = Tuple[int, int, int, int]


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

    @property
    def min_dbz(self) -> float:
        return float(self.display_min_dbz) if self.display_min_dbz is not None else 0.0

    def colorize(
        self, dbz: np.ndarray, category: Optional[np.ndarray] = None
    ) -> np.ndarray:
        """Map a dBZ array to uint8 RGBA. Input is not modified.

        Only CAT_VALID cells with finite dBZ at/above display_min_dbz get
        color. NaNs, no-echo, and missing/no-coverage stay transparent and
        are never treated as 0 dBZ. Between anchor stops, RGB is linearly
        interpolated against the actual dBZ value (0.1 dBZ LUT); values are
        not quantized to 5 dBZ buckets. Samples below the first stop but
        still at/above display_min clamp to that stop.
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
        safe = np.where(valid, flat, self._lut_dbz0)
        idx = np.rint((safe - self._lut_dbz0) / self._lut_step).astype(np.int32)
        idx = np.clip(idx, 0, len(self._lut) - 1)
        colored = self._lut[idx]
        out[valid] = colored[valid]
        below = valid & (flat < self.min_dbz)
        out[below] = np.array(self.below_min, dtype=np.uint8)
        return out

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
            return ys[0].astype(np.uint8)
        if dbz >= xs[-1]:
            return ys[-1].astype(np.uint8)
        j = int(np.searchsorted(xs, dbz, side="right") - 1)
        span = xs[j + 1] - xs[j]
        t = 0.0 if span == 0 else (dbz - xs[j]) / span
        return np.clip(ys[j] + t * (ys[j + 1] - ys[j]), 0, 255).astype(np.uint8)

    def _build_lut(self) -> np.ndarray:
        dbz_max = 80.0
        n = int(round((dbz_max - self._lut_dbz0) / self._lut_step)) + 1
        lut = np.zeros((n, 4), dtype=np.uint8)
        for i in range(n):
            dbz = self._lut_dbz0 + i * self._lut_step
            lut[i] = self._interpolate_rgba(dbz)
        return lut


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


def load_palette(name: str = "mpwg-clean-2026-09") -> Palette:
    if name.endswith(".json") and Path(name).is_file():
        return _load_json(json.loads(Path(name).read_text()))
    stem = Path(name).stem
    pkg = resources.files("mpwg_radar").joinpath("palettes")
    path = pkg.joinpath(f"{stem}.json")
    with path.open("r", encoding="utf-8") as fh:
        return _load_json(json.loads(fh.read()))
