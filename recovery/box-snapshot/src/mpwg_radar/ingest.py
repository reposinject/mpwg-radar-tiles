"""Fetch NOAA MRMS GRIB2 (NCEP HTTP, AWS Open Data fallback)."""

from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from mpwg_radar.config import CookerConfig

log = logging.getLogger(__name__)


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


@dataclass(frozen=True)
class MrmsScan:
    """One real MRMS object. `valid_time` comes from the filename, not a guess."""

    valid_time: datetime
    key: str


def download_ncep_latest(cfg: CookerConfig, dest_dir: Path) -> Path:
    """Download the NCEP `.latest` object for cfg.product."""
    return _download_ncep(cfg, dest_dir)


def download_s3_key(cfg: CookerConfig, key: str, dest_dir: Path) -> Path:
    """Download one noaa-mrms-pds object and return the local gzip path."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    url = f"https://{cfg.mrms_s3_bucket}.s3.amazonaws.com/{key}"
    log.info("Downloading MRMS from AWS Open Data %s", url)
    payload = _request(url, cfg.mrms_timeout_seconds, cfg.user_agent)
    dest = dest_dir / Path(key).name
    dest.write_bytes(payload)
    log.info("Saved %s (%d bytes)", dest, dest.stat().st_size)
    return dest


def list_recent_s3_scans(
    cfg: CookerConfig,
    *,
    max_age: timedelta,
    now: Optional[datetime] = None,
) -> list[MrmsScan]:
    """Real S3 scans whose filename time falls inside the rolling window.

    Newest first. Files without a ``YYYYMMDD-HHMMSS`` stamp are ignored.
    Listings are paginated so a full day is not truncated to the first page
    (S3 returns keys in lexicographic order, so the latest file is last).
    """
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    moment = moment.astimezone(timezone.utc)
    cutoff = moment - max_age
    future = moment + timedelta(minutes=10)
    found: dict[datetime, str] = {}
    for day in (moment, moment - timedelta(days=1)):
        prefix = f"{cfg.mrms_s3_prefix}/{day.strftime('%Y%m%d')}/"
        for key in _list_s3_grib_keys(cfg, prefix):
            when = parse_filename_time(Path(key))
            if when is None or when < cutoff or when > future:
                continue
            found.setdefault(when, key)
    scans = [MrmsScan(when, key) for when, key in found.items()]
    scans.sort(key=lambda scan: scan.valid_time, reverse=True)
    return scans


def _download_s3_latest(cfg: CookerConfig, dest_dir: Path) -> Path:
    now = datetime.now(timezone.utc)
    key = None
    for day in (now, now - timedelta(days=1)):
        prefix = f"{cfg.mrms_s3_prefix}/{day.strftime('%Y%m%d')}/"
        keys = _list_s3_grib_keys(cfg, prefix)
        if keys:
            key = sorted(keys)[-1]
            break
    if not key:
        raise IngestError("No recent MRMS objects in noaa-mrms-pds")
    return download_s3_key(cfg, key, dest_dir)


def _list_s3_grib_keys(cfg: CookerConfig, prefix: str) -> list[str]:
    keys: list[str] = []
    token: Optional[str] = None
    seen_tokens: set[str] = set()
    for _page in range(20):
        url = (
            f"https://{cfg.mrms_s3_bucket}.s3.amazonaws.com/"
            f"?list-type=2&prefix={prefix}"
        )
        if token:
            url += f"&continuation-token={quote(token, safe='')}"
        xml = _request(url, cfg.mrms_timeout_seconds, cfg.user_agent)
        root = ET.fromstring(xml)
        for key in _xml_values(root, "Key"):
            if key.endswith(".grib2.gz"):
                keys.append(key)
        truncated = _xml_values(root, "IsTruncated")
        if not truncated or truncated[0].lower() != "true":
            break
        nxt = _xml_values(root, "NextContinuationToken")
        if not nxt or nxt[0] in seen_tokens:
            break
        token = nxt[0]
        seen_tokens.add(token)
    return keys


def _xml_values(root: ET.Element, local: str) -> list[str]:
    suffix = "}" + local
    values: list[str] = []
    for node in root.iter():
        if node.tag == local or node.tag.endswith(suffix):
            if node.text and node.text.strip():
                values.append(node.text.strip())
    return values


_TIME_RE = re.compile(r"(\d{8})-(\d{6})")


def parse_filename_time(path: Path):
    match = _TIME_RE.search(path.name)
    if not match:
        return None
    return datetime.strptime(match.group(1) + match.group(2), "%Y%m%d%H%M%S").replace(
        tzinfo=timezone.utc
    )
