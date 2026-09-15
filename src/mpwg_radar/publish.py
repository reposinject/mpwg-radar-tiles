"""Publish cooked tiles + frame/manifest JSON to Cloudflare R2 (S3 API).

CONUS z6–8 can leave hundreds of MB of retained frames on local disk. This
module uploads the new frame, `latest/` pointers, sidecars, and `manifest.json`
by default — not the whole retention tree. boto3 `upload_file` / TransferManager
is avoided in favor of `put_object` with botocore connect/read timeouts so a
stuck PUT cannot hang the systemd oneshot indefinitely.
"""

from __future__ import annotations

import logging
import mimetypes
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from queue import Empty, Queue
from threading import Lock
from typing import Iterable, List, Optional, Sequence, Tuple

from mpwg_radar.config import R2Config

log = logging.getLogger(__name__)

UploadItem = Tuple[Path, str]

# Immutable frame tiles can be cached; latest/manifest stay short-lived.
CACHE_FRAME = "public, max-age=120, s-maxage=120"
CACHE_LATEST = "public, max-age=30, s-maxage=30"
CACHE_MANIFEST = "public, max-age=15, s-maxage=15"


class R2UploadTimeout(TimeoutError):
    """Cook upload phase exceeded MPWG_UPLOAD_TIMEOUT_SECONDS."""


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


def _files_under(directory: Path) -> List[Path]:
    if not directory.is_dir():
        return []
    return sorted(path for path in directory.rglob("*") if path.is_file())


def _items_under(directory: Path, radar_root: Path) -> List[UploadItem]:
    items: List[UploadItem] = []
    for path in _files_under(directory):
        items.append((path, path.relative_to(radar_root).as_posix()))
    return items


@dataclass(frozen=True)
class CookUploadPlan:
    """Files to push this cook, staged so `manifest.json` goes last."""

    frame_files: List[UploadItem] = field(default_factory=list)
    latest_files: List[UploadItem] = field(default_factory=list)
    sidecar_files: List[UploadItem] = field(default_factory=list)
    manifest_files: List[UploadItem] = field(default_factory=list)

    def batches(self) -> List[Tuple[str, List[UploadItem]]]:
        ordered = [
            ("frame", self.frame_files),
            ("latest", self.latest_files),
            ("sidecar", self.sidecar_files),
            ("manifest", self.manifest_files),
        ]
        return [(name, items) for name, items in ordered if items]

    def all_files(self) -> List[UploadItem]:
        return (
            list(self.frame_files)
            + list(self.latest_files)
            + list(self.sidecar_files)
            + list(self.manifest_files)
        )

    def __len__(self) -> int:
        return len(self.all_files())


def collect_cook_uploads(
    radar_root: Path,
    *,
    frame_id: str,
    modes: Sequence[str],
    all_frames: bool = False,
) -> CookUploadPlan:
    """Choose local radar files to upload.

    Default (`all_frames=False`): new `{mode}/{frame_id}/` tree, `{mode}/latest/`,
    `colorbar.png`, and `manifest.json`. Older retained frames stay on disk for
    the local animation window and are not re-uploaded.

    `all_frames=True` walks the whole `radar_root` (legacy / repair path).
    """
    radar_root = Path(radar_root)
    if all_frames:
        everything = _items_under(radar_root, radar_root)
        manifest = [item for item in everything if item[1].endswith("manifest.json")]
        rest = [item for item in everything if not item[1].endswith("manifest.json")]
        return CookUploadPlan(frame_files=rest, manifest_files=manifest)

    frame_files: List[UploadItem] = []
    latest_files: List[UploadItem] = []
    for mode in modes:
        mode = mode.strip()
        if not mode:
            continue
        frame_files.extend(_items_under(radar_root / mode / frame_id, radar_root))
        latest_files.extend(_items_under(radar_root / mode / "latest", radar_root))

    sidecar_files: List[UploadItem] = []
    colorbar = radar_root / "colorbar.png"
    if colorbar.is_file():
        sidecar_files.append((colorbar, "colorbar.png"))

    manifest_files: List[UploadItem] = []
    manifest = radar_root / "manifest.json"
    if manifest.is_file():
        manifest_files.append((manifest, "manifest.json"))

    return CookUploadPlan(
        frame_files=frame_files,
        latest_files=latest_files,
        sidecar_files=sidecar_files,
        manifest_files=manifest_files,
    )


