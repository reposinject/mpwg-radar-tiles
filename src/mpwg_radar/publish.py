"""Publish cooked tiles + frame/manifest JSON to Cloudflare R2 (S3 API).

Each cook uploads only the new frame, latest pointers, colorbar, and
manifest. Retained historical frames stay on disk/R2 and are not re-sent.
Uploads use put_object (no boto3 TransferManager thread pool), per-object
and overall deadlines, and a small worker pool sized for t4g.small.
"""

from __future__ import annotations

import logging
import mimetypes
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from mpwg_radar.config import R2Config
from mpwg_radar.products import DEFAULT_PRODUCT_ID, get_product

log = logging.getLogger(__name__)

# Immutable frame tiles can be cached; latest/manifest stay short-lived.
CACHE_FRAME = "public, max-age=120, s-maxage=120"
CACHE_LATEST = "public, max-age=30, s-maxage=30"
CACHE_MANIFEST = "public, max-age=15, s-maxage=15"

ROOT_FILES = frozenset({"manifest.json", "colorbar.png"})


class UploadError(RuntimeError):
    """R2 upload failed; remaining objects were not sent."""


class UploadTimeoutError(UploadError):
    """Per-object or overall upload deadline elapsed."""


@dataclass
class UploadStats:
    started: int = 0
    uploaded: int = 0
    failed: int = 0
    skipped: int = 0
    duration_seconds: float = 0.0
    keys: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)

    def as_log_fields(self) -> str:
        return (
            f"started={self.started} uploaded={self.uploaded} "
            f"failed={self.failed} skipped={self.skipped} "
            f"duration={self.duration_seconds:.2f}s"
        )


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


def _iter_files(root: Path) -> Iterable[Path]:
    if not root.exists():
        return
    yield from sorted(path for path in root.rglob("*") if path.is_file())


def classify_radar_files(
    local_root: Path,
    frame_id: str,
    modes: Sequence[str],
    product_id: str = DEFAULT_PRODUCT_ID,
) -> Tuple[Dict[str, List[Tuple[Path, str]]], int]:
    """Split radar_root into upload groups for this cook; count skipped files.

    Groups (upload order): frame → root (colorbar) → latest → manifest.
    Historical retained frames and *other products* are skipped so they
    are not re-uploaded (composite production stays put during a RALA cook).
    """
    mode_set = {m.strip().lower() for m in modes if m.strip()}
    groups: Dict[str, List[Tuple[Path, str]]] = {
        "frame": [],
        "root": [],
        "latest": [],
        "manifest": [],
    }
    skipped = 0
    for path in _iter_files(local_root):
        rel = path.relative_to(local_root).as_posix()
        bucket = _upload_group(rel, frame_id, mode_set, product_id)
        if bucket is None:
            skipped += 1
            continue
        groups[bucket].append((path, rel))
    return groups, skipped


def _other_product_prefixes(product_id: str) -> set[str]:
    from mpwg_radar.products import PRODUCTS

    skip = set()
    for pid, spec in PRODUCTS.items():
        if pid == product_id:
            continue
        if spec.tile_prefix:
            skip.add(spec.tile_prefix.strip("/"))
    return skip


