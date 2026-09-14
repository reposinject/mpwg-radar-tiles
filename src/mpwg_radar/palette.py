"""dBZ → RGBA colorization, kept separate from the physical grid.

The cooker always stores/resamples reflectivity in dBZ. This module is the
only place that applies the MPWG Clean palette (James, Sep 2026).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import List, Sequence, Tuple, Union

import numpy as np
from PIL import Image

RGBA = Tuple[int, int, int, int]


@dataclass(frozen=True)
class PaletteStop:
    dbz: float
    rgba: RGBA
    label: str = ""


@dataclass
class Palette:
    id: str
    name: str
    stops: List[PaletteStop]
    below_min: RGBA = (0, 0, 0, 0)
    author: str = ""
    version: str = ""
    description: str = ""

    def __post_init__(self) -> None:
        self.stops = sorted(self.stops, key=lambda s: s.dbz)
        self._lut_dbz0 = -20.0
        self._lut_step = 0.1
        self._lut = self._build_lut()

    @property
    def min_dbz(self) -> float:
        return self.stops[0].dbz if self.stops else 0.0

    def colorize(self, dbz: np.ndarray) -> np.ndarray:
        """Map a dBZ array to uint8 RGBA. NaNs and below-min stay transparent."""
        flat = np.asarray(dbz, dtype=np.float32)
        out = np.zeros(flat.shape + (4,), dtype=np.uint8)
        valid = np.isfinite(flat)
        if not np.any(valid):
            return out
        safe = np.where(valid, flat, self._lut_dbz0)
        idx = np.rint((safe - self._lut_dbz0) / self._lut_step).astype(np.int32)
        idx = np.clip(idx, 0, len(self._lut) - 1)
        colored = self._lut[idx]
        out[valid] = colored[valid]
        # Values below the first stop remain transparent even if finite.
        below = valid & (flat < self.stops[0].dbz)
        out[below] = np.array(self.below_min, dtype=np.uint8)
        return out

    def colorbar(self, width: int = 512, height: int = 48, dbz_min: float = 10, dbz_max: float = 75) -> Image.Image:
        ramp = np.linspace(dbz_min, dbz_max, width, dtype=np.float32)
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
            "stops": [
                {"dbz": s.dbz, "rgba": list(s.rgba), "label": s.label}
                for s in self.stops
            ],
        }

    def _build_lut(self) -> np.ndarray:
        dbz_max = 80.0
        n = int(round((dbz_max - self._lut_dbz0) / self._lut_step)) + 1
        lut = np.zeros((n, 4), dtype=np.uint8)
        xs = [s.dbz for s in self.stops]
        ys = np.array([s.rgba for s in self.stops], dtype=np.float32)
        for i in range(n):
            dbz = self._lut_dbz0 + i * self._lut_step
            if dbz < xs[0]:
                lut[i] = self.below_min
                continue
            if dbz >= xs[-1]:
                lut[i] = ys[-1]
                continue
            # linear interpolate in RGBA
            j = int(np.searchsorted(xs, dbz, side="right") - 1)
            t = (dbz - xs[j]) / (xs[j + 1] - xs[j])
            lut[i] = np.clip(ys[j] + t * (ys[j + 1] - ys[j]), 0, 255)
        return lut


def _load_json(payload: dict) -> Palette:
    stops = [
        PaletteStop(
            dbz=float(stop["dbz"]),
            rgba=tuple(int(c) for c in stop["rgba"]),  # type: ignore[arg-type]
            label=str(stop.get("label", "")),
        )
        for stop in payload["stops"]
    ]
    below = tuple(int(c) for c in payload.get("below_min", [0, 0, 0, 0]))
    return Palette(
        id=payload.get("id", "custom"),
        name=payload.get("name", "custom"),
        stops=stops,
        below_min=below,  # type: ignore[arg-type]
        author=str(payload.get("author", "")),
        version=str(payload.get("version", "")),
        description=str(payload.get("description", "")),
    )


def load_palette(name: str = "mpwg-clean-2026-09") -> Palette:
    if name.endswith(".json") and Path(name).is_file():
        return _load_json(json.loads(Path(name).read_text()))
    stem = Path(name).stem
    pkg = resources.files("mpwg_radar").joinpath("palettes")
    path = pkg.joinpath(f"{stem}.json")
    with path.open("r", encoding="utf-8") as fh:
        return _load_json(json.loads(fh.read()))
