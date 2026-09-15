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
    # Fail-fast upload limits. Defaults fit t4g.small → Cloudflare R2.
    connect_timeout_seconds: float = 10.0
    read_timeout_seconds: float = 30.0
    object_timeout_seconds: float = 60.0
    upload_timeout_seconds: float = 180.0
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
    keep_dbz: bool = True
    data_dir: Path = Path("./data")
    output_dir: Path = Path("./output")
    retention_frames: int = 30
    log_level: str = "INFO"
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
    user_agent: str = "mpwg-radar-tiles/1.0 (+https://github.com/reposinject/mpwg-radar-tiles)"

    def validate(self) -> None:
        if self.bbox.west >= self.bbox.east or self.bbox.south >= self.bbox.north:
            raise ValueError("Invalid bbox (need west < east and south < north)")
        if self.min_zoom < 0 or self.max_zoom > 14 or self.min_zoom > self.max_zoom:
            raise ValueError("Invalid zoom range")
        if self.tile_size != 512:
            # Supported for experiments, but production contract is 512.
            if self.tile_size < 256 or self.tile_size > 1024:
                raise ValueError("tile_size must be between 256 and 1024")
        unknown = [m for m in self.modes if m not in {"clean", "standard", "all"}]
        if unknown:
            raise ValueError(f"Unknown modes: {unknown}")
        if not self.modes:
            raise ValueError("At least one mode is required")


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
    cfg = CookerConfig(
        region_name=region_name,
        bbox=bbox,
        modes=_csv(os.environ.get("MPWG_MODES"), ["clean"]),
        min_zoom=int(os.environ.get("MPWG_MIN_ZOOM", str(CONUS_MIN_ZOOM))),
        max_zoom=int(os.environ.get("MPWG_MAX_ZOOM", str(CONUS_MAX_ZOOM))),
        tile_size=int(os.environ.get("MPWG_TILE_SIZE", "512")),
        skip_empty_tiles=_truthy(os.environ.get("MPWG_SKIP_EMPTY_TILES"), True),
        keep_dbz=_truthy(os.environ.get("MPWG_KEEP_DBZ"), True),
        data_dir=Path(os.environ.get("MPWG_DATA_DIR", "./data")),
        output_dir=Path(os.environ.get("MPWG_OUTPUT_DIR", "./output")),
        retention_frames=int(os.environ.get("MPWG_RETENTION_FRAMES", "30")),
        log_level=os.environ.get("MPWG_LOG_LEVEL", "INFO").upper(),
        mrms_latest_url=os.environ.get(
            "MRMS_LATEST_URL",
            CookerConfig.mrms_latest_url,
        ),
        mrms_s3_bucket=os.environ.get("MRMS_S3_BUCKET", "noaa-mrms-pds"),
        mrms_s3_prefix=os.environ.get(
            "MRMS_S3_PREFIX", "CONUS/MergedReflectivityQCComposite_00.50"
        ),
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
            upload_timeout_seconds=float(os.environ.get("MPWG_UPLOAD_TIMEOUT", "180")),
            upload_concurrency=int(os.environ.get("MPWG_UPLOAD_CONCURRENCY", "2")),
            max_attempts=int(os.environ.get("MPWG_UPLOAD_MAX_ATTEMPTS", "2")),
        ),
        palette_id=os.environ.get("MPWG_PALETTE", "mpwg-clean-2026-09"),
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
    cfg.validate()
    return cfg
