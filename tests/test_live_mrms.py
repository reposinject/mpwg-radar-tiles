from __future__ import annotations

import pytest

from mpwg_radar.config import CookerConfig
from mpwg_radar.geo import CENTRAL_TEXAS
from mpwg_radar.grib import decode_grib2
from mpwg_radar.ingest import download_latest_mrms


@pytest.mark.live
def test_live_mrms_decode(tmp_path):
    cfg = CookerConfig(data_dir=tmp_path, output_dir=tmp_path, bbox=CENTRAL_TEXAS, upload=False)
    path = download_latest_mrms(cfg, tmp_path)
    frame = decode_grib2(path, bbox=CENTRAL_TEXAS)
    assert frame.dbz.ndim == 2
    assert frame.dbz.shape[0] > 50 and frame.dbz.shape[1] > 50
    assert frame.product == "MergedReflectivityQCComposite"
