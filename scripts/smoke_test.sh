#!/usr/bin/env bash
# Documented smoke test: sample Clean-palette 512×512 tiles (synthetic by default).
# Usage:
#   ./scripts/smoke_test.sh           # synthetic Central Texas storm
#   ./scripts/smoke_test.sh --live    # latest NOAA MRMS (falls back to synthetic)
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
PYTHON="${PYTHON:-python3}"
if [[ ! -d "$ROOT/.venv" ]]; then
  "$PYTHON" -m venv "$ROOT/.venv"
fi
# shellcheck disable=SC1091
source "$ROOT/.venv/bin/activate"
pip install -q -e "$ROOT"
OUT="${MPWG_SMOKE_OUT:-$ROOT/output/smoke}"
rm -rf "$OUT"
exec mpwg-radar smoke --out "$OUT" "$@"
