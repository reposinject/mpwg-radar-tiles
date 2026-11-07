#!/usr/bin/env python3
"""Read-only checks for the composite recovery launcher. Prints no secrets."""

from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path


def _load(path: Path) -> dict:
    return json.loads(path.read_text())


def _public_ids(doc: dict) -> list[str]:
    frames = (
        ((doc.get("products") or {}).get("composite") or {}).get("modes") or {}
    ).get("clean", {}).get("frames") or []
    return [item["id"] for item in frames if isinstance(item, dict) and item.get("id")]


def _frame_dirs(clean: Path) -> set[str]:
    if not clean.is_dir():
        return set()
    return {path.name for path in clean.iterdir() if path.is_dir() and path.name != "latest"}


def cmd_public_ids(path: str) -> int:
    for frame_id in _public_ids(_load(Path(path))):
        print(frame_id)
    return 0


def cmd_fingerprint(cooker: str, products: str) -> int:
    cooker_text = Path(cooker).read_text()
    products_text = Path(products).read_text()
    if "def _reclaim_enabled" not in cooker_text:
        sys.exit("cooker patch missing")
    if "2147483647" in cooker_text:
        sys.exit("playback cap was removed")
    if 'sample_mode=SAMPLE_MASKED_BILINEAR' not in products_text:
        sys.exit("composite sample mode changed")
    if 'palette_id="mpwg-clean-2026-10-61"' not in products_text:
        sys.exit("composite palette changed")
    if "apply_grid_smooth=False" not in products_text.split("COMPOSITE_PREVIOUS")[0]:
        sys.exit("composite grid smooth changed")
    if 'RENDER_VERSION = "mpwg-r61mb-2026-10"' not in cooker_text:
        sys.exit("render version changed")
    print("FINGERPRINT_OK")
    return 0


def _recipe_ok(doc: dict, frame_id: str | None = None) -> None:
    if frame_id is not None and doc.get("id") != frame_id:
        raise SystemExit("frame id mismatch")
    if doc.get("render_version") != "mpwg-r61mb-2026-10" or doc.get("renderVersion") != "mpwg-r61mb-2026-10":
        raise SystemExit("render_version mismatch")
    palette = doc.get("palette") or {}
    if palette.get("id") != "mpwg-clean-2026-10-61" or palette.get("version") != "2026-10-61":
        raise SystemExit("palette mismatch")
    if float(palette.get("display_min_dbz")) != 15.0:
        raise SystemExit("display floor mismatch")
    spec = doc.get("mode_spec") or {}
    if spec.get("sample") != "masked-bilinear" or spec.get("smooth_kind") != "none":
        raise SystemExit("sample mismatch")
    stops = {stop.get("dbz"): stop for stop in palette.get("stops") or []}
    if (stops.get(29.0) or stops.get(29) or {}).get("hex") != "#65D65A":
        raise SystemExit("29 dBZ guard mismatch")
    if (stops.get(30.0) or stops.get(30) or {}).get("hex") != "#F4F20D":
        raise SystemExit("30 dBZ yellow mismatch")


def cmd_frame_recipe(path: str, frame_id: str) -> int:
    _recipe_ok(_load(Path(path)), frame_id)
    print("FRAME_RECIPE_OK", frame_id)
    return 0


def _fetch_json(url: str) -> dict:
    request = urllib.request.Request(url, headers={"Cache-Control": "no-cache", "User-Agent": "mpwg-recovery-check"})
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.loads(response.read().decode("utf-8"))


def cmd_after_cook(out: str, public: str, state: str, advance_log: str) -> int:
    root = Path(out)
    state_dir = Path(state)
    status = _load(root / "status-composite.json")
    if status.get("skipped") not in (None, ""):
        print("NO_ADVANCE", status.get("frame_id"))
        return 0
    frame_id = status.get("frame_id")
    if not frame_id or not status.get("upload_finished_at") or not status.get("uploaded"):
        sys.exit("cook did not publish a frame")
    if status.get("palette") != "mpwg-clean-2026-10-61" or float(status.get("display_min_dbz")) != 15.0:
        sys.exit("status palette mismatch")
    local = _load(root / "radar" / "clean" / frame_id / "frame.json")
    _recipe_ok(local, frame_id)
    written = int((local.get("stats") or {}).get("written") or 0)
    pngs = list((root / "radar" / "clean" / frame_id).rglob("*.png"))
    if written < 1 or len(pngs) != written:
        sys.exit(f"tile completeness pngs={len(pngs)} written={written}")
    manifest = _fetch_json(public.rstrip("/") + "/manifest.json")
    public_ids = _public_ids(manifest)
    tip = ((manifest.get("products") or {}).get("composite") or {}).get("latest_frame")
    if tip != frame_id or not public_ids or public_ids[0] != frame_id:
        sys.exit(f"public tip {tip} != {frame_id}")
    if len(public_ids) > 31 and (state_dir / "phase").read_text().strip() == "steady":
        sys.exit(f"playback manifest grew to {len(public_ids)}")
    remote = _fetch_json(public.rstrip("/") + f"/clean/{frame_id}/frame.json")
    _recipe_ok(remote, frame_id)
    phase = (state_dir / "phase").read_text().strip() if (state_dir / "phase").is_file() else ""
    if phase == "transition":
        pins = [line.strip() for line in (state_dir / "pin-ids.txt").read_text().splitlines() if line.strip()]
        missing = [item for item in pins if item not in public_ids]
        if missing:
            sys.exit("first publish dropped public ids " + ",".join(missing))
    before_file = state_dir / "local-ids-before.txt"
    if before_file.is_file():
        for frame in before_file.read_text().splitlines():
            if frame and not (root / "radar" / "clean" / frame).is_dir():
                sys.exit(f"local frame directory removed {frame}")
    aged = state_dir / "aged-public-ids.txt"
    previous = state_dir / "last-public-ids.txt"
    if previous.is_file() and phase == "steady":
        old_ids = [line.strip() for line in previous.read_text().splitlines() if line.strip()]
        dropped = [item for item in old_ids if item not in public_ids]
        with aged.open("a") as handle:
            for item in dropped:
                handle.write(item + "\n")
                url = public.rstrip("/") + f"/clean/{item}/frame.json"
                try:
                    _fetch_json(url)
                except Exception as exc:  # noqa: BLE001
                    sys.exit(f"aged public object missing {item}: {exc}")
    previous.write_text("\n".join(public_ids) + "\n")
    Path(advance_log).parent.mkdir(parents=True, exist_ok=True)
    with Path(advance_log).open("a") as handle:
        handle.write(f"ADVANCE {frame_id}\n")
    print("AFTER_COOK_OK", frame_id, "playback", len(public_ids))
    return 0


def main(argv: list[str]) -> int:
    command = argv[1] if len(argv) > 1 else ""
    if command == "public-ids":
        return cmd_public_ids(argv[2])
    if command == "fingerprint":
        return cmd_fingerprint(argv[2], argv[3])
    if command == "frame-recipe":
        return cmd_frame_recipe(argv[2], argv[3])
    if command == "after-cook":
        return cmd_after_cook(argv[2], argv[3], argv[4], argv[5])
    sys.exit("usage: fingerprint|public-ids|frame-recipe|after-cook")


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
