from __future__ import annotations

from io import BytesIO
from pathlib import Path

import numpy as np
from PIL import Image

from mpwg_radar.grib import decode_grib2, mask_fill
from mpwg_radar.geo import BBox


def _u32(n: int) -> bytes:
    return int(n).to_bytes(4, "big")


def _s32(n: int) -> bytes:
    return int(n).to_bytes(4, "big", signed=True)


def build_png_grib2(
    dbz: np.ndarray,
    lat_first: float,
    lon_first: float,
    di: float = 0.01,
    dj: float = 0.01,
    valid=(2026, 9, 14, 17, 18, 41),
) -> bytes:
    """Minimal PNG-packed GRIB2 (template 5.41) for unit tests."""
    nj, ni = dbz.shape
    packed = np.zeros((nj, ni), dtype=np.uint16)
    finite = np.isfinite(dbz)
    packed[finite] = np.clip(np.rint(dbz[finite] * 10.0 + 9990.0), 0, 65535).astype(
        np.uint16
    )
    png_buf = BytesIO()
    Image.fromarray(packed).save(png_buf, format="PNG")
    png = png_buf.getvalue()

    # Section 1 — identification (centre 161 NSSL/OAR)
    y, mo, d, h, mi, s = valid
    s1 = bytearray(21)
    s1[4] = 1
    s1[5:7] = (161).to_bytes(2, "big")
    s1[12:14] = y.to_bytes(2, "big")
    s1[14:19] = bytes([mo, d, h, mi, s])
    s1[0:4] = _u32(len(s1))

    # Section 3 — lat/lon template 3.0, scan 0 (W→E, N→S)
    lat_last = lat_first - dj * (nj - 1)
    lon_last = lon_first + di * (ni - 1)
    s3 = bytearray(72)
    s3[4] = 3
    s3[6:10] = _u32(ni * nj)
    s3[14] = 2  # earth shape (same as MRMS sample)
    s3[30:34] = _u32(ni)
    s3[34:38] = _u32(nj)
    s3[46:50] = _s32(int(round(lat_first * 1e6)))
    s3[50:54] = _s32(int(round(lon_first * 1e6)))
    s3[55:59] = _s32(int(round(lat_last * 1e6)))
    s3[59:63] = _s32(int(round(lon_last * 1e6)))
    s3[63:67] = _u32(int(round(di * 1e6)))
    s3[67:71] = _u32(int(round(dj * 1e6)))
    s3[71] = 0
    s3[0:4] = _u32(len(s3))

    s4 = bytearray(34)
    s4[4] = 4
    s4[0:4] = _u32(len(s4))

    import struct

    s5 = bytearray(21)
    s5[4] = 5
    s5[5:9] = _u32(ni * nj)
    s5[9:11] = (41).to_bytes(2, "big")  # PNG packing
    s5[11:15] = struct.pack(">f", -9990.0)
    s5[15:17] = (0).to_bytes(2, "big")  # E
    s5[17:19] = (1).to_bytes(2, "big")  # D
    s5[19] = 16
    s5[0:4] = _u32(len(s5))

    s6 = bytearray(6)
    s6[4] = 6
    s6[5] = 255
    s6[0:4] = _u32(len(s6))

    s7 = bytearray(5 + len(png))
    s7[4] = 7
    s7[5:] = png
    s7[0:4] = _u32(len(s7))

    body = bytes(s1) + bytes(s3) + bytes(s4) + bytes(s5) + bytes(s6) + bytes(s7) + b"7777"
    total = 16 + len(body)
    indicator = bytearray(16)
    indicator[0:4] = b"GRIB"
    indicator[6] = 209
    indicator[7] = 2
    indicator[8:16] = total.to_bytes(8, "big")
    return bytes(indicator) + body


def test_decode_png_grib_and_crop(tmp_path: Path):
    grid = np.full((20, 30), np.nan, dtype=np.float32)
    grid[5:12, 8:18] = 21.5
    grid[10, 12] = 47.0
    lat0, lon0 = 31.0, -98.0
    blob = build_png_grib2(grid, lat_first=lat0, lon_first=lon0)
    path = tmp_path / "tiny.grib2"
    path.write_bytes(blob)
    frame = decode_grib2(path)
    assert frame.dbz.shape == (20, 30)
    assert frame.valid_time.year == 2026
    assert abs(float(np.nanmax(frame.dbz)) - 47.0) < 0.15
    cropped = decode_grib2(
        path, bbox=BBox(west=-97.9, south=30.9, east=-97.75, north=30.97, name="t")
    )
    assert cropped.dbz.size < frame.dbz.size


def test_mask_fill_sentinels():
    arr = np.array([[-999.0, -99.0, 12.0]], dtype=np.float32)
    out = mask_fill(arr)
    assert np.isnan(out[0, 0]) and np.isnan(out[0, 1])
    assert out[0, 2] == 12.0
