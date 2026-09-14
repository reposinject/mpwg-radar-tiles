"""Decode NOAA MRMS GRIB2 without GDAL/eccodes.

Operational MergedReflectivityQCComposite is a single GRIB2 message on a
regular 0.01° CONUS lat/lon grid, packed with PNG (template 5.41). That lets
a t4g.small cook CONUS tiles using only Pillow + numpy.

Physical dBZ is returned as float32; colorization happens later.
"""

from __future__ import annotations

import gzip
import logging
import struct
from dataclasses import dataclass
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from typing import Optional, Tuple, Union

import numpy as np
from PIL import Image

from mpwg_radar.geo import BBox, lon_to_180

log = logging.getLogger(__name__)

# MRMS / NSSL sentinels. Kept as NaN in the physical grid.
FILL_VALUES = (-999.0, -99.0, -3.0, -1.0)


@dataclass
class GribGrid:
    """Regular lat/lon GRIB2 grid (header only)."""

    ni: int
    nj: int
    lat_first: float
    lon_first: float
    lat_last: float
    lon_last: float
    di: float
    dj: float
    scan: int
    npts: int

    @property
    def north_to_south(self) -> bool:
        # WMO scanning mode bit 2 (0x40): 0 means consecutive j scans in -j.
        return (self.scan & 0x40) == 0

    def lat_axis(self) -> np.ndarray:
        if self.north_to_south:
            return self.lat_first - self.dj * np.arange(self.nj, dtype=np.float64)
        return self.lat_first + self.dj * np.arange(self.nj, dtype=np.float64)

    def lon_axis(self) -> np.ndarray:
        lon0 = lon_to_180(self.lon_first)
        return lon0 + self.di * np.arange(self.ni, dtype=np.float64)


@dataclass
class ReflectivityFrame:
    """Physical reflectivity crop. Colorization is a separate step."""

    dbz: np.ndarray  # float32, NaN = no echo / no coverage
    lat: np.ndarray  # (ny,) degrees, north → south or south → north
    lon: np.ndarray  # (nx,) degrees in [-180, 180)
    valid_time: datetime
    product: str = "MergedReflectivityQCComposite"
    source: str = "NOAA MRMS"
    native_fill: Optional[np.ndarray] = None

    @property
    def frame_id(self) -> str:
        vt = self.valid_time.astimezone(timezone.utc)
        return vt.strftime("%Y%m%dT%H%M%SZ")

    @property
    def bbox(self) -> BBox:
        return BBox(
            west=float(np.min(self.lon)),
            south=float(np.min(self.lat)),
            east=float(np.max(self.lon)),
            north=float(np.max(self.lat)),
        )


def _read_path(path: Union[str, Path]) -> bytes:
    raw = Path(path).read_bytes()
    if len(raw) >= 2 and raw[0] == 0x1F and raw[1] == 0x8B:
        return gzip.decompress(raw)
    if raw.startswith(b"GRIB"):
        return raw
    # Some servers wrap already-compressed GRIB in an extra gzip.
    try:
        unzipped = gzip.decompress(raw)
        if unzipped.startswith(b"GRIB"):
            return unzipped
    except OSError:
        pass
    raise ValueError(f"{path} is not a GRIB2 or gzipped GRIB2 file")


def _iter_sections(blob: bytes):
    if blob[:4] != b"GRIB" or blob[7] != 2:
        raise ValueError("Not a GRIB2 message")
    total = int.from_bytes(blob[8:16], "big")
    off = 16
    while off < len(blob) - 4 and off < total:
        if blob[off : off + 4] == b"7777":
            break
        length = int.from_bytes(blob[off : off + 4], "big")
        if length < 5:
            raise ValueError(f"Invalid GRIB2 section length {length} at {off}")
        sec = blob[off + 4]
        yield sec, blob[off : off + length]
        off += length


def _parse_section1(section: bytes) -> datetime:
    year = int.from_bytes(section[12:14], "big")
    month, day, hour, minute, second = section[14:19]
    return datetime(year, month, day, hour, minute, second, tzinfo=timezone.utc)


def _parse_section3(section: bytes) -> GribGrid:
    tmpl = int.from_bytes(section[12:14], "big")
    if tmpl != 0:
        raise ValueError(f"Unsupported grid template 3.{tmpl} (need lat/lon 3.0)")
    npts = int.from_bytes(section[6:10], "big")
    ni = int.from_bytes(section[30:34], "big")
    nj = int.from_bytes(section[34:38], "big")
    lat1 = int.from_bytes(section[46:50], "big", signed=True) / 1e6
    lon1 = int.from_bytes(section[50:54], "big", signed=True) / 1e6
    lat2 = int.from_bytes(section[55:59], "big", signed=True) / 1e6
    lon2 = int.from_bytes(section[59:63], "big", signed=True) / 1e6
    di = int.from_bytes(section[63:67], "big") / 1e6
    dj = int.from_bytes(section[67:71], "big") / 1e6
    scan = section[71]
    return GribGrid(
        ni=ni,
        nj=nj,
        lat_first=lat1,
        lon_first=lon1,
        lat_last=lat2,
        lon_last=lon2,
        di=di,
        dj=dj,
        scan=scan,
        npts=npts,
    )


def _parse_section5(section: bytes) -> Tuple[int, float, int, int, int, int]:
    npts = int.from_bytes(section[5:9], "big")
    tmpl = int.from_bytes(section[9:11], "big")
    ref = struct.unpack(">f", section[11:15])[0]
    e = int.from_bytes(section[15:17], "big", signed=True)
    d = int.from_bytes(section[17:19], "big", signed=True)
    nbits = section[19]
    return tmpl, ref, e, d, nbits, npts


