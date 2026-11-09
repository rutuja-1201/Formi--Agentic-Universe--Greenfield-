#!/usr/bin/env bash
# Contract: ./run.sh <SIM_URL> runs until the scenario finishes. Uses Python 3 and SQLite; no service dependency.
set -euo pipefail
SIM_URL="${1:?usage: ./run.sh <SIM_URL>}"
ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"
PYTHON="${PYTHON:-python3}"
if [[ -x "$ROOT/.venv/bin/python" ]]; then PYTHON="$ROOT/.venv/bin/python"; fi
exec "$PYTHON" controller.py --url "$SIM_URL"
