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
from datetime import datetime, timezone
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


class VerifyMismatch(UploadError):
    """ListObjectsV2 did not show every expected key at the expected size."""


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


def _common_dir(rels: Sequence[str]) -> str:
    """Directory shared by every relative key, excluding the file name."""
    dirs = []
    for rel in rels:
        parent = rel.rsplit("/", 1)[0] if "/" in rel else ""
        dirs.append(parent.split("/") if parent else [])
    if not dirs:
        return ""
    common: List[str] = []
    for tokens in zip(*dirs):
        first = tokens[0]
        if all(token == first for token in tokens):
            common.append(first)
        else:
            break
    return "/".join(common)


def _iter_files(root: Path) -> Iterable[Path]:
    if not root.exists():
        return
    yield from sorted(path for path in root.rglob("*") if path.is_file())


def classify_radar_files(
    local_root: Path,
    frame_id: str,
    modes: Sequence[str],
    product_id: str = DEFAULT_PRODUCT_ID,
    tile_prefix: Optional[str] = None,
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
        bucket = _upload_group(
            rel, frame_id, mode_set, product_id, tile_prefix=tile_prefix
        )
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
    tile_prefix: Optional[str] = None,
) -> Optional[str]:
    if rel == "manifest.json":
        return "manifest"
    spec = get_product(product_id)
    prefix = (spec.tile_prefix if tile_prefix is None else tile_prefix).strip("/")
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
        include_latest: bool = True,
        copy_latest: bool = False,
        tile_prefix: Optional[str] = None,
    ) -> UploadStats:
        """Upload this cook's new frame + latest pointers + manifest.

        Does not re-walk retained historical frames onto the wire.
        Manifest is last so a failed cook leaves CDN on the previous frame.
        Other products (e.g. composite while cooking rala) are skipped.

        Pass include_manifest=False when the caller will merge manifest.json
        under a file lock and PUT it afterwards. That keeps a parallel cook
        from uploading a snapshot that dropped the other product.

        Pass include_latest=False when this frame is older than the on-disk
        ``latest`` alias (RALA backfill). The historical frame is still
        uploaded; the live alias is left on the newer scan.

        Pass copy_latest=True to server-side copy frame PNGs onto ``latest``
        instead of uploading those bytes again. ``latest/frame.json`` is still
        PUT because its metadata differs. RALA uses this so a 2-minute scan
        is not stuck behind a second full PUT of the same tiles.
        """
        groups, skipped = classify_radar_files(
            local_root,
            frame_id,
            modes,
            product_id=product_id,
            tile_prefix=tile_prefix,
        )
        if not include_latest:
            skipped += len(groups["latest"])
            groups["latest"] = []
        copy_pairs: List[Tuple[str, str]] = []
        if copy_latest and groups["latest"]:
            kept: List[Tuple[Path, str]] = []
            needle = "/latest/"
            for path, rel in groups["latest"]:
                if needle in rel and rel.endswith(".png"):
                    copy_pairs.append((rel.replace(needle, f"/{frame_id}/", 1), rel))
                else:
                    kept.append((path, rel))
            groups["latest"] = kept
        phases = [
            ("frame", groups["frame"]),
            ("root", groups["root"]),
            ("latest", groups["latest"]),
        ]
        if include_manifest:
            phases.append(("manifest", groups["manifest"]))
        total = sum(len(items) for _, items in phases) + len(copy_pairs)
        stats = UploadStats(started=total, skipped=skipped)
        t0 = time.monotonic()
        deadline = t0 + self.upload_timeout
        log.info(
            "R2 upload start objects=%d skipped=%d concurrency=%d "
            "connect_timeout=%.0fs read_timeout=%.0fs object_timeout=%.0fs "
            "total_timeout=%.0fs bucket=%s prefix=%s "
            "frame_id=%s frame=%d latest=%d latest_copy=%d root=%d manifest=%d",
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
            len(copy_pairs),
            len(groups["root"]),
            len(groups["manifest"]),
        )
        try:
            for name, items in phases:
                if not items:
                    continue
                log.debug("R2 upload phase=%s objects=%d", name, len(items))
                self._run_uploads(items, stats, deadline)
            if copy_pairs:
                log.debug("R2 copy phase=latest objects=%d", len(copy_pairs))
                self._run_copies(copy_pairs, stats, deadline)
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

    def _run_copies(
        self,
        pairs: Sequence[Tuple[str, str]],
        stats: UploadStats,
        deadline: float,
    ) -> None:
        """Server-side copy frame tiles onto ``latest`` after those PUTs land."""
        workers = min(self.concurrency, len(pairs))
        pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="r2-copy")
        future_map = {
            pool.submit(self._copy_object, src, dest): (src, dest) for src, dest in pairs
        }
        pending = set(future_map)
        try:
            while pending:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    stats.failed += len(pending)
                    raise UploadTimeoutError(
                        f"R2 copy exceeded total timeout "
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
                        f"R2 copy stalled: no object finished within "
                        f"{wait_for:.0f}s (uploaded {stats.uploaded}/"
                        f"{stats.started})"
                    )
                for fut in done:
                    src, dest = future_map[fut]
                    try:
                        key = fut.result()
                    except Exception as exc:
                        stats.failed += 1 + len(pending)
                        stats.errors.append(f"{dest}: {exc}")
                        pending.clear()
                        raise UploadError(
                            f"R2 copy failed for {src} -> {dest}: {exc}"
                        ) from exc
                    stats.uploaded += 1
                    stats.keys.append(key)
        finally:
            pool.shutdown(wait=False, cancel_futures=True)

    def _copy_object(self, source_rel: str, dest_rel: str) -> str:
        dest = self.key_for(dest_rel)
        src = self.key_for(source_rel)
        self.client.copy_object(
            Bucket=self.bucket,
            Key=dest,
            CopySource={"Bucket": self.bucket, "Key": src},
            ContentType=_content_type(Path(dest_rel)),
            CacheControl=_cache_control(dest),
            MetadataDirective="REPLACE",
        )
        return dest

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

    def publish_frame_atomic(
        self,
        local_root: Path,
        frame_id: str,
        modes: Sequence[str],
        *,
        product_id: str = DEFAULT_PRODUCT_ID,
        include_latest: bool = True,
        copy_latest: bool = False,
        tile_prefix: Optional[str] = None,
        publish_latest_alias: bool = True,
        manifest_relative: str = "manifest.json",
        manifest_path: Optional[Path] = None,
        before_manifest=None,
        prune_stale_latest: bool = True,
        marks: Optional[dict] = None,
        previous_manifest: Optional[bytes] = None,
        lock_root: Optional[Path] = None,
    ) -> UploadStats:
        """Upload one frame in the gated order.

        PROCESS is the caller's. This method is:

        1. PUT every timestamped frame object (and the product colorbar).
        2. VERIFY ``…/<mode>/<frame_id>/`` with one ListObjectsV2 per page.
        3. Publish the manifest that names ``<frame_id>``.
        4. CopyObject or PUT the latest tiles (not ``frame.json``).
        5. VERIFY ``latest/``.
        6. PUT ``latest/frame.json`` last.
        7. List ``latest/`` and delete keys that are not in the new tile set.

        A client still showing the previous latest can 404 on a tile the new
        frame left empty. Those tiles are transparent. Treat HTTP 404 on a
        latest tile as a transparent pixel, the same as an empty PNG.

        ``MPWG_PUBLISH_LATEST_ALIAS=0`` skips steps 4–7. The manifest's
        ``latest`` template then points at the timestamped tip.

        File bodies are streamed from disk. Tile bytes are not retained on
        this object after the call returns.

        On a frame PUT or frame VERIFY failure the manifest is not PUT and
        latest is not updated. On a later latest failure, ``latest/frame.json``
        is not PUT and, when ``previous_manifest`` was supplied, that body is
        PUT back so the published manifest stays the previous frame.
        """
        groups, skipped = classify_radar_files(
            local_root,
            frame_id,
            modes,
            product_id=product_id,
            tile_prefix=tile_prefix,
        )
        do_latest = bool(include_latest and publish_latest_alias and groups["latest"])
        latest_json: List[Tuple[Path, str]] = []
        latest_tiles: List[Tuple[Path, str]] = []
        if do_latest:
            for path, rel in groups["latest"]:
                if rel.endswith("/frame.json") or rel.endswith("frame.json"):
                    latest_json.append((path, rel))
                else:
                    latest_tiles.append((path, rel))
        copy_pairs: List[Tuple[str, str]] = []
        latest_puts: List[Tuple[Path, str]] = []
        if do_latest and copy_latest:
            needle = "/latest/"
            for path, rel in latest_tiles:
                if needle in rel and rel.endswith(".png"):
                    copy_pairs.append((rel.replace(needle, f"/{frame_id}/", 1), rel))
                else:
                    latest_puts.append((path, rel))
        elif do_latest:
            latest_puts = list(latest_tiles)

        frame_items = list(groups["frame"])
        root_items = list(groups["root"])
        manifest_file = manifest_path or (local_root / manifest_relative)
        total = len(frame_items) + len(root_items) + 1 + len(latest_puts) + len(copy_pairs)
        total += len(latest_json)
        stats = UploadStats(started=total, skipped=skipped)
        t0 = time.monotonic()
        deadline = t0 + self.upload_timeout
        manifest_sent = False
        local_manifest_written = False
        log.info(
            "R2 atomic upload start frame_id=%s objects=%d frame=%d root=%d "
            "latest_tiles=%d latest_copy=%d latest_json=%d alias=%s",
            frame_id,
            stats.started,
            len(frame_items),
            len(root_items),
            len(latest_puts),
            len(copy_pairs),
            len(latest_json) if do_latest else 0,
            int(bool(do_latest)),
        )
        try:
            if frame_items:
                self._run_stream_uploads(frame_items, stats, deadline)
            if root_items:
                self._run_stream_uploads(root_items, stats, deadline)
            self._stamp(marks, "frame_put_done")
            self._verify_objects(frame_items)
            self._stamp(marks, "verify_done")
            def _commit_manifest() -> None:
                nonlocal local_manifest_written, manifest_sent
                if before_manifest is not None:
                    before_manifest()
                    local_manifest_written = True
                if not manifest_file.is_file():
                    raise UploadError(f"manifest missing at {manifest_file}")
                key = self._put_object_stream(manifest_file, manifest_relative)
                stats.uploaded += 1
                stats.keys.append(key)
                manifest_sent = True
                self._stamp(marks, "manifest_put")

            if lock_root is None:
                _commit_manifest()
            else:
                from mpwg_radar.cooker import manifest_lock

                with manifest_lock(lock_root):
                    _commit_manifest()
            if do_latest:
                if latest_puts:
                    self._run_stream_uploads(latest_puts, stats, deadline)
                if copy_pairs:
                    self._run_copies(copy_pairs, stats, deadline)
                if latest_puts:
                    self._verify_objects(latest_puts)
                if copy_pairs:
                    self._verify_copies(copy_pairs)
                if latest_json:
                    self._run_stream_uploads(latest_json, stats, deadline)
                self._stamp(marks, "latest_done")
                if prune_stale_latest:
                    keep = [rel for _, rel in latest_puts]
                    keep.extend(dest for _, dest in copy_pairs)
                    keep.extend(rel for _, rel in latest_json)
                    self._delete_stale_latest(keep)
            else:
                self._stamp(marks, "latest_done")
        except Exception:
            stats.duration_seconds = time.monotonic() - t0
            log.error("R2 atomic upload failed %s", stats.as_log_fields())
            if previous_manifest is not None and (local_manifest_written or manifest_sent):
                self._restore_manifest(
                    manifest_file,
                    manifest_relative,
                    previous_manifest,
                    stats,
                    reput=manifest_sent,
                )
            raise
        stats.duration_seconds = time.monotonic() - t0
        log.info("R2 atomic upload complete %s", stats.as_log_fields())
        return stats

    def upload_relative(self, local: Path, relative: str) -> UploadStats:
        """PUT one object at ``relative`` (not forced to ``manifest.json``)."""
        t0 = time.monotonic()
        stats = UploadStats(started=1)
        key = self._put_object_stream(local, relative)
        stats.uploaded = 1
        stats.keys.append(key)
        stats.duration_seconds = time.monotonic() - t0
        return stats

    def list_object_sizes(self, relative_prefix: str) -> Dict[str, int]:
        """ListObjectsV2 sizes under ``relative_prefix``. One call per 1000 keys."""
        prefix = self.key_for(relative_prefix.rstrip("/") + "/")
        found: Dict[str, int] = {}
        token: Optional[str] = None
        while True:
            kwargs = {"Bucket": self.bucket, "Prefix": prefix}
            if token:
                kwargs["ContinuationToken"] = token
            page = self.client.list_objects_v2(**kwargs)
            for obj in page.get("Contents") or []:
                found[str(obj["Key"])] = int(obj["Size"])
            if not page.get("IsTruncated"):
                break
            token = page.get("NextContinuationToken")
            if not token:
                break
        return found

    def _verify_objects(self, items: Sequence[Tuple[Path, str]]) -> None:
        if not items:
            return
        found = self.list_object_sizes(_common_dir([rel for _, rel in items]))
        problems: List[str] = []
        for path, rel in items:
            key = self.key_for(rel)
            listed = found.get(key)
            local_size = path.stat().st_size
            if listed is None or listed != local_size:
                problems.append(f"{key} listed={listed} local={local_size}")
        if problems:
            raise VerifyMismatch(
                "R2 verify mismatch (" + "; ".join(problems[:6]) + ")"
            )

    def _verify_copies(self, copies: Sequence[Tuple[str, str]]) -> None:
        """Confirm each copied latest key is listed with the source size."""
        if not copies:
            return
        found = self.list_object_sizes(_common_dir([dest for _, dest in copies]))
        sources = self.list_object_sizes(_common_dir([src for src, _ in copies]))
        problems: List[str] = []
        for src_rel, dest_rel in copies:
            dest = self.key_for(dest_rel)
            src = self.key_for(src_rel)
            listed = found.get(dest)
            expect = sources.get(src)
            if listed is None or expect is None or listed != expect:
                problems.append(f"{dest} listed={listed} source={expect}")
        if problems:
            raise VerifyMismatch(
                "R2 latest verify mismatch (" + "; ".join(problems[:6]) + ")"
            )

    def _delete_stale_latest(self, keep_rels: Sequence[str]) -> int:
        """Delete latest keys that are not in the new frame's tile set.

        Deletes are free on R2. A removed tile was empty in this frame; a
        client that still holds the previous latest must treat 404 as
        transparent.
        """
        if not keep_rels:
            return 0
        prefix = _common_dir(keep_rels)
        found = self.list_object_sizes(prefix)
        keep = {self.key_for(rel) for rel in keep_rels}
        stale = sorted(key for key in found if key not in keep)
        deleted = 0
        for start in range(0, len(stale), 1000):
            batch = stale[start : start + 1000]
            self.client.delete_objects(
                Bucket=self.bucket,
                Delete={
                    "Objects": [{"Key": key} for key in batch],
                    "Quiet": True,
                },
            )
            deleted += len(batch)
        if deleted:
            log.info(
                "Pruned %d stale latest objects under %s (404 means transparent)",
                deleted,
                prefix,
            )
        return deleted

    def _restore_manifest(
        self,
        manifest_file: Path,
        relative: str,
        previous: bytes,
        stats: UploadStats,
        *,
        reput: bool,
    ) -> None:
        """Put the previous manifest body back on disk and, if it was sent, on R2."""
        log.error("Publish failed after the manifest was staged. Restoring the previous manifest.")
        manifest_file.parent.mkdir(parents=True, exist_ok=True)
        manifest_file.write_bytes(previous)
        if not reput:
            return
        try:
            key = self._put_bytes(previous, relative, manifest_file)
        except Exception:
            log.error("Could not restore the previous manifest at %s", relative)
            raise
        stats.keys.append(key)
        stats.uploaded += 1

    def _put_bytes(self, body: bytes, relative: str, content_path: Path) -> str:
        key = self.key_for(relative)
        self.client.put_object(
            Bucket=self.bucket,
            Key=key,
            Body=body,
            ContentType=_content_type(content_path),
            CacheControl=_cache_control(key),
        )
        return key

    def _put_object_stream(self, local: Path, relative: str) -> str:
        """PUT one file without retaining its bytes after the call."""
        key = self.key_for(relative)
        size = local.stat().st_size
        with local.open("rb") as body:
            self.client.put_object(
                Bucket=self.bucket,
                Key=key,
                Body=body,
                ContentLength=size,
                ContentType=_content_type(local),
                CacheControl=_cache_control(key),
            )
        return key

    def _run_stream_uploads(
        self,
        items: Sequence[Tuple[Path, str]],
        stats: UploadStats,
        deadline: float,
    ) -> None:
        workers = min(self.concurrency, len(items))
        pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="r2-put")
        future_map = {
            pool.submit(self._put_object_stream, path, rel): (path, rel)
            for path, rel in items
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

    @staticmethod
    def _stamp(marks: Optional[dict], key: str) -> None:
        if marks is None:
            return
        marks[key] = datetime.now(timezone.utc).isoformat()

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