class R2Publisher:
    def __init__(
        self,
        cfg: R2Config,
        *,
        workers: Optional[int] = None,
        timeout_seconds: Optional[float] = None,
    ):
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
        self.workers = max(1, int(workers if workers is not None else cfg.upload_workers))
        self.timeout_seconds = float(
            timeout_seconds if timeout_seconds is not None else cfg.upload_timeout_seconds
        )
        if self.timeout_seconds <= 0:
            raise ValueError("upload timeout_seconds must be > 0")
        pool = max(10, self.workers + 2)
        self.client = boto3.client(
            "s3",
            endpoint_url=cfg.endpoint_url,
            aws_access_key_id=cfg.access_key_id,
            aws_secret_access_key=cfg.secret_access_key,
            region_name="auto",
            config=Config(
                signature_version="s3v4",
                connect_timeout=cfg.connect_timeout,
                read_timeout=cfg.read_timeout,
                retries={"max_attempts": cfg.max_attempts, "mode": "standard"},
                max_pool_connections=pool,
                tcp_keepalive=True,
            ),
        )
        self.bucket = cfg.bucket
        self.prefix = cfg.prefix.strip("/")

    def key_for(self, relative: str) -> str:
        rel = relative.lstrip("/")
        return f"{self.prefix}/{rel}" if self.prefix else rel

    def upload_file(self, local: Path, relative: str) -> str:
        """PUT one object. Small PNG/JSON tiles use put_object, not TransferManager."""
        key = self.key_for(relative)
        extra = {
            "ContentType": _content_type(local),
            "CacheControl": _cache_control(key),
        }
        size = local.stat().st_size
        with local.open("rb") as body:
            self.client.put_object(
                Bucket=self.bucket,
                Key=key,
                Body=body,
                ContentLength=size,
                ContentType=extra["ContentType"],
                CacheControl=extra["CacheControl"],
            )
        return key

    def upload_files(self, items: Iterable[UploadItem], *, deadline: Optional[float] = None) -> List[str]:
        """Upload ``(local_path, relative_key)`` pairs with a worker pool and deadline."""
        pending = [(Path(local), str(relative)) for local, relative in items]
        if not pending:
            return []
        if deadline is None:
            deadline = time.monotonic() + self.timeout_seconds
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise R2UploadTimeout(
                f"R2 upload timed out before starting a batch of {len(pending)} objects"
            )
        workers = min(self.workers, len(pending))
        log.info(
            "R2 upload batch objects=%d workers=%d timeout_remaining=%.1fs",
            len(pending),
            workers,
            remaining,
        )
        if workers == 1:
            keys: List[str] = []
            for local, relative in pending:
                if time.monotonic() >= deadline:
                    raise R2UploadTimeout(
                        f"R2 upload timed out after {self.timeout_seconds:.0f}s "
                        f"({len(keys)}/{len(pending)} objects in this batch)"
                    )
                keys.append(self.upload_file(local, relative))
            return keys
        return self._upload_concurrent(pending, workers=workers, deadline=deadline)

    def _upload_concurrent(
        self,
        pending: List[UploadItem],
        *,
        workers: int,
        deadline: float,
    ) -> List[str]:
        keys: List[str] = []
        errors: List[BaseException] = []
        done_lock = Lock()
        done_count = 0
        total = len(pending)
        work: Queue[UploadItem] = Queue()
        for item in pending:
            work.put(item)
        stop = threading.Event()

        def worker() -> None:
            nonlocal done_count
            while not stop.is_set():
                try:
                    item = work.get_nowait()
                except Empty:
                    return
                try:
                    if time.monotonic() >= deadline:
                        raise R2UploadTimeout(
                            f"R2 upload timed out after {self.timeout_seconds:.0f}s"
                        )
                    key = self.upload_file(item[0], item[1])
                    with done_lock:
                        keys.append(key)
                        done_count += 1
                        if done_count == total or done_count % 100 == 0:
                            log.info("R2 upload progress %d/%d", done_count, total)
                except BaseException as exc:
                    with done_lock:
                        errors.append(exc)
                    stop.set()
                    return

        threads = [
            threading.Thread(target=worker, name=f"r2-upload-{i}", daemon=True)
            for i in range(workers)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            remaining = deadline - time.monotonic()
            thread.join(timeout=max(0.0, remaining))
        stop.set()
        if errors:
            raise errors[0]
        if len(keys) < total:
            raise R2UploadTimeout(
                f"R2 upload timed out after {self.timeout_seconds:.0f}s "
                f"({len(keys)}/{total} objects completed in this batch)"
            )
        return keys

    def upload_tree(self, local_root: Path, relative_root: str = "") -> List[str]:
        """Upload every file under ``local_root`` (legacy full-tree path)."""
        items: List[UploadItem] = []
        local_root = Path(local_root)
        for path in _files_under(local_root):
            rel = path.relative_to(local_root).as_posix()
            remote = f"{relative_root.rstrip('/')}/{rel}" if relative_root else rel
            items.append((path, remote))
        keys = self.upload_files(items)
        log.info("Uploaded %d objects to r2://%s/%s", len(keys), self.bucket, self.prefix)
        return keys

    def upload_cook(
        self,
        radar_root: Path,
        *,
        frame_id: str,
        modes: Sequence[str],
        all_frames: bool = False,
    ) -> List[str]:
        """Upload one cook: new frame + latest + sidecars + manifest (unless all_frames)."""
        plan = collect_cook_uploads(
            radar_root, frame_id=frame_id, modes=modes, all_frames=all_frames
        )
        items = plan.all_files()
        nbytes = sum(path.stat().st_size for path, _ in items if path.is_file())
        skipped = 0
        if not all_frames:
            skipped = max(0, len(_files_under(Path(radar_root))) - len(items))
        log.info(
            "R2 upload scope=%s frame=%s modes=%s objects=%d bytes=%d skipped_retained=%d "
            "workers=%d timeout=%ss (set MPWG_UPLOAD_ALL_FRAMES=true to re-upload history)",
            "all-frames" if all_frames else "new-frame",
            frame_id,
            list(modes),
            len(items),
            nbytes,
            skipped,
            self.workers,
            int(self.timeout_seconds),
        )
        deadline = time.monotonic() + self.timeout_seconds
        keys: List[str] = []
        for batch_name, batch in plan.batches():
            remaining = deadline - time.monotonic()
            log.info(
                "R2 upload stage=%s objects=%d remaining=%.1fs",
                batch_name,
                len(batch),
                remaining,
            )
            keys.extend(self.upload_files(batch, deadline=deadline))
        elapsed = time.monotonic() - (deadline - self.timeout_seconds)
        log.info(
            "Uploaded %d objects to r2://%s/%s in %.1fs",
            len(keys),
            self.bucket,
            self.prefix,
            elapsed,
        )
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
