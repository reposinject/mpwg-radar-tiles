"""Opt-in RALA pipeline overlap and atomic publication.

Unset flags leave ``cook()`` and ``upload_frame`` on the production path.
This module is imported only when ``MPWG_PIPELINE``, ``MPWG_ATOMIC_PUBLISH``,
or ``MPWG_REVIEW_MANIFEST_KEY`` is set.

Publish order (gated):

    PROCESS → UPLOAD ALL → VERIFY → MANIFEST → LATEST TILES → VERIFY LATEST
    → latest/frame.json → delete stale latest keys

``MPWG_PUBLISH_LATEST_ALIAS=0`` skips the latest alias. The manifest ``latest``
template is then the tip frame's timestamped prefix.

A client that still shows the previous ``latest/`` can 404 on a tile the new
frame did not write. That tile is empty. Treat 404 as transparent.

Remote prefixes are deleted only after a manifest that no longer lists them
has been published, and only after a grace of at least 10 minutes. The pending
deletes live in ``radar/.publish-state.json`` so a restart does not delete
early and does not forget a prefix that was already unlisted.

A timestamp that publishes successfully is removed from that list. Delete time
re-reads the current review or root manifest under ``manifest_lock`` and will
not remove a prefix that manifest still names. On the review path, catch-up
does not cook a scan that is already outside the retention window, and a
``clean/`` LIST deletes untracked objects older than the window plus grace.
Prefixes containing ``_opt_validate`` or ``_atomic_test``, and anything
outside the configured review prefix, are left alone. Flags unset: none of
this runs.
"""

from __future__ import annotations

import gc
import json
import logging
import os
import queue
import re
import resource
import shutil
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Set

import numpy as np

from mpwg_radar.config import CookerConfig
from mpwg_radar.grib import ReflectivityFrame
from mpwg_radar.palette import Palette, load_palette
from mpwg_radar.products import ProductSpec
from mpwg_radar.publish import R2Publisher, UploadStats
from mpwg_radar.qc import apply_mode
from mpwg_radar.tiles import SPATIAL_REVISION, write_colorbar

log = logging.getLogger(__name__)

# Clients mid-loop must not lose a frame they already fetched.
GRACE_FLOOR_SECONDS = 600.0
STATE_NAME = ".publish-state.json"
PIPELINE_WORK = "pipeline-work"
# Throwaway proof prefixes from the review box. Never reconcile-delete these.
_RECONCILE_EXCLUDED = ("_opt_validate", "_atomic_test")
_FRAME_DIR_RE = re.compile(r"^\d{8}T\d{6}Z$")


def effective_grace(cfg: CookerConfig) -> float:
    """Grace before a remote prefix delete. Never under 10 minutes."""
    return max(GRACE_FLOOR_SECONDS, float(cfg.retention_grace_seconds))


def state_path(radar_root: Path) -> Path:
    return radar_root / STATE_NAME


def empty_state() -> Dict:
    return {
        "version": 1,
        "latest_frame": None,
        "verified": [],
        "pending_deletes": [],
    }


def load_publish_state(path: Path) -> Dict:
    if not path.is_file():
        return empty_state()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return empty_state()
    if not isinstance(data, dict):
        return empty_state()
    data.setdefault("version", 1)
    data.setdefault("latest_frame", None)
    data.setdefault("verified", [])
    data.setdefault("pending_deletes", [])
    return data


