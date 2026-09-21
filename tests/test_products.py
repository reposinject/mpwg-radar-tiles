from __future__ import annotations

from mpwg_radar.products import (
    MERGED_RALA_UNQC,
    RALA,
    COMPOSITE,
    DEFAULT_PRODUCT_ID,
    get_product,
)


def test_default_product_is_composite():
    assert DEFAULT_PRODUCT_ID == "composite"
    spec = get_product("composite")
    assert spec.mrms_name == "MergedReflectivityQCComposite"
    assert spec.quality_controlled is True
    assert spec.tile_prefix == ""
    assert spec.apply_dbz_floor is True
    assert spec.apply_despeckle is True
    assert spec.sample_mode == "nearest"
    assert spec.palette_id == "mpwg-clean-2026-09"


def test_rala_is_operational_param_57_not_unqc_merged():
    spec = get_product("rala")
    assert spec is RALA
    assert spec.mrms_name == "ReflectivityAtLowestAltitude"
    assert spec.quality_controlled is True
    assert spec.apply_dbz_floor is False
    assert spec.apply_despeckle is False
    assert spec.sample_mode == "masked-bilinear"
    assert spec.tile_prefix == "rala"
    assert spec.palette_id == "mpwg-rala-2026-09"
    assert "ReflectivityAtLowestAltitude" in spec.ncep_latest_url
    assert "MergedReflectivityAtLowestAltitude" not in spec.ncep_latest_url
    # Explicitly unQC sibling is documented and not cookable.
    assert MERGED_RALA_UNQC.quality_controlled is False
    assert MERGED_RALA_UNQC.mrms_name == "MergedReflectivityAtLowestAltitude"


def test_unknown_product_raises():
    import pytest

    with pytest.raises(ValueError, match="Unknown product"):
        get_product("merged-rala-unqc")