def _upload_group(
    rel: str,
    frame_id: str,
    modes: set[str],
    product_id: str = DEFAULT_PRODUCT_ID,
) -> Optional[str]:
    if rel == "manifest.json":
        return "manifest"
    spec = get_product(product_id)
    prefix = (spec.tile_prefix or "").strip("/")
    other = _other_product_prefixes(product_id)
    first = rel.split("/", 1)[0]
    if first in other:
        return None

    rest = rel
    if prefix:
        if rel == f"{prefix}/colorbar.png":
            return "root"
        if not rel.startswith(prefix + "/"):
            # Composite colorbar/tiles are not part of a prefixed product cook.
            return None
        rest = rel[len(prefix) + 1 :]
    else:
        if rel in ROOT_FILES:
            return "root"

    parts = rest.split("/")
    if len(parts) < 2:
        return None
    mode, slot = parts[0], parts[1]
    if mode not in modes:
        return None
    if slot == "latest":
        return "latest"
    if slot == frame_id:
        return "frame"
    return None


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
        self.connect_timeout = float(cfg.connect_timeout_seconds)
        self.read_timeout = float(cfg.read_timeout_seconds)
        self.upload_timeout = float(cfg.upload_timeout_seconds)
        self.object_timeout = float(cfg.object_timeout_seconds)
        self.concurrency = max(1, int(cfg.upload_concurrency))
        self.max_attempts = max(1, int(cfg.max_attempts))
        self.client = boto3.client(
            "s3",
            endpoint_url=cfg.endpoint_url,
            aws_access_key_id=cfg.access_key_id,
            aws_secret_access_key=cfg.secret_access_key,
            region_name="auto",
            config=Config(
                signature_version="s3v4",
                connect_timeout=self.connect_timeout,
                read_timeout=self.read_timeout,
                retries={
                    "max_attempts": self.max_attempts,
                    "mode": "standard",
                },
                max_pool_connections=max(10, self.concurrency + 4),
                tcp_keepalive=True,
            ),
        )
        self.bucket = cfg.bucket
        self.prefix = cfg.prefix.strip("/")

    def key_for(self, relative: str) -> str:
        rel = relative.lstrip("/")
        return f"{self.prefix}/{rel}" if self.prefix else rel

    def upload_file(self, local: Path, relative: str) -> str:
        """Upload one local file. Used by tests and as the worker primitive."""
        return self._put_object(local, relative)

    def upload_frame(
        self,
        local_root: Path,
        frame_id: str,
        modes: Sequence[str],
        product_id: str = DEFAULT_PRODUCT_ID,
        include_manifest: bool = True,
    ) -> UploadStats:
        """Upload this cook's new frame + latest pointers + manifest.

        Does not re-walk retained historical frames onto the wire.
        Manifest is last so a failed cook leaves CDN on the previous frame.
        Other products (e.g. composite while cooking rala) are skipped.

        Pass include_manifest=False when the caller will merge manifest.json
        under a file lock and PUT it afterwards. That keeps a parallel cook
        from uploading a snapshot that dropped the other product.
        """
        groups, skipped = classify_radar_files(
            local_root, frame_id, modes, product_id=product_id
        )
        phases = [
            ("frame", groups["frame"]),
            ("root", groups["root"]),
            ("latest", groups["latest"]),
        ]
        if include_manifest:
            phases.append(("manifest", groups["manifest"]))
        total = sum(len(items) for _, items in phases)
        stats = UploadStats(started=total, skipped=skipped)
        t0 = time.monotonic()
        deadline = t0 + self.upload_timeout
        log.info(
            "R2 upload start objects=%d skipped=%d concurrency=%d "
            "connect_timeout=%.0fs read_timeout=%.0fs object_timeout=%.0fs "
            "total_timeout=%.0fs bucket=%s prefix=%s "
            "frame_id=%s frame=%d latest=%d root=%d manifest=%d",
            stats.started,
            skipped,
            self.concurrency,
            self.connect_timeout,
            self.read_timeout,
            self.object_timeout,
            self.upload_timeout,
            self.bucket,
            self.prefix,
            frame_id,
            len(groups["frame"]),
            len(groups["latest"]),
            len(groups["root"]),
            len(groups["manifest"]),
        )
        try:
            for name, items in phases:
                if not items:
                    continue
                log.debug("R2 upload phase=%s objects=%d", name, len(items))
                self._run_uploads(items, stats, deadline)
        except Exception:
            stats.duration_seconds = time.monotonic() - t0
            log.error("R2 upload failed %s", stats.as_log_fields())
            raise
        stats.duration_seconds = time.monotonic() - t0
        log.info("R2 upload complete %s", stats.as_log_fields())
        return stats

    def upload_manifest(self, local_root: Path) -> UploadStats:
        """PUT manifest.json only. Caller must hold the manifest lock."""
        path = local_root / "manifest.json"
        if not path.is_file():
            raise UploadError(f"manifest missing at {path}")
        t0 = time.monotonic()
        stats = UploadStats(started=1)
        key = self.upload_file(path, "manifest.json")
        stats.uploaded = 1
        stats.keys.append(key)
        stats.duration_seconds = time.monotonic() - t0
        log.info("R2 manifest upload complete %s", stats.as_log_fields())
        return stats

    def upload_tree(self, local_root: Path, relative_root: str = "") -> UploadStats:
        """Full-tree republish. Not used by the cooker; keep for manual rescue."""
        items: List[Tuple[Path, str]] = []
        for path in _iter_files(local_root):
            rel = path.relative_to(local_root).as_posix()
            remote = f"{relative_root.rstrip('/')}/{rel}" if relative_root else rel
            items.append((path, remote))
        log.warning(
            "R2 upload_tree republishing %d objects under %s (full retention walk)",
            len(items),
            local_root,
        )
        return self._upload_many(items, skipped=0, extra="mode=full-tree")

    def _upload_many(
        self,
        items: Sequence[Tuple[Path, str]],
        *,
        skipped: int = 0,
        extra: str = "",
    ) -> UploadStats:
        stats = UploadStats(started=len(items), skipped=skipped)
        t0 = time.monotonic()
        deadline = t0 + self.upload_timeout
        extra_bit = f" {extra}" if extra else ""
        log.info(
            "R2 upload start objects=%d skipped=%d concurrency=%d "
            "connect_timeout=%.0fs read_timeout=%.0fs object_timeout=%.0fs "
            "total_timeout=%.0fs bucket=%s prefix=%s%s",
            stats.started,
            skipped,
            self.concurrency,
            self.connect_timeout,
            self.read_timeout,
            self.object_timeout,
            self.upload_timeout,
            self.bucket,
            self.prefix,
            extra_bit,
        )
        try:
            if stats.started:
                self._run_uploads(items, stats, deadline)
        except Exception:
            stats.duration_seconds = time.monotonic() - t0
            log.error("R2 upload failed %s", stats.as_log_fields())
            raise
        stats.duration_seconds = time.monotonic() - t0
        log.info("R2 upload complete %s", stats.as_log_fields())
        return stats

    def _run_uploads(
        self,
        items: Sequence[Tuple[Path, str]],
        stats: UploadStats,
        deadline: float,
    ) -> None:
        workers = min(self.concurrency, len(items))
        pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="r2-put")
        future_map = {
            pool.submit(self._put_object, path, rel): (path, rel) for path, rel in items
        }
        pending = set(future_map)
        try:
            while pending:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    stats.failed += len(pending)
                    raise UploadTimeoutError(
                        f"R2 upload exceeded total timeout "
                        f"({self.upload_timeout:.0f}s) after {stats.uploaded} "
                        f"of {stats.started} objects"
                    )
                wait_for = min(remaining, self.object_timeout)
                done, pending = wait(
                    pending, timeout=wait_for, return_when=FIRST_COMPLETED
                )
                if not done:
                    stats.failed += len(pending)
                    raise UploadTimeoutError(
                        f"R2 upload stalled: no object finished within "
                        f"{wait_for:.0f}s (uploaded {stats.uploaded}/"
                        f"{stats.started})"
                    )
                for fut in done:
                    path, rel = future_map[fut]
                    try:
                        key = fut.result()
                    except Exception as exc:
                        stats.failed += 1 + len(pending)
                        stats.errors.append(f"{rel}: {exc}")
                        pending.clear()
                        raise UploadError(
                            f"R2 upload failed for {rel}: {exc}"
                        ) from exc
                    stats.uploaded += 1
                    stats.keys.append(key)
        finally:
            pool.shutdown(wait=False, cancel_futures=True)

    def _put_object(self, local: Path, relative: str) -> str:
        key = self.key_for(relative)
        extra = {
            "ContentType": _content_type(local),
            "CacheControl": _cache_control(key),
        }
        body = local.read_bytes()
        self.client.put_object(
            Bucket=self.bucket,
            Key=key,
            Body=body,
            ContentType=extra["ContentType"],
            CacheControl=extra["CacheControl"],
        )
        return key

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
