"""Environment-driven cooker config. Secrets come from the environment only."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from mpwg_radar.geo import (
    CONUS,
    CONUS_MAX_ZOOM,
    CONUS_MIN_ZOOM,
    REGIONS,
    BBox,
    parse_bbox,
)
from mpwg_radar.products import (
    COOKABLE_PRODUCT_IDS,
    DEFAULT_PRODUCT_ID,
    ProductSpec,
    get_product,
)


def _truthy(value: Optional[str], default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _csv(value: Optional[str], default: List[str]) -> List[str]:
    if not value or not value.strip():
        return list(default)
    return [part.strip().lower() for part in value.split(",") if part.strip()]


def _first_env(*names: str, default: str = "") -> str:
    for name in names:
        raw = os.environ.get(name)
        if raw is not None and str(raw).strip():
            return str(raw)
    return default


def resolve_region_bbox(
    region_name: str, bbox_text: Optional[str] = None
) -> tuple[str, BBox]:
    """Resolve MPWG_REGION / MPWG_BBOX (or REGION / BBOX aliases)."""
    name = (region_name or "conus").strip().lower()
    if bbox_text and bbox_text.strip():
        return name, parse_bbox(bbox_text, name=name)
    if name not in REGIONS:
        raise ValueError(f"Unknown region {name!r}. Known: {sorted(REGIONS)}")
    return name, REGIONS[name]


@dataclass
class R2Config:
    account_id: str = ""
    access_key_id: str = ""
    secret_access_key: str = ""
    bucket: str = "mpwg-radar"
    endpoint: str = ""
    prefix: str = "radar"
    public_base_url: str = ""
    # Upload limits for t4g.small → Cloudflare R2.
    # Whole-upload budget covers a CONUS frame (~800–1000 objects at concurrency=2).
    connect_timeout_seconds: float = 10.0
    read_timeout_seconds: float = 30.0
    object_timeout_seconds: float = 60.0
    upload_timeout_seconds: float = 900.0
    upload_concurrency: int = 2
    max_attempts: int = 2

    @property
    def enabled(self) -> bool:
        return bool(self.access_key_id and self.secret_access_key and self.bucket)

    @property
    def endpoint_url(self) -> str:
        if self.endpoint:
            return self.endpoint.rstrip("/")
        if self.account_id:
            return f"https://{self.account_id}.r2.cloudflarestorage.com"
        return ""


@dataclass
class CookerConfig:
    region_name: str = "conus"
    bbox: BBox = field(default_factory=lambda: CONUS)
    modes: List[str] = field(default_factory=lambda: ["clean"])
    min_zoom: int = CONUS_MIN_ZOOM
    max_zoom: int = CONUS_MAX_ZOOM
    tile_size: int = 512
    skip_empty_tiles: bool = True
    # 1 keeps composite serial. load_config raises RALA to 2 (t4g.small has
    # two vCPUs). The frame grid is shared read-only across workers.
    tile_workers: int = 1
    keep_dbz: bool = True
    data_dir: Path = Path("./data")
    output_dir: Path = Path("./output")
    retention_frames: int = 30
    # RALA rolling archive. Composite keeps retention_frames (count only).
    # A low MPWG_RETENTION_FRAMES must not thin the RALA loop. Age is the
    # operating limit (75 min ≥ a 60-minute loop). The frame cap sits above
    # a ~2-minute cadence over that window (~38 frames) so it does not subsample.
    rala_retention_frames: int = 60
    rala_retention_minutes: int = 75
    # Stop starting further RALA catch-up frames after this many seconds so
    # the oneshot can finish the in-flight upload before systemd stops it.
    # ~30 frames at a sped-up RALA upload still fit under the unit timeout.
    rala_catchup_budget_seconds: float = 2700.0
    log_level: str = "INFO"
    product_id: str = DEFAULT_PRODUCT_ID
    # NOAA MRMS endpoints. Defaults follow product_id; composite stays the
    # production QC column-max mosaic. See products.py for RALA vs unQC sibling.
    mrms_latest_url: str = (
        "https://mrms.ncep.noaa.gov/2D/MergedReflectivityQCComposite/"
        "MRMS_MergedReflectivityQCComposite.latest.grib2.gz"
    )
    mrms_s3_bucket: str = "noaa-mrms-pds"
    mrms_s3_prefix: str = "CONUS/MergedReflectivityQCComposite_00.50"
    mrms_timeout_seconds: int = 60
    upload: bool = True
    r2: R2Config = field(default_factory=R2Config)
    palette_id: str = "mpwg-clean-2026-09"
    display_min_dbz: Optional[float] = None
    user_agent: str = "mpwg-radar-tiles/1.0 (+https://github.com/reposinject/mpwg-radar-tiles)"

    @property
    def product(self) -> ProductSpec:
        return get_product(self.product_id)

    def validate(self) -> None:
        if self.bbox.west >= self.bbox.east or self.bbox.south >= self.bbox.north:
            raise ValueError("Invalid bbox (need west < east and south < north)")
        if self.min_zoom < 0 or self.max_zoom > 14 or self.min_zoom > self.max_zoom:
            raise ValueError("Invalid zoom range")
        if self.tile_workers < 1:
            raise ValueError("tile_workers must be >= 1")
        if self.tile_size != 512:
            # Supported for experiments, but production contract is 512.
            if self.tile_size < 256 or self.tile_size > 1024:
                raise ValueError("tile_size must be between 256 and 1024")
        unknown = [m for m in self.modes if m not in {"clean", "standard", "all"}]
        if unknown:
            raise ValueError(f"Unknown modes: {unknown}")
        if not self.modes:
            raise ValueError("At least one mode is required")
        if self.product_id not in COOKABLE_PRODUCT_IDS:
            raise ValueError(
                f"Unknown product {self.product_id!r}. Cookable: {list(COOKABLE_PRODUCT_IDS)}"
            )

    def __post_init__(self) -> None:
        if self.product_id == DEFAULT_PRODUCT_ID:
            return
        spec = get_product(self.product_id)
        if "MergedReflectivityQCComposite" in self.mrms_latest_url:
            self.mrms_latest_url = spec.ncep_latest_url
        if "MergedReflectivityQCComposite" in self.mrms_s3_prefix:
            self.mrms_s3_prefix = spec.s3_prefix
        if self.palette_id == "mpwg-clean-2026-09":
            self.palette_id = spec.palette_id


def load_dotenv(path: Optional[Path] = None) -> None:
    """Load a local .env if present, without overriding real environment vars."""
    env_path = path or Path(".env")
    if not env_path.is_file():
        return
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip("'").strip('"')
        if key and key not in os.environ:
            os.environ[key] = value


def load_config(overrides: Optional[dict] = None) -> CookerConfig:
    load_dotenv()
    region_name, bbox = resolve_region_bbox(
        _first_env("MPWG_REGION", "REGION", default="conus"),
        _first_env("MPWG_BBOX", "BBOX", default=""),
    )
    product_id = _first_env("MPWG_PRODUCT", default=DEFAULT_PRODUCT_ID).strip().lower()
    product = get_product(product_id)
    palette_id = os.environ.get("MPWG_PALETTE") or product.palette_id
    display_min_raw = os.environ.get("MPWG_DISPLAY_MIN_DBZ")
    display_min = float(display_min_raw) if display_min_raw else None
    # Product catalog supplies endpoints. MRMS_LATEST_URL is a composite-era
    # override: honor it for composite so existing /etc/mpwg-radar.env keeps
    # working; ignore a leftover composite URL when cooking rala.
    mrms_url = os.environ.get("MRMS_LATEST_URL", "").strip()
    mrms_prefix = os.environ.get("MRMS_S3_PREFIX", "").strip()
    if product_id != DEFAULT_PRODUCT_ID:
        if (not mrms_url) or ("MergedReflectivityQCComposite" in mrms_url):
            mrms_url = product.ncep_latest_url
        if (not mrms_prefix) or ("MergedReflectivityQCComposite" in mrms_prefix):
            mrms_prefix = product.s3_prefix
    else:
        mrms_url = mrms_url or product.ncep_latest_url
        mrms_prefix = mrms_prefix or product.s3_prefix
    cfg = CookerConfig(
        region_name=region_name,
        bbox=bbox,
        modes=_csv(os.environ.get("MPWG_MODES"), ["clean"]),
        min_zoom=int(os.environ.get("MPWG_MIN_ZOOM", str(CONUS_MIN_ZOOM))),
        max_zoom=int(os.environ.get("MPWG_MAX_ZOOM", str(CONUS_MAX_ZOOM))),
        tile_size=int(os.environ.get("MPWG_TILE_SIZE", "512")),
        skip_empty_tiles=_truthy(os.environ.get("MPWG_SKIP_EMPTY_TILES"), True),
        tile_workers=_tile_workers(product_id),
        keep_dbz=_truthy(os.environ.get("MPWG_KEEP_DBZ"), True),
        data_dir=Path(os.environ.get("MPWG_DATA_DIR", "./data")),
        output_dir=Path(os.environ.get("MPWG_OUTPUT_DIR", "./output")),
        retention_frames=int(os.environ.get("MPWG_RETENTION_FRAMES", "30")),
        rala_retention_frames=int(os.environ.get("MPWG_RALA_RETENTION_FRAMES", "60")),
        rala_retention_minutes=int(os.environ.get("MPWG_RALA_RETENTION_MINUTES", "75")),
        rala_catchup_budget_seconds=float(
            os.environ.get("MPWG_RALA_CATCHUP_BUDGET_SECONDS", "2700")
        ),
        log_level=os.environ.get("MPWG_LOG_LEVEL", "INFO").upper(),
        product_id=product_id,
        mrms_latest_url=mrms_url,
        mrms_s3_bucket=os.environ.get("MRMS_S3_BUCKET", "noaa-mrms-pds"),
        mrms_s3_prefix=mrms_prefix,
        mrms_timeout_seconds=int(os.environ.get("MRMS_TIMEOUT_SECONDS", "60")),
        upload=_truthy(os.environ.get("MPWG_UPLOAD"), True),
        r2=R2Config(
            account_id=os.environ.get("R2_ACCOUNT_ID", "").strip(),
            access_key_id=os.environ.get("R2_ACCESS_KEY_ID", "").strip(),
            secret_access_key=os.environ.get("R2_SECRET_ACCESS_KEY", "").strip(),
            bucket=os.environ.get("R2_BUCKET", "mpwg-radar").strip(),
            endpoint=os.environ.get("R2_ENDPOINT", "").strip(),
            prefix=os.environ.get("R2_PREFIX", "radar").strip().strip("/"),
            public_base_url=os.environ.get("R2_PUBLIC_BASE_URL", "").rstrip("/"),
            connect_timeout_seconds=float(
                os.environ.get("MPWG_UPLOAD_CONNECT_TIMEOUT", "10")
            ),
            read_timeout_seconds=float(
                os.environ.get("MPWG_UPLOAD_READ_TIMEOUT", "30")
            ),
            object_timeout_seconds=float(
                os.environ.get("MPWG_UPLOAD_OBJECT_TIMEOUT", "60")
            ),
            upload_timeout_seconds=float(os.environ.get("MPWG_UPLOAD_TIMEOUT", "900")),
            upload_concurrency=_upload_concurrency(product_id),
            max_attempts=int(os.environ.get("MPWG_UPLOAD_MAX_ATTEMPTS", "2")),
        ),
        palette_id=palette_id,
        display_min_dbz=display_min,
    )
    if overrides:
        for key, value in overrides.items():
            if value is not None and hasattr(cfg, key):
                setattr(cfg, key, value)
        if (
            overrides.get("region_name")
            and overrides.get("bbox") is None
            and cfg.region_name in REGIONS
        ):
            cfg.bbox = REGIONS[cfg.region_name]
        if overrides.get("product_id") and "mrms_latest_url" not in overrides:
            spec = get_product(cfg.product_id)
            cfg.mrms_latest_url = spec.ncep_latest_url
            cfg.mrms_s3_prefix = spec.s3_prefix
            if "palette_id" not in overrides or overrides.get("palette_id") is None:
                if not os.environ.get("MPWG_PALETTE"):
                    cfg.palette_id = spec.palette_id
    # Apply after --product overrides. RALA must not inherit concurrency 2:
    # at ~4 objects/s a CONUS frame cannot keep a 2-minute scan (~5 frames/hour).
    if not overrides or "r2" not in overrides:
        cfg.r2.upload_concurrency = _upload_concurrency(cfg.product_id)
    if not overrides or "tile_workers" not in overrides:
        cfg.tile_workers = _tile_workers(cfg.product_id)
    cfg.validate()
    return cfg


def _tile_workers(product_id: str) -> int:
    """RALA masked-splat defaults to 2 threads. Composite stays at 1.

    ``MPWG_TILE_WORKERS`` overrides both. Two workers match t4g.small
    (2 vCPU) without a second copy of the CONUS grid. More than that on a
    2 GB host risks the 1536M cap while composite is also cooking.
    """
    raw = os.environ.get("MPWG_TILE_WORKERS")
    if raw is not None and str(raw).strip():
        return max(1, int(raw))
    if product_id == "rala":
        return 2
    return 1


def _upload_concurrency(product_id: str) -> int:
    """Composite stays at 2. RALA defaults to 8 unless explicitly overridden.

    ``MPWG_UPLOAD_CONCURRENCY`` is the composite/shared knob. A host that set
    it to 2 must not throttle the RALA archive.
    """
    if product_id == "rala":
        raw = os.environ.get("MPWG_RALA_UPLOAD_CONCURRENCY")
        if raw is not None and str(raw).strip():
            return max(1, int(raw))
        return 8
    return max(1, int(os.environ.get("MPWG_UPLOAD_CONCURRENCY", "2")))