def _png_to_packed(png_bytes: bytes, nj: int, ni: int) -> np.ndarray:
    image = Image.open(BytesIO(png_bytes))
    packed = np.array(image)
    if packed.ndim != 2:
        packed = np.array(image.convert("I"))
    if packed.shape != (nj, ni):
        raise ValueError(
            f"PNG packed grid {packed.shape} does not match GRIB Nj,Ni {(nj, ni)}"
        )
    return packed.astype(np.uint16, copy=False)


def _unpack_dbz(packed: np.ndarray, ref: float, e: int, d: int) -> np.ndarray:
    # Y * 10^D = R + X * 2^E
    values = (ref + packed.astype(np.float32) * (2.0**e)) / (10.0**d)
    return values


def mask_fill(dbz: np.ndarray) -> np.ndarray:
    """Replace MRMS sentinels and non-physical values with NaN."""
    out = dbz.astype(np.float32, copy=True)
    bad = ~np.isfinite(out)
    for fill in FILL_VALUES:
        bad |= np.isclose(out, fill, atol=0.01)
    bad |= out < -32.0
    bad |= out > 95.0
    out[bad] = np.nan
    return out


def _crop_indices(
    lat: np.ndarray, lon: np.ndarray, bbox: BBox, pad: int = 2
) -> Tuple[slice, slice]:
    lat_min, lat_max = bbox.south, bbox.north
    lon_min, lon_max = bbox.west, bbox.east
    lat_sel = (lat >= lat_min) & (lat <= lat_max)
    lon_sel = (lon >= lon_min) & (lon <= lon_max)
    if not np.any(lat_sel) or not np.any(lon_sel):
        raise ValueError(f"BBox {bbox} does not intersect GRIB grid")
    j = np.flatnonzero(lat_sel)
    i = np.flatnonzero(lon_sel)
    j0 = max(0, int(j[0]) - pad)
    j1 = min(lat.size, int(j[-1]) + 1 + pad)
    i0 = max(0, int(i[0]) - pad)
    i1 = min(lon.size, int(i[-1]) + 1 + pad)
    return slice(j0, j1), slice(i0, i1)


def decode_grib2(
    path: Union[str, Path],
    bbox: Optional[BBox] = None,
    product: str = "MergedReflectivityQCComposite",
) -> ReflectivityFrame:
    """Decode a (gzipped) GRIB2 reflectivity field and optionally crop it."""
    blob = _read_path(path)
    valid_time: Optional[datetime] = None
    grid: Optional[GribGrid] = None
    tmpl = ref = e = d = nbits = None
    png_payload: Optional[bytes] = None
    simple_payload: Optional[bytes] = None

    for sec, data in _iter_sections(blob):
        if sec == 1:
            valid_time = _parse_section1(data)
        elif sec == 3:
            grid = _parse_section3(data)
        elif sec == 5:
            tmpl, ref, e, d, nbits, _npts = _parse_section5(data)
        elif sec == 6:
            indicator = data[5]
            if indicator not in (0, 255):
                log.warning("GRIB2 bitmap indicator %s not applied", indicator)
        elif sec == 7:
            payload = data[5:]
            if tmpl == 41:
                png_payload = payload
            elif tmpl == 0:
                simple_payload = payload
            else:
                raise ValueError(
                    f"Unsupported GRIB2 data template 5.{tmpl}. "
                    "MRMS operational composite is PNG-packed (5.41). "
                    "If NOAA switches to JPEG2000 (5.40), install eccodes/pygrib "
                    "or wgrib2 on the host."
                )

    if grid is None or valid_time is None or tmpl is None:
        raise ValueError("Incomplete GRIB2 message")

    if tmpl == 41:
        if not png_payload:
            raise ValueError("PNG-packed GRIB2 is missing section 7")
        packed = _png_to_packed(png_payload, grid.nj, grid.ni)
    elif tmpl == 0:
        packed = _unpack_simple(simple_payload or b"", grid, nbits or 16)
    else:
        raise ValueError(f"Unsupported template 5.{tmpl}")

    lat = grid.lat_axis()
    lon = grid.lon_axis()
    if bbox is not None:
        row, col = _crop_indices(lat, lon, bbox)
        packed = packed[row, col]
        lat = lat[row]
        lon = lon[col]
        log.info(
            "Cropped MRMS to %s → %s (%.2f–%.2fN, %.2f–%.2fE)",
            bbox.name or "bbox",
            packed.shape,
            float(lat.min()),
            float(lat.max()),
            float(lon.min()),
            float(lon.max()),
        )

    dbz = mask_fill(_unpack_dbz(packed, ref, e, d))
    log.info(
        "Decoded %s valid=%s grid=%s echo_cells=%d max=%.1f",
        product,
        valid_time.isoformat(),
        dbz.shape,
        int(np.isfinite(dbz).sum()),
        float(np.nanmax(dbz)) if np.isfinite(dbz).any() else float("nan"),
    )
    return ReflectivityFrame(
        dbz=dbz,
        lat=lat.astype(np.float32),
        lon=lon.astype(np.float32),
        valid_time=valid_time,
        product=product,
    )


def _unpack_simple(
    payload: bytes, grid: GribGrid, nbits: int
) -> np.ndarray:
    """Minimal simple-packing (template 5.0) fallback."""
    if nbits not in (8, 16):
        raise ValueError(f"simple packing nbits={nbits} not supported")
    count = grid.nj * grid.ni
    dtype = np.uint8 if nbits == 8 else ">u2"
    packed = np.frombuffer(payload, dtype=dtype, count=count)
    if packed.size != count:
        raise ValueError("simple-packed section 7 is the wrong size")
    return packed.reshape(grid.nj, grid.ni).astype(np.uint16)
