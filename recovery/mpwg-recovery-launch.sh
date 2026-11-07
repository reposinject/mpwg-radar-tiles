#!/usr/bin/env bash
# Continuous composite publisher for the installed oct29d cooker.
#
# This process ignores SIGHUP, and `start` launches it with nohup, so closing
# the task session does not stop it. It does not survive a box or container
# restart: PID 1 is tini, and no supervisor, cron, or boot path is configured.
# Nothing here installs or edits a supervisor. Internal cook failures are
# retried. A reboot or a kill of this process stops updates until this
# command is started again.
#
# Writes launcher state only under /workspace/mpwg-recovery.
# The existing tile tree /workspace/mpwg-cook-out is the cooker output.
# Saved frame objects are not deleted. Playback manifest stays at 30 frames
# after the first publish, which still lists every public id from before it.
set -euo pipefail

ROOT=/workspace/mpwg-recovery
LOG_DIR=$ROOT/logs
STATE=$ROOT/state
BACKUP=$ROOT/backup
LOCK=$STATE/.writer.lock
OUT=/workspace/mpwg-cook-out
BIN=/workspace/mpwg-cook-venv/bin/mpwg-radar
PY=/workspace/mpwg-cook-venv/bin/python
CHECK=$ROOT/bin/mpwg_recovery_check.py
PUBLIC=https://pub-829cdbedbe614c25b476d3e0a83c26c8.r2.dev/radar
MIN_FREE_KIB=10485760
SLEEP_SECONDS=180
MODE=${1:-start}

umask 077
mkdir -p "$LOG_DIR" "$STATE" "$BACKUP" "$ROOT/bin"

log() {
  printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" | tee -a "$LOG_DIR/launcher.log"
}

disk_ok() {
  local avail
  avail=$(df -Pk /workspace | awk 'NR==2 {print $4}')
  if [[ -z "$avail" || "$avail" -lt "$MIN_FREE_KIB" ]]; then
    log "STOP headroom avail_kib=${avail:-unknown} floor=$MIN_FREE_KIB; not deleting"
    return 1
  fi
}

source_cook_env() {
  set +x
  set -a
  # shellcheck disable=SC1091
  source /workspace/mpwg-radar.env
  # shellcheck disable=SC1091
  source /workspace/mpwg-r2-credentials.env
  set +a
  unset MPWG_RECLAIM MPWG_PALETTE MPWG_DISPLAY_MIN_DBZ MPWG_PIN_FRAME_IDS_FILE
  export MPWG_KEEP_DBZ=0
  export MPWG_RETENTION_FRAMES=30
  export MPWG_PRODUCT=composite
  export MPWG_MODES=clean
  export MPWG_REGION=conus
  export MPWG_MIN_ZOOM=6
  export MPWG_MAX_ZOOM=8
  export MPWG_TILE_SIZE=512
  PYTHONDONTWRITEBYTECODE=1 "$PY" - <<'PY'
import os, sys
missing = [name for name in ("R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY") if not os.environ.get(name)]
if not (os.environ.get("R2_ENDPOINT") or os.environ.get("R2_ACCOUNT_ID")):
    missing.append("R2_ENDPOINT_or_R2_ACCOUNT_ID")
if os.environ.get("MPWG_RECLAIM"):
    missing.append("MPWG_RECLAIM_still_set")
if os.environ.get("MPWG_PALETTE") or os.environ.get("MPWG_DISPLAY_MIN_DBZ"):
    missing.append("palette_override_still_set")
if missing:
    sys.exit("refusing cook, missing or unsafe names: " + ",".join(missing))
print("credential_names_present")
PY
}

prepare_transition() {
  local manifest="$BACKUP/public-manifest-before.json"
  mkdir -p "$BACKUP/incoming"
  if [[ ! -f "$manifest" ]]; then
    curl --disable -fsS --netrc-file /dev/null "$PUBLIC/manifest.json" -o "$manifest"
    cp -a "$OUT/radar/manifest.json" "$BACKUP/local-manifest-before.json" 2>/dev/null || true
    cp -a "$OUT/radar/.published-composite" "$BACKUP/published-composite-before" 2>/dev/null || true
    find "$OUT/radar/clean" -mindepth 1 -maxdepth 1 -type d ! -name latest -printf '%f\n' \
      | sort > "$STATE/local-ids-before.txt"
  fi
  PYTHONDONTWRITEBYTECODE=1 "$PY" "$CHECK" public-ids "$manifest" > "$STATE/pin-ids.txt"
  : > "$STATE/added-frame-json.ids"
  while IFS= read -r id; do
    [[ -n "$id" ]] || continue
    local dest="$OUT/radar/clean/$id/frame.json"
    if [[ -f "$dest" ]]; then
      continue
    fi
    mkdir -p "$BACKUP/incoming/$id"
    curl --disable -fsS --netrc-file /dev/null \
      "$PUBLIC/clean/$id/frame.json" -o "$BACKUP/incoming/$id/frame.json"
    PYTHONDONTWRITEBYTECODE=1 "$PY" "$CHECK" frame-recipe "$BACKUP/incoming/$id/frame.json" "$id"
    mkdir -p "$OUT/radar/clean/$id"
    cp "$BACKUP/incoming/$id/frame.json" "$dest"
    printf '%s\n' "$id" >> "$STATE/added-frame-json.ids"
    log "restored metadata $id"
  done < "$STATE/pin-ids.txt"
  log "transition pin_count=$(wc -l < "$STATE/pin-ids.txt") added=$(wc -l < "$STATE/added-frame-json.ids" 2>/dev/null || echo 0)"
}

