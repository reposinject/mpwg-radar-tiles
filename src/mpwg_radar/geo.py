"""Central Texas region, Web Mercator XYZ math, and 512px tile coverage.

XYZ indices match OSM/Google: tile (z, x, y) covers the same geographic
extent as a standard 256px slippy map tile. We just sample it at 512×512.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable, Iterator, List, Sequence, Tuple

# Web Mercator latitude limit used by OSM/Google XYZ.
_MAX_LAT = 85.05112878


@dataclass(frozen=True)
class BBox:
    west: float
    south: float
    east: float
    north: float
    name: str = ""

    def padded(self, deg: float) -> "BBox":
        return BBox(
            west=self.west - deg,
            south=self.south - deg,
            east=self.east + deg,
            north=self.north + deg,
            name=self.name,
        )

    def as_dict(self) -> dict:
        return {
            "west": self.west,
            "south": self.south,
            "east": self.east,
            "north": self.north,
            "name": self.name,
        }


# I-35 corridor: San Antonio – Austin – Waco, Hill Country to College Station.
# Wide enough for metro context, small enough that t4g.small never renders CONUS.
CENTRAL_TEXAS = BBox(
    west=-100.25,
    south=28.85,
    east=-96.15,
    north=32.55,
    name="central-texas",
)

REGIONS = {
    "central-texas": CENTRAL_TEXAS,
}


def lon_to_180(lon: float) -> float:
    """Normalize longitude to [-180, 180)."""
    x = ((lon + 180.0) % 360.0) - 180.0
    return x if x > -180.0 or lon == -180.0 else 180.0 - 1e-12


def latlon_to_global_xy(lon: float, lat: float, zoom: int) -> Tuple[float, float]:
    """Fractional OSM tile coordinates at `zoom` (origin NW, y south-positive)."""
    lat = max(min(lat, _MAX_LAT), -_MAX_LAT)
    n = 1 << zoom
    x = (lon + 180.0) / 360.0 * n
    lat_rad = math.radians(lat)
    y = (1.0 - math.asinh(math.tan(lat_rad)) / math.pi) / 2.0 * n
    return x, y


def global_xy_to_lonlat(x: float, y: float, zoom: int) -> Tuple[float, float]:
    n = 1 << zoom
    lon = x / n * 360.0 - 180.0
    lat_rad = math.atan(math.sinh(math.pi * (1.0 - 2.0 * y / n)))
    return lon, math.degrees(lat_rad)


def tile_bounds(z: int, x: int, y: int) -> BBox:
    west, north = global_xy_to_lonlat(x, y, z)
    east, south = global_xy_to_lonlat(x + 1, y + 1, z)
    return BBox(west=west, south=south, east=east, north=north)


def tiles_for_bbox(bbox: BBox, zoom: int) -> List[Tuple[int, int, int]]:
    """Inclusive list of XYZ tiles that intersect `bbox` at `zoom`."""
    n = 1 << zoom
    x0, y0 = latlon_to_global_xy(bbox.west, bbox.north, zoom)
    x1, y1 = latlon_to_global_xy(bbox.east, bbox.south, zoom)
    xmin = max(0, int(math.floor(x0)))
    xmax = min(n - 1, int(math.floor(x1)))
    ymin = max(0, int(math.floor(y0)))
    ymax = min(n - 1, int(math.floor(y1)))
    if xmax < xmin:
        xmax = xmin
    if ymax < ymin:
        ymax = ymin
    out: List[Tuple[int, int, int]] = []
    for x in range(xmin, xmax + 1):
        for y in range(ymin, ymax + 1):
            out.append((zoom, x, y))
    return out


def iter_tiles(
    bbox: BBox, min_zoom: int, max_zoom: int
) -> Iterator[Tuple[int, int, int]]:
    for z in range(min_zoom, max_zoom + 1):
        yield from tiles_for_bbox(bbox, z)


def tile_pixel_centers(
    z: int, x: int, y: int, tile_size: int
) -> Tuple[Sequence[float], Sequence[float]]:
    """Return 1-D fractional global x and y coordinates of pixel centers."""
    # Built as lists; caller meshgrids with numpy.
    step = 1.0 / tile_size
    xs = [x + (i + 0.5) * step for i in range(tile_size)]
    ys = [y + (j + 0.5) * step for j in range(tile_size)]
    return xs, ys


def count_tiles(bbox: BBox, min_zoom: int, max_zoom: int) -> int:
    return sum(1 for _ in iter_tiles(bbox, min_zoom, max_zoom))
