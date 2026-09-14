"""Publish cooked tiles + frame/manifest JSON to Cloudflare R2 (S3 API)."""

from __future__ import annotations

import logging
import mimetypes
from pathlib import Path
from typing import Iterable, List, Optional

from mpwg_radar.config import R2Config

log = logging.getLogger(__name__)

# Immutable frame tiles can be cached; latest/manifest stay short-lived.
CACHE_FRAME = "public, max-age=120, s-maxage=120"
CACHE_LATEST = "public, max-age=30, s-maxage=30"
CACHE_MANIFEST = "public, max-age=15, s-maxage=15"


def _content_type(path: Path) -> str:
    if path.suffix == ".png":
        return "image/png"
    if path.suffix == ".json":
        return "application/json; charset=utf-8"
    guessed, _ = mimetypes.guess_type(path.name)
    return guessed or "application/octet-stream"


def _cache_control(key: str) -> str:
    if key.endswith("manifest.json") or "/latest/" in key or key.endswith("/latest"):
        return CACHE_MANIFEST if key.endswith("manifest.json") else CACHE_LATEST
    return CACHE_FRAME


class R2Publisher:
    def __init__(self, cfg: R2Config):
        if not cfg.enabled:
            raise RuntimeError("R2 credentials are not set")
        if not cfg.endpoint_url:
            raise RuntimeError("R2_ENDPOINT or R2_ACCOUNT_ID is required")
        try:
            import boto3
            from botocore.config import Config
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("boto3 is required for R2 upload") from exc
        self.cfg = cfg
        self.client = boto3.client(
            "s3",
            endpoint_url=cfg.endpoint_url,
            aws_access_key_id=cfg.access_key_id,
            aws_secret_access_key=cfg.secret_access_key,
            region_name="auto",
            config=Config(signature_version="s3v4"),
        )
        self.bucket = cfg.bucket
        self.prefix = cfg.prefix.strip("/")

    def key_for(self, relative: str) -> str:
        rel = relative.lstrip("/")
        return f"{self.prefix}/{rel}" if self.prefix else rel

    def upload_file(self, local: Path, relative: str) -> str:
        key = self.key_for(relative)
        extra = {
            "ContentType": _content_type(local),
            "CacheControl": _cache_control(key),
        }
        self.client.upload_file(str(local), self.bucket, key, ExtraArgs=extra)
        return key

    def upload_tree(self, local_root: Path, relative_root: str = "") -> List[str]:
        keys: List[str] = []
        for path in sorted(local_root.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(local_root).as_posix()
            remote = f"{relative_root.rstrip('/')}/{rel}" if relative_root else rel
            keys.append(self.upload_file(path, remote))
        log.info("Uploaded %d objects to r2://%s/%s", len(keys), self.bucket, self.prefix)
        return keys

    def delete_prefix(self, relative_prefix: str) -> int:
        """Best-effort cleanup of an old frame prefix."""
        prefix = self.key_for(relative_prefix.rstrip("/") + "/")
        paginator = self.client.get_paginator("list_objects_v2")
        deleted = 0
        for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
            objs = [{"Key": obj["Key"]} for obj in page.get("Contents", [])]
            if not objs:
                continue
            self.client.delete_objects(
                Bucket=self.bucket, Delete={"Objects": objs, "Quiet": True}
            )
            deleted += len(objs)
        if deleted:
            log.info("Deleted %d objects under %s", deleted, prefix)
        return deleted