def save_publish_state(path: Path, state: Dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")


def note_unlisted(
    state: Dict,
    prefixes: Sequence[str],
    now: datetime,
    grace_seconds: float,
) -> None:
    """Remember prefixes to delete later. An existing entry keeps its clock.

    Restarting must not push ``delete_after`` forward (that would leak) and
    must not move it backward (that would delete inside the grace).
    """
    pending = state.setdefault("pending_deletes", [])
    known = {item.get("prefix") for item in pending}
    moment = now.astimezone(timezone.utc)
    for prefix in prefixes:
        text = str(prefix).strip().strip("/")
        if not text or text in known:
            continue
        pending.append(
            {
                "prefix": text,
                "unlisted_at": moment.isoformat(),
                "delete_after": (moment + timedelta(seconds=grace_seconds)).isoformat(),
            }
        )
        known.add(text)


def due_deletes(state: Dict, now: datetime) -> List[str]:
    moment = now.astimezone(timezone.utc)
    due: List[str] = []
    for item in state.get("pending_deletes") or []:
        when = _parse_time(item.get("delete_after"))
        if when is not None and moment >= when:
            prefix = str(item.get("prefix") or "").strip().strip("/")
            if prefix:
                due.append(prefix)
    return due


def drop_deleted(state: Dict, prefix: str) -> None:
    state["pending_deletes"] = [
        item
        for item in (state.get("pending_deletes") or [])
        if item.get("prefix") != prefix
    ]


def sweep_remote_deletes(
    publisher: R2Publisher,
    state: Dict,
    now: datetime,
    *,
    skip: Optional[Callable[[str], str]] = None,
) -> List[str]:
    """Delete prefixes whose grace has elapsed. A failure stays pending.

    ``skip`` returns ``listed``, ``excluded``, or ``unreadable``. Listed and
    excluded prefixes are not deleted. An unreadable manifest skips the
    delete and leaves the pending entry for the next pass. ``skip`` is None
    for the state-file-only helper used when no manifest is in play.
    """
    deleted: List[str] = []
    for prefix in due_deletes(state, now):
        if skip is not None:
            reason = skip(prefix)
            if reason == "listed":
                log.info("Retention skip %s; current manifest lists it", prefix)
                drop_deleted(state, prefix)
                continue
            if reason == "excluded":
                log.info("Retention skip excluded prefix %s", prefix)
                drop_deleted(state, prefix)
                continue
            if reason == "unreadable":
                log.error(
                    "Retention skip %s; manifest could not be re-read", prefix
                )
                continue
        try:
            publisher.delete_prefix(prefix)
        except Exception as exc:  # noqa: BLE001 — keep the entry and retry
            log.error("Remote retention delete failed for %s (%s)", prefix, exc)
            continue
        drop_deleted(state, prefix)
        deleted.append(prefix)
    return deleted


def memory_snapshot() -> Dict:
    """RSS high-water plus cgroup ``memory.current`` when the kernel exposes it."""
    rss_kb = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    hwm = None
    vmrss = None
    try:
        for line in Path("/proc/self/status").read_text(encoding="utf-8").splitlines():
            if line.startswith("VmHWM:"):
                hwm = int(line.split()[1]) * 1024
            elif line.startswith("VmRSS:"):
                vmrss = int(line.split()[1]) * 1024
    except OSError:
        pass
    return {
        "rss_high_water_bytes": rss_kb * 1024,
        "vm_hwm_bytes": hwm,
        "vm_rss_bytes": vmrss,
        "cgroup_current_bytes": _cgroup_memory_current(),
    }


def log_memory(stage: str, edge: str, frame_id: str) -> None:
    snap = memory_snapshot()
    snap.update({"stage": stage, "edge": edge, "frame_id": frame_id})
    log.info("memory_sample %s", json.dumps(snap, sort_keys=True))


def release_frame_arrays(frame: Optional[ReflectivityFrame]) -> None:
    """Drop dBZ / category / axis buffers before the next frame is prepared."""
    if frame is None:
        return
    frame.dbz = np.array([], dtype=np.float32)
    frame.category = None
    frame.native_fill = None
    frame.lat = np.array([], dtype=np.float64)
    frame.lon = np.array([], dtype=np.float64)


def _cgroup_memory_current() -> Optional[int]:
    candidates = [
        Path("/sys/fs/cgroup/memory.current"),
        Path("/sys/fs/cgroup/memory/memory.usage_in_bytes"),
    ]
    try:
        for line in Path("/proc/self/cgroup").read_text(encoding="utf-8").splitlines():
            parts = line.split(":")
            if len(parts) < 3:
                continue
            if parts[1] not in {"", "memory"}:
                continue
            rel = parts[2].strip("/")
            if not rel:
                continue
            candidates.append(Path("/sys/fs/cgroup") / rel / "memory.current")
            candidates.append(
                Path("/sys/fs/cgroup/memory") / rel / "memory.usage_in_bytes"
            )
    except OSError:
        pass
    for path in candidates:
        try:
            text = path.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if not text or text.lower() == "max":
            continue
        try:
            return int(text)
        except ValueError:
            continue
    return None


def _parse_time(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    text = str(value).strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _restore_production_manifest(path: Path, previous: Optional[bytes]) -> None:
    """A review publish must not leave a rewritten production manifest behind."""
    if previous is None:
        return
    if path.is_file() and path.read_bytes() != previous:
        path.write_bytes(previous)


def frame_prefix(product: ProductSpec, mode: str, frame_id: str) -> str:
    return product.tile_url_template(mode, frame_id).split("/{z}")[0].rstrip("/")


def review_scope(cfg: CookerConfig) -> str:
    """Directory of ``MPWG_REVIEW_MANIFEST_KEY``, or empty when unset.

    Remote reconcile deletes only under this prefix. The production tree is
    outside it and is not listed.
    """
    key = (cfg.review_manifest_key or "").strip().strip("/")
    if not key or "/" not in key:
        return ""
    return key.rsplit("/", 1)[0]


def prefix_blocked(prefix: str, scope: str) -> bool:
    """True for proof prefixes and for anything outside the review tree."""
    text = str(prefix or "").strip().strip("/")
    if not text:
        return True
    if any(marker in text for marker in _RECONCILE_EXCLUDED):
        return True
    root = (scope or "").strip().strip("/")
    if root and text != root and not text.startswith(root + "/"):
        return True
    return False


def _strip_publisher_prefix(publisher: R2Publisher, key: str) -> str:
    root = str(getattr(publisher, "prefix", "") or "").strip("/")
    if root and (key == root or key.startswith(root + "/")):
        return key[len(root) :].lstrip("/")
    return key


def _frame_dir_age(segment: str, now: datetime) -> Optional[float]:
    if not _FRAME_DIR_RE.fullmatch(segment):
        return None
    when = datetime.strptime(segment, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    return (now.astimezone(timezone.utc) - when).total_seconds()


def scan_outside_retention(valid_time: datetime, policy, now: datetime) -> bool:
    """True when a scan would already be dropped by the age window.

    Count-only retention (composite) has no age window. The same bounds as
    ``apply_retention`` apply: older than ``max_age_seconds``, or more than
    10 minutes in the future.
    """
    if policy.max_age_seconds is None:
        return False
    age = (
        now.astimezone(timezone.utc) - valid_time.astimezone(timezone.utc)
    ).total_seconds()
    return age > float(policy.max_age_seconds) or age < -600


def _listed_prefixes_from_disk(
    cfg: CookerConfig, product: ProductSpec, radar_root: Path
) -> Optional[Set[str]]:
    """Frame prefixes named by the current review or root manifest.

    ``None`` means the file could not be read. Callers must not delete.
    """
    try:
        if cfg.review_manifest_key:
            path = radar_root / cfg.review_manifest_key.lstrip("/")
            if not path.is_file():
                return set()
            document = json.loads(path.read_text(encoding="utf-8"))
            ids = {
                str(item["id"])
                for item in (document.get("frames") or [])
                if isinstance(item, dict) and item.get("id")
            }
            tip = document.get("latest_frame")
            if tip:
                ids.add(str(tip))
        else:
            path = radar_root / "manifest.json"
            if not path.is_file():
                return set()
            document = json.loads(path.read_text(encoding="utf-8"))
            ids = set()
            entry = (document.get("products") or {}).get(product.id) or {}
            for mode in cfg.modes:
                ids |= _manifest_frame_ids(document, product.id, mode)
                mode_entry = (entry.get("modes") or {}).get(mode) or {}
                tip = mode_entry.get("latest_frame") or entry.get("latest_frame")
                if tip:
                    ids.add(str(tip))
    except (OSError, json.JSONDecodeError, UnicodeError, TypeError) as exc:
        log.error("Could not re-read the manifest for retention (%s)", exc)
        return None
    return {
        frame_prefix(product, mode, frame_id)
        for frame_id in ids
        for mode in cfg.modes
    }


def protected_prefixes(
    cfg: CookerConfig, product: ProductSpec, radar_root: Path
) -> Optional[Set[str]]:
    """Re-read the live manifest while holding ``manifest_lock``."""
    from mpwg_radar.cooker import manifest_lock

    with manifest_lock(radar_root):
        return _listed_prefixes_from_disk(cfg, product, radar_root)


def _retention_skip(
    cfg: CookerConfig, product: ProductSpec, radar_root: Path
) -> Callable[[str], str]:
    """Decide one prefix immediately before it would be deleted."""
    scope = review_scope(cfg)

    def skip(prefix: str) -> str:
        if prefix_blocked(prefix, scope):
            return "excluded"
        protected = protected_prefixes(cfg, product, radar_root)
        if protected is None:
            return "unreadable"
        if prefix in protected:
            return "listed"
        return ""

    return skip


@dataclass
class FrameJob:
    frame_id: str
    valid_time: datetime
    private_root: Path
    mode_summaries: List[Dict]
    marks: Dict = field(default_factory=dict)
    source: str = "NOAA MRMS"
    product_name: str = ""
    palette_id: str = ""


class PublisherQueue:
    """One publisher thread and a queue of maxsize 1.

    The slot stays logically occupied until ``publish`` returns. The main
    thread can process the next frame during that publish, then blocks in
    ``submit`` until the publish finishes. That is two frames in flight:
    one processing, one publishing. A third frame is not started.
    """

    def __init__(self, handler):
        self.queue: queue.Queue = queue.Queue(maxsize=1)
        self.maxsize = self.queue.maxsize
        self._idle = threading.Event()
        self._idle.set()
        self._handler = handler
        self.results: List[Dict] = []
        self._thread = threading.Thread(
            target=self._loop, name="rala-publisher", daemon=False
        )

    def start(self) -> None:
        self._thread.start()

    def submit(self, job: FrameJob) -> None:
        waited = time.monotonic()
        self._idle.wait()
        job.marks["queue_wait_seconds"] = round(time.monotonic() - waited, 3)
        self._idle.clear()
        job.marks["enqueue"] = _iso(_now())
        self.queue.put(job)

    def close(self) -> None:
        self._idle.wait()
        self.queue.put(None)
        self._thread.join()

    def _loop(self) -> None:
        while True:
            job = self.queue.get()
            try:
                if job is None:
                    return
                self.results.append(self._handler(job))
            except Exception as exc:  # noqa: BLE001 — one frame must not stop the thread
                log.error(
                    "Publisher frame %s failed (%s); continuing",
                    getattr(job, "frame_id", "?"),
                    exc,
                )
                self.results.append(
                    {
                        "ok": False,
                        "frame_id": getattr(job, "frame_id", None),
                        "valid_time": _iso(job.valid_time) if job is not None else "",
                        "error": str(exc),
                    }
                )
            finally:
                self._idle.set()


def _pending_path(cfg: CookerConfig, product: ProductSpec, frame_id: str) -> Path:
    safe = product.tile_prefix.replace("/", "-") if product.tile_prefix else product.id
    return cfg.output_dir / f".pending-{safe}-{frame_id}.json"


def _write_pending(cfg: CookerConfig, product: ProductSpec, frame_id: str) -> None:
    """Status beside the output dir. This is not ``manifest.json``."""
    path = _pending_path(cfg, product, frame_id)
    path.write_text(
        json.dumps(
            {
                "frame_id": frame_id,
                "state": "publishing",
                "product": product.id,
                "tile_prefix": product.tile_prefix,
                "noted_at": _iso(_now()),
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def _clear_pending(cfg: CookerConfig, product: ProductSpec, frame_id: str) -> None:
    path = _pending_path(cfg, product, frame_id)
    if path.is_file():
        path.unlink()


def _link_tree(src: Path, dest: Path) -> None:
    if not src.is_dir():
        return
    if dest.exists():
        shutil.rmtree(dest)
    for path in src.rglob("*"):
        rel = path.relative_to(src)
        target = dest / rel
        if path.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        if path.suffix == ".png":
            try:
                os.link(path, target)
                continue
            except OSError:
                pass
        shutil.copy2(path, target)


def _install_frame(
    cfg: CookerConfig,
    job: FrameJob,
    product: ProductSpec,
    shared_root: Path,
    promoted: bool,
) -> None:
    from mpwg_radar.cooker import _alias_frame_tree

    for mode in cfg.modes:
        src = product.mode_dir(job.private_root, mode) / job.frame_id
        dest_root = product.mode_dir(shared_root, mode)
        dest = dest_root / job.frame_id
        dest_root.mkdir(parents=True, exist_ok=True)
        if src.is_dir() and dest.is_dir() and src.resolve() == dest.resolve():
            if promoted and cfg.publish_latest_alias:
                _alias_frame_tree(dest, dest_root / "latest")
            continue
        _link_tree(src, dest)
        if promoted and cfg.publish_latest_alias:
            _alias_frame_tree(dest_root / job.frame_id, dest_root / "latest")
    colorbar = job.private_root / product.colorbar_rel()
    if colorbar.is_file():
        target = shared_root / product.colorbar_rel()
        if colorbar.resolve() != target.resolve():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(colorbar, target)


def _current_tip(
    cfg: CookerConfig, radar_root: Path, product: ProductSpec
) -> Optional[str]:
    from mpwg_radar.cooker import _is_review_publish, _latest_id

    if cfg.review_manifest_key:
        tip = load_publish_state(state_path(radar_root)).get("latest_frame")
        return str(tip) if tip else None
    path = radar_root / "manifest.json"
    manifest: Dict = {}
    if path.is_file():
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            manifest = {}
    entry = (manifest.get("products") or {}).get(product.id) or {}
    if _is_review_publish(product):
        reviews = entry.get("reviews") or {}
        slot = reviews.get(cfg.rala_dbz_interp) or {}
        tip = slot.get("latest_frame")
        if tip:
            return str(tip)
    tip = entry.get("latest_frame")
    if tip:
        return str(tip)
    return _latest_id(product.mode_dir(radar_root, cfg.modes[0]) / "latest")


def _frame_entry(product: ProductSpec, mode: str, summary: Dict) -> Dict:
    frame_id = summary["id"]
    frame_rel = (
        f"{product.tile_prefix}/{mode}/{frame_id}/frame.json"
        if product.tile_prefix
        else f"{mode}/{frame_id}/frame.json"
    )
    return {
        "id": frame_id,
        "valid_time": summary.get("valid_time"),
        "tiles": product.tile_url_template(mode, frame_id),
        "frame": frame_rel,
    }


def _public_slot(
    cfg: CookerConfig, frame_id: str, promoted: bool, tip: Optional[str]
) -> Optional[str]:
    """None keeps the ``latest`` alias. A frame id is the timestamped template."""
    if cfg.publish_latest_alias:
        return None
    if promoted or not tip:
        return frame_id
    return tip


def build_review_document(
    cfg: CookerConfig,
    product: ProductSpec,
    palette: Palette,
    frames_oldest_first: Sequence[Dict],
    tip: str,
) -> Dict:
    """Self-contained review manifest. Not the production ``manifest.json``."""
    from mpwg_radar.cooker import _consumer_rala_latest, _frames_in_last_hour

    mode = cfg.modes[0]
    slot = "latest" if cfg.publish_latest_alias else tip
    return {
        "label": "NOT PRODUCTION",
        "dbz_interp": cfg.rala_dbz_interp,
        "spatial": SPATIAL_REVISION,
        "palette_version": palette.version,
        "palette": palette.as_dict(),
        "latest": product.tile_url_template(mode, slot),
        "latest_frame": tip,
        "frame_json": f"{product.tile_prefix}/{mode}/{slot}/frame.json",
        "consumer_latest": _consumer_rala_latest(mode),
        "frames": list(frames_oldest_first),
        "min_zoom": cfg.min_zoom,
        "max_zoom": cfg.max_zoom,
        "retention": {
            "max_age_minutes": cfg.rala_retention_minutes,
            "max_frames": cfg.rala_retention_frames,
            "frames_last_60_minutes": _frames_in_last_hour(
                list(frames_oldest_first), _now()
            ),
            "grace_seconds": int(effective_grace(cfg)),
        },
        "updated_at": _iso(_now()),
    }


def _retain_verified(
    cfg: CookerConfig, verified: Sequence[Dict], extra: Optional[Dict]
) -> List[Dict]:
    from mpwg_radar.cooker import apply_retention, frame_retention

    rows = [dict(item) for item in verified]
    if extra is not None and not any(item.get("id") == extra.get("id") for item in rows):
        rows.append(dict(extra))
    kept = apply_retention(rows, frame_retention(cfg))
    return kept


def _schedule_dropped(
    cfg: CookerConfig,
    product: ProductSpec,
    state: Dict,
    previous_ids: Sequence[str],
    kept_ids: Sequence[str],
    now: datetime,
) -> None:
    dropped = set(previous_ids) - set(kept_ids)
    prefixes = [
        frame_prefix(product, mode, frame_id)
        for frame_id in sorted(dropped)
        for mode in cfg.modes
    ]
    note_unlisted(state, prefixes, now, effective_grace(cfg))


def _schedule_partial(
    cfg: CookerConfig, product: ProductSpec, frame_id: str, radar_root: Path
) -> None:
    path = state_path(radar_root)
    state = load_publish_state(path)
    prefixes = [frame_prefix(product, mode, frame_id) for mode in cfg.modes]
    note_unlisted(state, prefixes, _now(), effective_grace(cfg))
    save_publish_state(path, state)


def _release_published_prefix(
    cfg: CookerConfig, product: ProductSpec, frame_id: str, radar_root: Path
) -> None:
    """Cancel a pending delete once this timestamp is in the live manifest.

    A failed upload queues ``clean/<ts>/``. The retry publishes that same
    timestamp. Leaving the old ``delete_after`` in place removes the tiles
    the manifest just named. If this commit did not keep the frame, the
    pending entry stays so retention can still remove the unlisted upload.
    """
    path = state_path(radar_root)
    if not path.is_file():
        return
    prefixes = [frame_prefix(product, mode, frame_id) for mode in cfg.modes]
    protected = protected_prefixes(cfg, product, radar_root)
    if protected is None:
        log.error(
            "Not clearing pending delete for %s; manifest could not be re-read",
            frame_id,
        )
        return
    if not any(prefix in protected for prefix in prefixes):
        return
    state = load_publish_state(path)
    for prefix in prefixes:
        drop_deleted(state, prefix)
    save_publish_state(path, state)
    log.info("Cleared pending remote delete for published frame %s", frame_id)


def emit_frame_timing(marks: Dict) -> None:
    log.info("frame_timing %s", json.dumps(marks, sort_keys=True, default=str))


def process_loaded_frame(
    cfg: CookerConfig,
    frame: ReflectivityFrame,
    *,
    detected_at: datetime,
    downloaded_at: datetime,
    decoded_at: datetime,
) -> FrameJob:
    """Render one frame into a private directory. No manifest and no upload.

    Large arrays are released before this returns so the next prepare does
    not overlap them. The publisher reads PNGs from disk.
    """
    from mpwg_radar.cooker import _product_for_publish, _write_mode

    frame_id = frame.frame_id
    log_memory("process", "start", frame_id)
    marks: Dict = {
        "frame_id": frame_id,
        "detect": _iso(detected_at),
        "download": _iso(downloaded_at),
        "decode": _iso(decoded_at),
    }
    private = cfg.output_dir / PIPELINE_WORK / frame_id
    if private.exists():
        shutil.rmtree(private)
    private.mkdir(parents=True, exist_ok=True)
    product = _product_for_publish(cfg)
    palette = load_palette(cfg.palette_id)
    if cfg.display_min_dbz is not None:
        palette = palette.with_display_min(cfg.display_min_dbz)
    summaries: List[Dict] = []
    try:
        for mode in cfg.modes:
            cooked = apply_mode(
                frame,
                mode,
                min_dbz=product.min_dbz_override,
                apply_dbz_floor=product.apply_dbz_floor,
                apply_despeckle=product.apply_despeckle,
                edge_aware=product.edge_aware_smooth,
                apply_grid_smooth=product.apply_grid_smooth,
            )
            summary, _private_promoted = _write_mode(
                cfg,
                palette,
                cooked,
                mode,
                private,
                product,
                stage_marks=marks,
            )
            summaries.append(summary)
            release_frame_arrays(cooked)
        write_colorbar(palette, private / product.colorbar_rel())
        if product.id == "composite":
            write_colorbar(palette, private / "colorbar.png")
    finally:
        release_frame_arrays(frame)
        gc.collect()
        log_memory("process", "end", frame_id)
    return FrameJob(
        frame_id=frame_id,
        valid_time=frame.valid_time,
        private_root=private,
        mode_summaries=summaries,
        marks=marks,
        source=frame.source,
        product_name=frame.product,
        palette_id=palette.id,
    )


def _publish_job(cfg: CookerConfig, job: FrameJob, upload: Optional[bool]) -> Dict:
    from mpwg_radar.cooker import (
        _mark_frame_uploaded,
        _need_upload,
        _product_for_publish,
        _prune_old_frames,
        _write_manifest,
        _write_status,
    )

    product = _product_for_publish(cfg)
    shared = cfg.output_dir / "radar"
    shared.mkdir(parents=True, exist_ok=True)
    log_memory("publish", "start", job.frame_id)
    job.marks["publish_start"] = _iso(_now())
    need_upload = _need_upload(cfg, upload)
    state_file = state_path(shared)
    snapshot = state_file.read_bytes() if state_file.is_file() else None
    try:
        _write_pending(cfg, product, job.frame_id)
        tip = _current_tip(cfg, shared, product)
        promoted = tip is None or job.frame_id >= tip
        if cfg.review_manifest_key:
            _commit_review(cfg, job, product, shared, promoted, tip, need_upload)
        else:
            _commit_product_manifest(
                cfg, job, product, shared, promoted, tip, need_upload, _write_manifest
            )
        if need_upload:
            _mark_frame_uploaded(cfg, shared, product, job.frame_id)
        _release_published_prefix(cfg, product, job.frame_id, shared)
        _prune_old_frames(cfg, shared, product)
        _sweep_if_possible(cfg, shared)
        result = _result_from_job(cfg, job, product, shared, promoted)
        _write_status(cfg, product, result)
        result["ok"] = True
        return result
    except Exception:
        if snapshot is None:
            if state_file.is_file():
                # The failed commit may have written state. Drop it when
                # this frame was the first one, so a retry starts clean.
                current = load_publish_state(state_file)
                verified = current.get("verified") or []
                if verified and all(item.get("id") == job.frame_id for item in verified):
                    state_file.unlink()
        else:
            state_file.write_bytes(snapshot)
        if need_upload:
            _schedule_partial(cfg, product, job.frame_id, shared)
        raise
    finally:
        _clear_pending(cfg, product, job.frame_id)
        if job.private_root.exists():
            shutil.rmtree(job.private_root, ignore_errors=True)
        for key in (
            "frame_put_done",
            "verify_done",
            "manifest_put",
            "latest_done",
            "enqueue",
            "queue_wait_seconds",
        ):
            job.marks.setdefault(key, None)
        log_memory("publish", "end", job.frame_id)
        emit_frame_timing(job.marks)


def _commit_review(
    cfg: CookerConfig,
    job: FrameJob,
    product: ProductSpec,
    shared: Path,
    promoted: bool,
    tip: Optional[str],
    need_upload: bool,
) -> None:
    palette = load_palette(cfg.palette_id)
    mode = cfg.modes[0]
    summary = next(item for item in job.mode_summaries if item.get("mode") == mode)
    extra = _frame_entry(product, mode, summary)
    path = state_path(shared)
    state = load_publish_state(path)
    previous_ids = [item.get("id") for item in state.get("verified") or []]
    kept = _retain_verified(cfg, state.get("verified") or [], extra)
    new_tip = job.frame_id if promoted or not tip else tip
    ordered = list(reversed(kept))
    document = build_review_document(cfg, product, palette, ordered, new_tip or job.frame_id)
    relative = cfg.review_manifest_key.lstrip("/")
    dest = shared / relative
    dest.parent.mkdir(parents=True, exist_ok=True)
    previous = dest.read_bytes() if dest.is_file() else None
    production = shared / "manifest.json"
    production_bytes = production.read_bytes() if production.is_file() else None

    def _write_locked() -> None:
        _install_frame(cfg, job, product, shared, promoted)
        dest.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
        state["verified"] = kept
        state["latest_frame"] = new_tip
        _schedule_dropped(
            cfg,
            product,
            state,
            [item for item in previous_ids if item],
            [item["id"] for item in kept],
            _now(),
        )
        save_publish_state(path, state)

    if need_upload:
        publisher = R2Publisher(cfg.r2)
        stats = publisher.publish_frame_atomic(
            job.private_root,
            job.frame_id,
            cfg.modes,
            product_id=product.id,
            include_latest=promoted,
            copy_latest=promoted and cfg.publish_latest_alias,
            tile_prefix=product.tile_prefix,
            publish_latest_alias=cfg.publish_latest_alias,
            manifest_relative=relative,
            manifest_path=dest,
            before_manifest=_write_locked,
            marks=job.marks,
            previous_manifest=previous,
            lock_root=shared,
        )
        _restore_production_manifest(production, production_bytes)
        return stats
    from mpwg_radar.cooker import manifest_lock

    with manifest_lock(shared):
        _write_locked()
    now = _iso(_now())
    job.marks["frame_put_done"] = now
    job.marks["verify_done"] = now
    job.marks["manifest_put"] = now
    job.marks["latest_done"] = now
    return UploadStats()


def _commit_product_manifest(
    cfg: CookerConfig,
    job: FrameJob,
    product: ProductSpec,
    shared: Path,
    promoted: bool,
    tip: Optional[str],
    need_upload: bool,
    write_manifest,
) -> None:
    from mpwg_radar.cooker import _is_review_publish

    palette = load_palette(cfg.palette_id)
    slot = _public_slot(cfg, job.frame_id, promoted, tip)
    manifest_path = shared / "manifest.json"
    previous = manifest_path.read_bytes() if manifest_path.is_file() else None
    cook_finished = _parse_time(job.marks.get("tiles_end")) or _now()

    def _write_locked() -> None:
        _install_frame(cfg, job, product, shared, promoted)
        write_manifest(
            cfg,
            palette,
            shared,
            _manifest_frame_proxy(job),
            job.mode_summaries,
            product,
            cook_finished_at=cook_finished,
            upload_finished_at=_now() if need_upload else None,
            promoted=promoted,
            public_latest_slot=slot,
            _locked=True,
        )

    if need_upload:
        publisher = R2Publisher(cfg.r2)
        publisher.publish_frame_atomic(
            job.private_root,
            job.frame_id,
            cfg.modes,
            product_id=product.id,
            include_latest=promoted,
            copy_latest=bool(
                promoted and cfg.publish_latest_alias and product.id == "rala"
            ),
            tile_prefix=product.tile_prefix if _is_review_publish(product) else None,
            publish_latest_alias=cfg.publish_latest_alias,
            manifest_path=manifest_path,
            before_manifest=_write_locked,
            marks=job.marks,
            previous_manifest=previous,
            lock_root=shared,
        )
    else:
        from mpwg_radar.cooker import manifest_lock

        with manifest_lock(shared):
            _write_locked()
        now = _iso(_now())
        job.marks["frame_put_done"] = now
        job.marks["verify_done"] = now
        job.marks["manifest_put"] = now
        job.marks["latest_done"] = now
    _note_manifest_retention(cfg, product, shared, previous)


def _note_manifest_retention(
    cfg: CookerConfig,
    product: ProductSpec,
    shared: Path,
    previous: Optional[bytes],
) -> None:
    """Schedule remote deletes for frame ids the new manifest dropped."""
    if previous is None:
        return
    try:
        old = json.loads(previous.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return
    new_path = shared / "manifest.json"
    if not new_path.is_file():
        return
    try:
        new = json.loads(new_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return
    old_ids = _manifest_frame_ids(old, product.id, cfg.modes[0])
    new_ids = _manifest_frame_ids(new, product.id, cfg.modes[0])
    dropped = old_ids - new_ids
    if not dropped:
        return
    path = state_path(shared)
    state = load_publish_state(path)
    _schedule_dropped(cfg, product, state, list(old_ids), list(new_ids), _now())
    save_publish_state(path, state)


def _manifest_frame_ids(manifest: Dict, product_id: str, mode: str) -> set:
    entry = (manifest.get("products") or {}).get(product_id) or {}
    frames = ((entry.get("modes") or {}).get(mode) or {}).get("frames") or []
    return {item.get("id") for item in frames if item.get("id")}


class _ManifestProxy:
    """The fields ``_write_manifest`` reads off a ``ReflectivityFrame``."""

    def __init__(self, job: FrameJob):
        self.frame_id = job.frame_id
        self.valid_time = job.valid_time
        self.product = job.product_name
        self.source = job.source


def _manifest_frame_proxy(job: FrameJob) -> _ManifestProxy:
    return _ManifestProxy(job)


def reconcile_remote_clean(
    publisher: R2Publisher,
    cfg: CookerConfig,
    product: ProductSpec,
    radar_root: Path,
    now: datetime,
    state: Dict,
) -> List[str]:
    """Delete untracked ``clean/`` prefixes older than the window plus grace.

    Runs only under the configured review prefix. A prefix the current
    manifest lists is spared. So is anything containing ``_opt_validate`` or
    ``_atomic_test``, and anything outside that review prefix. Objects that
    are missing from ``.publish-state.json`` are still deleted.
    """
    scope = review_scope(cfg)
    if not scope:
        return []
    from mpwg_radar.cooker import frame_retention

    policy = frame_retention(cfg)
    if not policy.max_age_seconds:
        return []
    limit = float(policy.max_age_seconds) + effective_grace(cfg)
    guard = _retention_skip(cfg, product, radar_root)
    deleted: List[str] = []
    for mode in cfg.modes:
        clean = f"{scope}/{mode}"
        try:
            found = publisher.list_object_sizes(clean)
        except Exception as exc:  # noqa: BLE001 — leave the objects for the next pass
            log.error("Remote retention list failed for %s (%s)", clean, exc)
            continue
        grouped: Dict[str, List[str]] = {}
        base = clean.strip("/") + "/"
        for key in found:
            rel = _strip_publisher_prefix(publisher, key)
            if not rel.startswith(base):
                continue
            segment = rel[len(base) :].split("/", 1)[0]
            if not segment or segment == "latest":
                continue
            child = f"{clean.strip('/')}/{segment}"
            grouped.setdefault(child, []).append(rel)
        for child, rels in sorted(grouped.items()):
            if prefix_blocked(child, scope) or any(
                prefix_blocked(rel, scope) for rel in rels
            ):
                log.info("Retention reconcile sparing excluded prefix %s", child)
                continue
            if not child.startswith(scope.strip("/") + "/"):
                continue
            segment = child.rsplit("/", 1)[-1]
            age = _frame_dir_age(segment, now)
            if age is None or age <= limit:
                continue
            reason = guard(child)
            if reason == "listed":
                log.info(
                    "Retention reconcile sparing %s; current manifest lists it",
                    child,
                )
                continue
            if reason in {"excluded", "unreadable"}:
                continue
            try:
                publisher.delete_prefix(child)
            except Exception as exc:  # noqa: BLE001
                log.error("Remote retention reconcile failed for %s (%s)", child, exc)
                continue
            drop_deleted(state, child)
            deleted.append(child)
            log.info(
                "Retention reconcile deleted %s (age %.0fs > window+grace %.0fs)",
                child,
                age,
                limit,
            )
    return deleted


def _sweep_if_possible(cfg: CookerConfig, shared: Path) -> None:
    """Grace deletes, then a review-prefix LIST of ``clean/``.

    An empty pending list does not skip the list. Untracked orphans older
    than the window plus grace are still removed. Production cooks do not
    call this.
    """
    if not cfg.r2.enabled:
        return
    try:
        publisher = R2Publisher(cfg.r2)
    except Exception as exc:  # noqa: BLE001 — local cooks have no R2
        log.info("Skipping remote retention sweep (%s)", exc)
        return
    from mpwg_radar.cooker import _product_for_publish

    product = _product_for_publish(cfg)
    path = state_path(shared)
    state = load_publish_state(path) if path.is_file() else empty_state()
    before = json.dumps(state.get("pending_deletes") or [], sort_keys=True)
    now = _now()
    sweep_remote_deletes(
        publisher, state, now, skip=_retention_skip(cfg, product, shared)
    )
    reconcile_remote_clean(publisher, cfg, product, shared, now, state)
    after = json.dumps(state.get("pending_deletes") or [], sort_keys=True)
    if after != before:
        save_publish_state(path, state)


def _result_from_job(
    cfg: CookerConfig,
    job: FrameJob,
    product: ProductSpec,
    shared: Path,
    promoted: bool,
) -> Dict:
    return {
        "ok": True,
        "frame_id": job.frame_id,
        "valid_time": _iso(job.valid_time),
        "source_valid_time": _iso(job.valid_time),
        "promoted": promoted,
        "source": job.source,
        "product": job.product_name,
        "product_id": product.id,
        "region": cfg.region_name,
        "modes": job.mode_summaries,
        "manifest": str((shared / "manifest.json").resolve()),
        "palette": job.palette_id,
        "queue_wait_seconds": job.marks.get("queue_wait_seconds"),
    }


def publish_serial_frame(
    cfg: CookerConfig,
    *,
    frame: ReflectivityFrame,
    palette: Palette,
    radar_root: Path,
    product: ProductSpec,
    mode_summaries: List[Dict],
    promoted: bool,
    cook_finished_at: datetime,
    should_upload: bool,
    consumer_manifest_live: bool,
) -> tuple:
    """Atomic publish for a frame ``cook()`` already rendered in place.

    The shared manifest is not advanced until frame objects verify. With
    ``MPWG_REVIEW_MANIFEST_KEY`` set, ``radar/manifest.json`` is not written
    and is not PUT.
    """
    from mpwg_radar.cooker import (
        _mark_frame_uploaded,
        _prune_old_frames,
        _write_manifest,
    )

    del consumer_manifest_live  # review key replaces the old manifest PUT gate
    _write_pending(cfg, product, frame.frame_id)
    stats = UploadStats()
    finished: Optional[datetime] = None
    radar_root.mkdir(parents=True, exist_ok=True)
    try:
        if not (should_upload and cfg.r2.enabled):
            _commit_local_only(
                cfg,
                frame,
                palette,
                radar_root,
                product,
                mode_summaries,
                promoted,
                cook_finished_at,
                _write_manifest,
            )
            if should_upload and not cfg.r2.enabled:
                log.info("R2 env not set — cooked locally, skipped upload")
            _prune_old_frames(cfg, radar_root, product)
            return stats, None
        stats = _upload_rendered_tree(
            cfg,
            frame,
            palette,
            radar_root,
            product,
            mode_summaries,
            promoted,
            cook_finished_at,
            _write_manifest,
        )
        finished = _now()
        _mark_frame_uploaded(cfg, radar_root, product, frame.frame_id)
        _release_published_prefix(cfg, product, frame.frame_id, radar_root)
        _prune_old_frames(cfg, radar_root, product)
        _sweep_if_possible(cfg, radar_root)
        return stats, finished
    except Exception:
        if should_upload and cfg.r2.enabled:
            _schedule_partial(cfg, product, frame.frame_id, radar_root)
        raise
    finally:
        _clear_pending(cfg, product, frame.frame_id)


def _commit_local_only(
    cfg,
    frame,
    palette,
    radar_root,
    product,
    mode_summaries,
    promoted,
    cook_finished_at,
    write_manifest,
) -> None:
    if cfg.review_manifest_key:
        job = FrameJob(
            frame_id=frame.frame_id,
            valid_time=frame.valid_time,
            private_root=radar_root,
            mode_summaries=mode_summaries,
            source=frame.source,
            product_name=frame.product,
            palette_id=palette.id,
        )
        tip = _current_tip(cfg, radar_root, product)
        _commit_review(
            cfg,
            job,
            product,
            radar_root,
            promoted,
            tip,
            need_upload=False,
        )
        return
    tip = _current_tip(cfg, radar_root, product)
    slot = _public_slot(cfg, frame.frame_id, promoted, tip)
    write_manifest(
        cfg,
        palette,
        radar_root,
        frame,
        mode_summaries,
        product,
        cook_finished_at=cook_finished_at,
        upload_finished_at=None,
        promoted=promoted,
        public_latest_slot=slot,
    )


def _upload_rendered_tree(
    cfg,
    frame,
    palette,
    radar_root,
    product,
    mode_summaries,
    promoted,
    cook_finished_at,
    write_manifest,
) -> UploadStats:
    from mpwg_radar.cooker import _is_review_publish

    production = radar_root / "manifest.json"
    production_bytes = production.read_bytes() if production.is_file() else None
    if cfg.review_manifest_key:
        job = FrameJob(
            frame_id=frame.frame_id,
            valid_time=frame.valid_time,
            private_root=radar_root,
            mode_summaries=mode_summaries,
            marks={},
            source=frame.source,
            product_name=frame.product,
            palette_id=palette.id,
        )
        tip = _current_tip(cfg, radar_root, product)
        stats = _commit_review(
            cfg, job, product, radar_root, promoted, tip, need_upload=True
        )
        _restore_production_manifest(production, production_bytes)
        return stats or UploadStats()
    tip = _current_tip(cfg, radar_root, product)
    slot = _public_slot(cfg, frame.frame_id, promoted, tip)
    previous = production_bytes

    def _write() -> None:
        # The caller already holds manifest_lock. Do not flock again.
        write_manifest(
            cfg,
            palette,
            radar_root,
            frame,
            mode_summaries,
            product,
            cook_finished_at=cook_finished_at,
            upload_finished_at=_now(),
            promoted=promoted,
            public_latest_slot=slot,
            _locked=True,
        )

    publisher = R2Publisher(cfg.r2)
    stats = publisher.publish_frame_atomic(
        radar_root,
        frame.frame_id,
        cfg.modes,
        product_id=product.id,
        include_latest=promoted,
        copy_latest=bool(promoted and cfg.publish_latest_alias and product.id == "rala"),
        tile_prefix=product.tile_prefix if _is_review_publish(product) else None,
        publish_latest_alias=cfg.publish_latest_alias,
        manifest_path=production,
        before_manifest=_write,
        previous_manifest=previous,
        lock_root=radar_root,
    )
    _note_manifest_retention(cfg, product, radar_root, previous)
    return stats


def cook_rala_archive_pipelined(cfg: CookerConfig, upload: Optional[bool]) -> Dict:
    """Newest-first archive with one publisher thread and a maxsize-1 queue."""
    from mpwg_radar import cooker as cooker_mod

    policy = cooker_mod.frame_retention(cfg)
    max_age = timedelta(seconds=policy.max_age_seconds or 0)
    try:
        scans = list(cooker_mod.list_recent_s3_scans(cfg, max_age=max_age))
    except Exception as exc:  # noqa: BLE001
        log.warning("RALA archive listing failed (%s); cooking latest scan only", exc)
        return cooker_mod.cook(cfg, source="mrms", upload=upload, archive=False)

    now = _now()
    ncep = None
    newest_age = None
    if scans:
        newest_age = (now - scans[0].valid_time.astimezone(timezone.utc)).total_seconds()
    if newest_age is None or newest_age > 180:
        ncep = cooker_mod._load_ncep_latest_frame(cfg)
    jobs = cooker_mod._archive_jobs(scans, ncep, policy, now)
    if not jobs:
        log.warning("RALA archive window was empty; cooking latest scan only")
        return cooker_mod.cook(cfg, source="mrms", upload=upload, archive=False)
    outside = [
        job
        for job in jobs
        if scan_outside_retention(job.valid_time, policy, now)
    ]
    if outside:
        log.info(
            "RALA pipeline skipping %d scan(s) already outside the %s-minute retention window",
            len(outside),
            cfg.rala_retention_minutes,
        )

    radar_root = cfg.output_dir / "radar"
    product = cooker_mod._product_for_publish(cfg)
    need_upload = cooker_mod._need_upload(cfg, upload)
    palette_version = load_palette(cfg.palette_id).version

    def _still_to_cook(window, moment, attempted_ids) -> List:
        return [
            job
            for job in window
            if job.frame_id not in attempted_ids
            and not scan_outside_retention(job.valid_time, policy, moment)
            and not cooker_mod._frame_is_complete(
                cfg,
                radar_root,
                product,
                job.frame_id,
                need_upload,
                palette_version=palette_version,
            )
        ]

    pending = _still_to_cook(jobs, now, set())
    log.info(
        "RALA pipeline window max_age_min=%s max_frames=%s listed=%d pending=%d queue_maxsize=1",
        cfg.rala_retention_minutes,
        cfg.rala_retention_frames,
        len(jobs),
        len(pending),
    )
    if not pending:
        return _unchanged_archive(cfg, jobs[0], product, radar_root, len(jobs))

    publisher = PublisherQueue(lambda item: _publish_job(cfg, item, upload))
    publisher.start()
    started = time.monotonic()
    budget = max(0.0, float(cfg.rala_catchup_budget_seconds))
    attempted: set = set()
    current_scans = scans
    window_jobs = jobs
    passes = 0
    failures = 0
    try:
        while True:
            if passes > 0 and (time.monotonic() - started) >= budget:
                log.info(
                    "RALA catch-up budget %.0fs reached after %d pass(es)",
                    budget,
                    passes,
                )
                break
            try:
                fresh = list(cooker_mod.list_recent_s3_scans(cfg, max_age=max_age))
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "RALA archive re-list failed (%s); using the previous window",
                    exc,
                )
                fresh = []
            if fresh:
                current_scans = cooker_mod._union_scans(current_scans, fresh)
            moment = _now()
            window_jobs = cooker_mod._archive_jobs(current_scans, ncep, policy, moment)
            pending = _still_to_cook(window_jobs, moment, attempted)
            if not pending:
                break
            job = pending[0]
            attempted.add(job.frame_id)
            passes += 1
            detected = _now()
            if scan_outside_retention(job.valid_time, policy, detected):
                log.info(
                    "RALA pipeline skip frame=%s; outside retention before publish",
                    job.frame_id,
                )
                continue
            log.info(
                "RALA pipeline process %d frame=%s valid_time=%s window=%d",
                passes,
                job.frame_id,
                _iso(job.valid_time),
                len(window_jobs),
            )
            try:
                if job.loaded is not None:
                    downloaded = detected
                    loaded = job.loaded
                else:
                    if not job.key:
                        raise cooker_mod.IngestError(
                            f"RALA scan {job.frame_id} has no source object"
                        )
                    detected_path = cooker_mod.download_s3_key(cfg, job.key, cfg.data_dir)
                    downloaded = _now()
                    loaded = cooker_mod.decode_grib2(
                        detected_path,
                        bbox=cfg.bbox,
                        product=cfg.product.mrms_name,
                    )
                decoded_at = _now()
            except Exception as exc:  # noqa: BLE001
                failures += 1
                log.error("RALA pipeline frame %s failed (%s); continuing", job.frame_id, exc)
                continue
            if scan_outside_retention(loaded.valid_time, policy, decoded_at):
                log.info(
                    "RALA pipeline skip frame=%s; outside retention before publish",
                    loaded.frame_id,
                )
                continue
            frame_job = process_loaded_frame(
                cfg,
                loaded,
                detected_at=detected,
                downloaded_at=downloaded,
                decoded_at=decoded_at,
            )
            if scan_outside_retention(frame_job.valid_time, policy, _now()):
                log.info(
                    "RALA pipeline skip frame=%s; outside retention before publish",
                    frame_job.frame_id,
                )
                if frame_job.private_root.exists():
                    shutil.rmtree(frame_job.private_root, ignore_errors=True)
                continue
            publisher.submit(frame_job)
    finally:
        publisher.close()

    cooked = [item for item in publisher.results if item.get("ok")]
    if not cooked:
        if failures == 0:
            log.info(
                "RALA pipeline cooked nothing; listed scans were already outside the retention window"
            )
            newest = window_jobs[0] if window_jobs else jobs[0]
            return _unchanged_archive(cfg, newest, product, radar_root, len(window_jobs))
        raise cooker_mod.IngestError(
            f"RALA archive had pending scan(s) but none cooked (attempted={len(attempted)})"
        )
    still_open = _still_to_cook(window_jobs, _now(), set())
    summary = max(cooked, key=lambda item: item.get("valid_time") or "")
    summary = dict(summary)
    summary["archive_cooked"] = len(cooked)
    summary["archive_pending"] = len(still_open)
    summary["archive_listed"] = len(window_jobs)
    cooker_mod._write_status(cfg, product, summary)
    log.info(
        "RALA pipeline pass cooked=%d pending=%d listed=%d newest=%s",
        len(cooked),
        len(still_open),
        len(window_jobs),
        summary.get("frame_id"),
    )
    return summary


def _unchanged_archive(cfg, newest, product, radar_root, listed: int) -> Dict:
    from mpwg_radar.cooker import _write_status

    finished = _now()
    age = (finished - newest.valid_time.astimezone(timezone.utc)).total_seconds()
    result = {
        "frame_id": newest.frame_id,
        "valid_time": _iso(newest.valid_time),
        "source_valid_time": _iso(newest.valid_time),
        "fetched_at": _iso(finished),
        "cook_finished_at": _iso(finished),
        "upload_finished_at": None,
        "source_age_seconds": round(age, 1),
        "cook_seconds": 0.0,
        "upload_seconds": 0.0,
        "lag_seconds": round(age, 1),
        "skipped": "unchanged",
        "source": "NOAA MRMS",
        "product": product.mrms_name,
        "product_id": product.id,
        "region": cfg.region_name,
        "modes": [],
        "manifest": str((radar_root / "manifest.json").resolve()),
        "uploaded": 0,
        "upload_skipped": 0,
        "upload_duration_seconds": 0.0,
        "palette": cfg.palette_id,
        "display_min_dbz": None,
        "archive_cooked": 0,
        "archive_pending": 0,
        "archive_listed": listed,
        "ok": True,
    }
    _write_status(cfg, product, result)
    log.info(
        "RALA pipeline already holds %d scan(s); newest=%s",
        listed,
        newest.frame_id,
    )
    return result