cook_once() {
  local cook_log=$1
  (
    cd "$STATE"
    if [[ -f "$STATE/.env" ]]; then
      echo "refusing cook: $STATE/.env exists"
      exit 1
    fi
    source_cook_env
    if [[ "$(cat "$STATE/phase")" == "transition" ]]; then
      export MPWG_PIN_FRAME_IDS_FILE=$STATE/pin-ids.txt
    fi
    "$BIN" cook \
      --source mrms \
      --region conus \
      --modes clean \
      --product composite \
      --min-zoom 6 \
      --max-zoom 8 \
      --out "$OUT" \
      --upload
  ) >"$cook_log" 2>&1
}

assert_clean_log() {
  if grep -E 'Pruning local frame|Deleted [0-9]+ objects' "$1"; then
    log "FATAL deletion text in cook log; not retrying and not rewriting the public manifest"
    return 2
  fi
}

run_loop() {
  trap '' HUP
  exec 9>"$LOCK"
  if ! flock -n 9; then
    log "writer lock held: $LOCK"
    exit 1
  fi
  log "launcher pid=$$ does not survive box or container restart; SIGHUP ignored; retry is inside this process"
  PYTHONDONTWRITEBYTECODE=1 "$PY" "$CHECK" fingerprint \
    /workspace/mpwg-radar-tiles/src/mpwg_radar/cooker.py \
    /workspace/mpwg-radar-tiles/src/mpwg_radar/products.py
  if [[ ! -f "$STATE/phase" ]]; then
    printf '%s\n' transition > "$STATE/phase"
  fi
  local backoff=15
  local advances=0
  while true; do
    disk_ok || exit 1
    if [[ "$(cat "$STATE/phase")" == "transition" ]]; then
      prepare_transition
    fi
    local cook_log="$LOG_DIR/cook-$(date -u +%Y%m%dT%H%M%SZ).log"
    set +e
    cook_once "$cook_log"
    local cook_rc=$?
    set -e
    if [[ "$cook_rc" -ne 0 ]]; then
      log "cook exit=$cook_rc; retry in ${backoff}s; no manifest rollback"
      sleep "$backoff"
      if [[ "$backoff" -lt 300 ]]; then
        backoff=$((backoff * 2))
      fi
      continue
    fi
    local fatal=0
    assert_clean_log "$cook_log" || fatal=$?
    if [[ "$fatal" -eq 2 ]]; then
      exit 1
    fi
    if ! PYTHONDONTWRITEBYTECODE=1 "$PY" "$CHECK" after-cook \
      "$OUT" "$PUBLIC" "$STATE" "$LOG_DIR/advances.log"; then
      log "validation failed; retry in ${backoff}s; no manifest rollback"
      sleep "$backoff"
      if [[ "$backoff" -lt 300 ]]; then
        backoff=$((backoff * 2))
      fi
      continue
    fi
    backoff=15
    if [[ "$(cat "$STATE/phase")" == "transition" ]]; then
      printf '%s\n' steady > "$STATE/phase"
      log "transition publish kept every pre-cook public id; later playback uses 30 without deleting objects"
    fi
    if grep -q '^ADVANCE ' "$LOG_DIR/advances.log"; then
      advances=$(grep -c '^ADVANCE ' "$LOG_DIR/advances.log" || true)
    fi
    if [[ "$advances" -ge 3 && ! -f "$STATE/healthy" ]]; then
      printf '%s\n' "$advances" > "$STATE/healthy"
      log "continuous updates healthy after $advances advancing public scans; launcher stays running"
    fi
    sleep "$SLEEP_SECONDS"
  done
}

case "$MODE" in
  start)
    if [[ -f "$STATE/launcher.pid" ]] && kill -0 "$(cat "$STATE/launcher.pid")" 2>/dev/null; then
      log "already running pid=$(cat "$STATE/launcher.pid")"
      exit 1
    fi
    nohup "$0" run >> "$LOG_DIR/launcher.log" 2>&1 &
    printf '%s\n' "$!" > "$STATE/launcher.pid"
    log "started pid=$(cat "$STATE/launcher.pid") nohup; disconnect does not stop it; restart of the box does"
    ;;
  run)
    run_loop
    ;;
  *)
    echo "usage: $0 start|run" >&2
    exit 2
    ;;
esac
