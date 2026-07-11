#!/usr/bin/env bash
# ci_setup.sh - Install dependencies and build components for CI pipelines.

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON=${PYTHON:-python3}

echo "[i] Installing base requirements..."
"$PYTHON" -m pip install --upgrade pip
"$PYTHON" -m pip install -r "$PROJECT_ROOT/requirements.txt" -r "$PROJECT_ROOT/requirements-dev.txt"

echo "[i] CI setup complete."
