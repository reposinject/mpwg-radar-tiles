"""NOAA MRMS product catalog.

Production stays on **composite** (`MergedReflectivityQCComposite`) until James
signs off. Phase 1 adds **rala** as a parallel cook path.

Product choice (QC vs unQC)
---------------------------
NSSL MRMS v12.2 GRIB2 user table
(https://github.com/NOAA-National-Severe-Storms-Laboratory/mrms-support):

| Param | Name | Description |
| 209.3.57 | ReflectivityAtLowestAltitude | operational RALA |
| 209.3.58 | MergedReflectivityAtLowestAltitude | **"Non Quality Controlled Reflectivity At Lowest Altitude"** |

There is no public GRIB2 named `ReflectivityAtLowestAltitudeQC`. WDTD documents
operational RALA as derived from the 3D reflectivity cube (clutter / AP /
bioscatter removed; bright band remains). RadarScope "Typed Reflectivity at
Lowest Altitude" is this RALA **dBZ field** colored by MRMS `PrecipFlag`
(rain/snow/convection/…). Public NOAA does not ship a single typed-RALA GRIB2;
this cooker ingests the dBZ field only (PrecipFlag typing is a later pass).

We therefore ingest **ReflectivityAtLowestAltitude** (param 57), not the
explicitly unQC Merged sibling.

NSSL sentinels for these dBZ mosaics (same table): Missing=-99, No Coverage=-999.
GRIB2 sometimes also uses -3 for no coverage (format-dependent; see flag table).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional


DEFAULT_PRODUCT_ID = "composite"

# Tile sampling. nearest is one source cell per pixel (composite).
# masked-bilinear keeps that nearest-cell footprint and blends dBZ only
# among valid neighbors, so clear air never inherits echo color.
SAMPLE_NEAREST = "nearest"
SAMPLE_MASKED_BILINEAR = "masked-bilinear"
# Contour drawn inside the echo mask. Clear-air cells stay empty; the visible
# edge is a smoothed iso-line, not the square MRMS cell.
SAMPLE_MASKED_SPLAT = "masked-splat"

# Physical-grid categories, kept through QC and into colorize.
# 0 is missing so an uninitialized mask cannot render as green.
CAT_MISSING = 0  # no coverage / invalid / out of mosaic
CAT_VALID = 1  # legitimate reflectivity, including weak dBZ
CAT_NO_ECHO = 2  # sampled, no return (NSSL Missing=-99)

# NSSL UserTable_MRMS_v12.2.csv for ReflectivityAtLowestAltitude /
# MergedReflectivityQCComposite: Missing=-99, No Coverage=-999.
NO_ECHO_VALUES = (-99.0,)
NO_COVERAGE_VALUES = (-999.0, -3.0, -1.0)
VALID_DBZ_MIN = -32.0
VALID_DBZ_MAX = 95.0


@dataclass(frozen=True)
class ProductSpec:
    id: str
    mrms_name: str
    ncep_dir: str
    s3_prefix: str
    palette_id: str
    tile_prefix: str
    quality_controlled: bool
    description: str
    # None = use qc.MODES[mode].min_dbz (composite Clean keeps the ~10 dBZ floor).
    # For RALA, James forbids arbitrary 10/15/20 dBZ blanking this pass.
    min_dbz_override: Optional[float] = None
    apply_dbz_floor: bool = True
    # Composite Clean drops speckle. RALA keeps isolated valid cells.
    apply_despeckle: bool = True
    sample_mode: str = SAMPLE_NEAREST
    # RALA: bilateral smooth inside the echo mask (cores stay, clear air stays empty).
    edge_aware_smooth: bool = False
    # When false, Clean's 3×3 / bilateral grid smooth is skipped. RALA smooths
    # at sample time so the contour is sub-cell, not another square grid.
    apply_grid_smooth: bool = True
    attribution: str = "NOAA MRMS"

    @property
    def ncep_latest_url(self) -> str:
        return (
            f"https://mrms.ncep.noaa.gov/2D/{self.ncep_dir}/"
            f"MRMS_{self.mrms_name}.latest.grib2.gz"
        )

    def mode_dir(self, radar_root, mode: str):
        """Local directory that holds `{frameId}/` and `latest/` for a mode."""
        if self.tile_prefix:
            return radar_root / self.tile_prefix / mode
        return radar_root / mode

    def tile_url_template(self, mode: str, slot: str) -> str:
        if self.tile_prefix:
            return f"{self.tile_prefix}/{mode}/{slot}/{{z}}/{{x}}/{{y}}.png"
        return f"{mode}/{slot}/{{z}}/{{x}}/{{y}}.png"

    def colorbar_rel(self) -> str:
        if self.tile_prefix:
            return f"{self.tile_prefix}/colorbar.png"
        return "colorbar.png"

    def as_public_dict(self) -> dict:
        return {
            "id": self.id,
            "mrms_product": self.mrms_name,
            "quality_controlled": self.quality_controlled,
            "ncep_path": f"/2D/{self.ncep_dir}/",
            "s3_prefix": self.s3_prefix,
            "palette_id": self.palette_id,
            "tile_prefix": self.tile_prefix or None,
            "sample_mode": self.sample_mode,
            "description": self.description,
        }


COMPOSITE = ProductSpec(
    id="composite",
    mrms_name="MergedReflectivityQCComposite",
    ncep_dir="MergedReflectivityQCComposite",
    s3_prefix="CONUS/MergedReflectivityQCComposite_00.50",
    palette_id="mpwg-clean-2026-09",
    tile_prefix="",  # production URLs stay radar/clean/... until James signs off
    quality_controlled=True,
    apply_dbz_floor=True,
    apply_despeckle=True,
    sample_mode=SAMPLE_NEAREST,
    description=(
        "QC column-max composite mosaic. Production default. Display cutoff "
        "15 dBZ (Clean palette); Clean mode still drops <~10 dBZ clutter on "
        "the mode grid."
    ),
    attribution="NOAA MRMS MergedReflectivityQCComposite",
)

RALA = ProductSpec(
    id="rala",
    mrms_name="ReflectivityAtLowestAltitude",
    ncep_dir="ReflectivityAtLowestAltitude",
    s3_prefix="CONUS/ReflectivityAtLowestAltitude_00.50",
    palette_id="mpwg-rala-2026-09",
    tile_prefix="rala",
    quality_controlled=True,
    apply_dbz_floor=False,
    apply_despeckle=False,
    sample_mode=SAMPLE_MASKED_SPLAT,
    edge_aware_smooth=False,
    apply_grid_smooth=False,
    min_dbz_override=None,
    description=(
        "Operational MRMS Reflectivity at Lowest Altitude (NSSL param 57). "
        "Closest public NOAA dBZ field to RadarScope Typed RALA; typing itself "
        "is PrecipFlag (not ingested this pass). Not "
        "MergedReflectivityAtLowestAltitude, which NSSL labels non-QC. "
        "No 10/15/20 dBZ blanking. Tiles are a mask-clipped contour: clear-air "
        "cells stay empty, and a short inset knocks the square rim off the "
        "echo without a multi-cell halo. dBZ is resampled locally inside "
        "that mask. Valid weak returns stay visible."
    ),
    attribution="NOAA MRMS ReflectivityAtLowestAltitude",
)

# Documented sibling — not cooked. Kept so tests/docs can name it.
MERGED_RALA_UNQC = ProductSpec(
    id="merged-rala-unqc",
    mrms_name="MergedReflectivityAtLowestAltitude",
    ncep_dir="MergedReflectivityAtLowestAltitude",
    s3_prefix="CONUS/MergedReflectivityAtLowestAltitude_00.50",
    palette_id="mpwg-rala-2026-09",
    tile_prefix="merged-rala-unqc",
    quality_controlled=False,
    apply_dbz_floor=False,
    description=(
        "NSSL: 'Non Quality Controlled Reflectivity At Lowest Altitude' "
        "(param 58). Not used; listed so we do not pick it by accident."
    ),
)

PRODUCTS: Dict[str, ProductSpec] = {
    COMPOSITE.id: COMPOSITE,
    RALA.id: RALA,
}

COOKABLE_PRODUCT_IDS = tuple(PRODUCTS.keys())


def get_product(product_id: Optional[str]) -> ProductSpec:
    name = (product_id or DEFAULT_PRODUCT_ID).strip().lower()
    if name not in PRODUCTS:
        raise ValueError(
            f"Unknown product {name!r}. Cookable: {list(PRODUCTS)} "
            "(merged-rala-unqc is documented only, not cooked)"
        )
    return PRODUCTS[name]
