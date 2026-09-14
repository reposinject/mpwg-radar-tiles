"""Radar data sources. MRMS is implemented; NEXRAD is a later milestone."""

from __future__ import annotations

from mpwg_radar.grib import ReflectivityFrame, decode_grib2
from mpwg_radar.geo import BBox
from mpwg_radar.synthetic import synthetic_central_texas


class RadarSource:
    name = "base"

    def load(self, **kwargs) -> ReflectivityFrame:
        raise NotImplementedError


class MrmsSource(RadarSource):
    name = "mrms"
    product = "MergedReflectivityQCComposite"

    def load(self, path, bbox: BBox) -> ReflectivityFrame:
        return decode_grib2(path, bbox=bbox, product=self.product)


class NexradSource(RadarSource):
    """Scaffold: NOAA NEXRAD Level-II from AWS Open Data (unidata/noaa-nexrad).

    Not used in production yet. Central Texas sites of interest later:
    KEWX (Austin/San Antonio), KGRK (Fort Hood/Killeen), KFWS (Dallas south).
    """

    name = "nexrad"

    def load(self, **kwargs) -> ReflectivityFrame:
        raise NotImplementedError(
            "NEXRAD Level-II ingest is scaffolded for a later milestone. "
            "Production cooker uses free NOAA MRMS MergedReflectivityQCComposite."
        )


class SyntheticSource(RadarSource):
    name = "synthetic"

    def load(self, **kwargs) -> ReflectivityFrame:
        return synthetic_central_texas()
