#!/usr/bin/env bash
# Documented smoke test: sample Clean-palette 512×512 tiles (synthetic by default).
# Usage:
#   ./scripts/smoke_test.sh           # synthetic Central Texas storm
#   ./scripts/smoke_test.sh --live    # latest NOAA MRMS (falls back to synthetic)
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
PYTHON="${PYTHON:-python3}"
OUT="${MPWG_SMOKE_OUT:-$ROOT/output/smoke}"
rm -rf "$OUT"

activate_or_fallback() {
  if [[ -x "$ROOT/.venv/bin/mpwg-radar" ]]; then
    # shellcheck disable=SC1091
    source "$ROOT/.venv/bin/activate"
    return
  fi
  rm -rf "$ROOT/.venv"
  if "$PYTHON" -m venv "$ROOT/.venv" >/dev/null 2>&1 && [[ -x "$ROOT/.venv/bin/pip" ]]; then
    # shellcheck disable=SC1091
    source "$ROOT/.venv/bin/activate"
    pip install -q -e "$ROOT"
    return
  fi
  rm -rf "$ROOT/.venv"
  export PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}"
  if ! "$PYTHON" -c "import mpwg_radar" 2>/dev/null; then
    "$PYTHON" -m pip install -q -e "$ROOT"
  fi
}

activate_or_fallback
if command -v mpwg-radar >/dev/null 2>&1; then
  exec mpwg-radar smoke --out "$OUT" "$@"
fi
exec "$PYTHON" -m mpwg_radar smoke --out "$OUT" "$@"
