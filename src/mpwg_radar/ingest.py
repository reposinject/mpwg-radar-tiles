"""Fetch NOAA MRMS GRIB2 (NCEP HTTP, AWS Open Data fallback)."""

from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from mpwg_radar.config import CookerConfig

log = logging.getLogger(__name__)

_S3_NS = {"s3": "http://s3.amazonaws.com/doc/2006-03-01/"}


class IngestError(RuntimeError):
    pass


def _request(url: str, timeout: int, user_agent: str) -> bytes:
    req = Request(url, headers={"User-Agent": user_agent, "Accept": "*/*"})
    with urlopen(req, timeout=timeout) as resp:
        return resp.read()


def download_latest_mrms(cfg: CookerConfig, dest_dir: Path) -> Path:
    """Download the latest CONUS field for cfg.product_id and return the gzip path."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    errors = []
    try:
        return _download_ncep(cfg, dest_dir)
    except Exception as exc:  # noqa: BLE001 — fall back to S3
        errors.append(f"NCEP: {exc}")
        log.warning("NCEP latest failed (%s); trying NOAA MRMS on AWS S3", exc)
    try:
        return _download_s3_latest(cfg, dest_dir)
    except Exception as exc:  # noqa: BLE001
        errors.append(f"S3: {exc}")
        raise IngestError(
            "Could not ingest NOAA MRMS from NCEP or AWS Open Data: "
            + " | ".join(errors)
        ) from exc


def _download_ncep(cfg: CookerConfig, dest_dir: Path) -> Path:
    url = cfg.mrms_latest_url
    log.info("Downloading MRMS %s latest from NCEP %s", cfg.product_id, url)
    payload = _request(url, cfg.mrms_timeout_seconds, cfg.user_agent)
    if len(payload) < 1000:
        raise IngestError(f"NCEP response too small ({len(payload)} bytes)")
    dest = dest_dir / f"MRMS_{cfg.product.mrms_name}.latest.grib2.gz"
    dest.write_bytes(payload)
    log.info("Saved %s (%d bytes)", dest, dest.stat().st_size)
    return dest


def _download_s3_latest(cfg: CookerConfig, dest_dir: Path) -> Path:
    now = datetime.now(timezone.utc)
    key = None
    for day in (now, now - timedelta(days=1)):
        prefix = f"{cfg.mrms_s3_prefix}/{day.strftime('%Y%m%d')}/"
        key = _latest_s3_key(cfg, prefix)
        if key:
            break
    if not key:
        raise IngestError("No recent MRMS objects in noaa-mrms-pds")
    url = f"https://{cfg.mrms_s3_bucket}.s3.amazonaws.com/{key}"
    log.info("Downloading MRMS from AWS Open Data %s", url)
    payload = _request(url, cfg.mrms_timeout_seconds, cfg.user_agent)
    name = Path(key).name
    dest = dest_dir / name
    dest.write_bytes(payload)
    log.info("Saved %s (%d bytes)", dest, dest.stat().st_size)
    return dest


def _latest_s3_key(cfg: CookerConfig, prefix: str) -> Optional[str]:
    url = (
        f"https://{cfg.mrms_s3_bucket}.s3.amazonaws.com/"
        f"?list-type=2&prefix={prefix}"
    )
    xml = _request(url, cfg.mrms_timeout_seconds, cfg.user_agent)
    root = ET.fromstring(xml)
    keys = []
    for node in root.findall("s3:Contents", _S3_NS):
        key_el = node.find("s3:Key", _S3_NS)
        if key_el is not None and key_el.text and key_el.text.endswith(".grib2.gz"):
            keys.append(key_el.text)
    # Some S3 listings omit the namespace.
    if not keys:
        for node in root.iter():
            if node.tag.endswith("Key") and node.text and node.text.endswith(".grib2.gz"):
                keys.append(node.text)
    if not keys:
        return None
    keys.sort()
    return keys[-1]


_TIME_RE = re.compile(r"(\d{8})-(\d{6})")


def parse_filename_time(path: Path):
    match = _TIME_RE.search(path.name)
    if not match:
        return None
    return datetime.strptime(match.group(1) + match.group(2), "%Y%m%d%H%M%S").replace(
        tzinfo=timezone.utc
    )
