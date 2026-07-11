#!/usr/bin/env bash
set -euo pipefail

# setup.sh - Bootstrap environment and initialize the application
#
# Goals:
# - Require Python 3.12 - 3.14 (3.14 recommended; it matches the Docker image).
#   The old exact-3.12 pin existed for native/pybind components that were
#   replaced by DuckDB - no native builds remain.
# - Create ./env venv, install deps, initialize databases.
# - Be portable across major Linux families (Debian/Ubuntu, RHEL/Fedora, Arch) and macOS.
#
# Usage:
#   ./setup.sh
#   ./setup.sh --python /path/to/python3.14
#   ./setup.sh --venv-dir ./env
#   ./setup.sh --skip-dev
#   ./setup.sh --wheel-only
#   ./setup.sh --allow-source-builds
#   ./setup.sh --recreate-venv
#   ./setup.sh --env-file /path/to/.env

SCRIPT_NAME="$(basename "$0")"

log_info() { echo "[i] $*"; }
log_warn() { echo "[!] $*" >&2; }
log_err()  { echo "[x] $*" >&2; }
log_dbg()  { echo "[DEBUG] $*" >&2; }

usage() {
  cat >&2 <<EOF
Usage: $SCRIPT_NAME [options]

Bootstraps the project by creating a Python virtual environment, installing dependencies,
building custom components, and initializing application databases.

Options:
  --python PATH          Use a specific Python interpreter (must be Python 3.12 - 3.14).
  --venv-dir PATH        Virtual environment directory (default: ./env).
  --env-file PATH        Env file to load/write (default: ./PROJECT_ROOT/.env).
  --skip-dev             Do not install requirements-dev.txt.
  --wheel-only           Prefer binary wheels only (adds: --only-binary=:all:).
  --allow-source-builds  Allow building from source (overrides --wheel-only behavior).
  --recreate-venv        Delete and recreate the venv directory if it exists.
  -h, --help             Show this help message.

Notes:
  - Python 3.12 - 3.14 is supported; 3.14 is recommended (matches the Docker image).
  - During setup, if SECRET_KEY is missing, this script will generate one and write it to the env file.
  - The shipped Docker image is based on python:3.14-slim.
EOF
}

# -----------------------------
# Platform / distro hints
# -----------------------------
detect_os() {
  local uname_s
  uname_s="$(uname -s 2>/dev/null || true)"
  case "$uname_s" in
    Linux)  echo "linux" ;;
    Darwin) echo "macos" ;;
    *)      echo "unknown" ;;
  esac
}

detect_linux_family() {
  # Best-effort distro family detection for printing install hints.
  if [[ -r /etc/os-release ]]; then
    # shellcheck disable=SC1091
    . /etc/os-release
    local id_like="${ID_LIKE:-}"
    local id="${ID:-}"

    if echo "$id_like $id" | grep -qiE '(debian|ubuntu)'; then
      echo "debian"
      return
    fi
    if echo "$id_like $id" | grep -qiE '(rhel|fedora|centos|rocky|almalinux)'; then
      echo "rhel"
      return
    fi
    if echo "$id_like $id" | grep -qiE '(arch)'; then
      echo "arch"
      return
    fi
  fi
  echo "unknown"
}

install_hint_python() {
  local os family
  os="$(detect_os)"
  if [[ "$os" == "macos" ]]; then
    log_info "macOS install hint (Homebrew):"
    log_info "  brew install python@3.14"
    log_info "  Then rerun: ./$SCRIPT_NAME --python \$(brew --prefix)/opt/python@3.14/bin/python3.14"
    return
  fi

  if [[ "$os" == "linux" ]]; then
    family="$(detect_linux_family)"
    case "$family" in
      debian)
        log_info "Debian/Ubuntu install hint:"
        log_info "  sudo apt-get update"
        log_info "  sudo apt-get install -y python3.14 python3.14-venv python3.14-dev"
        log_info "  Then rerun: ./$SCRIPT_NAME --python \$(command -v python3.14)"
        log_info ""
        log_info "  NOTE: if your release does not ship python3.14 in the main"
        log_info "  archive, use the deadsnakes PPA (Ubuntu) or uv instead:"
        log_info "    sudo add-apt-repository ppa:deadsnakes/ppa && sudo apt-get update"
        log_info "    sudo apt-get install -y python3.14 python3.14-venv python3.14-dev"
        log_info "  or:"
        log_info "    curl -LsSf https://astral.sh/uv/install.sh | sh"
        log_info "    uv python install 3.14   # then: ./$SCRIPT_NAME --python \$(uv python find 3.14)"
        ;;
      rhel)
        log_info "RHEL/Fedora-family install hint:"
        log_info "  # On Fedora:"
        log_info "  sudo dnf install -y python3.14 python3.14-devel"
        log_info "  # On RHEL/Rocky/Alma you may need EPEL/CRB and/or AppStream modules:"
        log_info "  sudo dnf install -y python3.14 python3.14-devel || true"
        log_info "  Then rerun: ./$SCRIPT_NAME --python \$(command -v python3.14)"
        ;;
      arch)
        log_info "Arch install hint:"
        log_info "  sudo pacman -Sy --noconfirm python"
        log_info "  # If python is not 3.12 - 3.14, use pyenv or uv for a pinned toolchain."
        ;;
      *)
        log_info "Linux install hint:"
        log_info "  Install Python 3.12 - 3.14 (plus venv + dev headers) using your distro's package manager."
        ;;
    esac
    return
  fi

  log_info "Install hint:"
  log_info "  Install Python 3.12 - 3.14 and rerun with: ./$SCRIPT_NAME --python /path/to/python3.14"
}

# -----------------------------
# Args / defaults
# -----------------------------
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$PROJECT_ROOT/env"
ENV_FILE="$PROJECT_ROOT/.env"
PYTHON_BIN=""
SKIP_DEV=0
WHEEL_ONLY=0
ALLOW_SOURCE_BUILDS=0
RECREATE_VENV=0

if [[ "${1:-}" =~ ^(-h|--help)$ ]]; then
  usage
  exit 0
fi

while [[ $# -gt 0 ]]; do
  case "$1" in
    --python)
      if [[ $# -lt 2 ]]; then
        log_err "--python requires a PATH argument"
        exit 2
      fi
      PYTHON_BIN="$2"
      shift 2
      ;;
    --venv-dir)
      if [[ $# -lt 2 ]]; then
        log_err "--venv-dir requires a PATH argument"
        exit 2
      fi
      VENV_DIR="$2"
      shift 2
      ;;
    --env-file)
      if [[ $# -lt 2 ]]; then
        log_err "--env-file requires a PATH argument"
        exit 2
      fi
      ENV_FILE="$2"
      shift 2
      ;;
    --skip-dev)
      SKIP_DEV=1
      shift
      ;;
    --wheel-only)
      WHEEL_ONLY=1
      shift
      ;;
    --allow-source-builds)
      ALLOW_SOURCE_BUILDS=1
      shift
      ;;
    --recreate-venv)
      RECREATE_VENV=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      log_err "Unknown argument: $1"
      usage
      exit 2
      ;;
  esac
done

log_info "Project root: $PROJECT_ROOT"
log_dbg "Venv dir: $VENV_DIR"
log_dbg "Env file: $ENV_FILE"

# -----------------------------
# Resolve Python interpreter
# -----------------------------
if [[ -z "$PYTHON_BIN" ]]; then
  for candidate in python3.14 python3.13 python3.12; do
    if command -v "$candidate" >/dev/null 2>&1; then
      PYTHON_BIN="$(command -v "$candidate")"
      break
    fi
  done
  if [[ -z "$PYTHON_BIN" ]]; then
    log_err "Could not find python3.14, python3.13, or python3.12 in PATH."
    install_hint_python
    exit 1
  fi
fi

if [[ ! -x "$PYTHON_BIN" ]]; then
  log_err "Python interpreter not found or not executable: $PYTHON_BIN"
  install_hint_python
  exit 1
fi

# -----------------------------
# Enforce Python 3.12 - 3.14
# -----------------------------
PY_FULL="$("$PYTHON_BIN" -c 'import sys; print(".".join(map(str, sys.version_info[:3])))')"
PY_MAJ="$("$PYTHON_BIN" -c 'import sys; print(sys.version_info.major)')"
PY_MIN="$("$PYTHON_BIN" -c 'import sys; print(sys.version_info.minor)')"

if [[ "$PY_MAJ" -ne 3 || "$PY_MIN" -lt 12 || "$PY_MIN" -gt 14 ]]; then
  log_err "Python 3.12 - 3.14 is required for this release. Detected: $PY_FULL"
  log_info "Remediation:"
  install_hint_python
  log_info "Then rerun with:"
  log_info "  ./$SCRIPT_NAME --python /path/to/python3.14"
  exit 1
fi

log_info "Using Python: $PYTHON_BIN ($PY_FULL)"

# -----------------------------
# Create / recreate venv
# -----------------------------
if [[ -d "$VENV_DIR" && "$RECREATE_VENV" -eq 1 ]]; then
  log_info "Removing existing virtual environment at $VENV_DIR (per --recreate-venv)"
  rm -rf "$VENV_DIR"
fi

if [[ -d "$VENV_DIR" && -x "$VENV_DIR/bin/python" ]]; then
  log_info "Virtual environment already exists at $VENV_DIR"
else
  log_info "Creating virtual environment at $VENV_DIR using $PYTHON_BIN"
  "$PYTHON_BIN" -m venv "$VENV_DIR"
fi

# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"
PYTHON="$VENV_DIR/bin/python"

# Defensive check: ensure the venv python is also 3.12 - 3.14
VENV_PY_FULL="$("$PYTHON" -c 'import sys; print(".".join(map(str, sys.version_info[:3])))')"
VENV_PY_MAJ="$("$PYTHON" -c 'import sys; print(sys.version_info.major)')"
VENV_PY_MIN="$("$PYTHON" -c 'import sys; print(sys.version_info.minor)')"
if [[ "$VENV_PY_MAJ" -ne 3 || "$VENV_PY_MIN" -lt 12 || "$VENV_PY_MIN" -gt 14 ]]; then
  log_err "Venv python is not 3.12 - 3.14 (detected: $VENV_PY_FULL). Aborting."
  log_info "Try rerunning with: ./$SCRIPT_NAME --recreate-venv"
  exit 1
fi

# -----------------------------
# Install Python dependencies
# -----------------------------
log_info "Upgrading pip"
"$PYTHON" -m pip install --upgrade pip

PIP_INSTALL_OPTS=()
if [[ "$WHEEL_ONLY" -eq 1 && "$ALLOW_SOURCE_BUILDS" -eq 0 ]]; then
  PIP_INSTALL_OPTS+=(--only-binary=:all:)
fi

# Helper: run pip install with PIP_INSTALL_OPTS only when non-empty.
# macOS ships Bash 3.2, where "${arr[@]}" on an empty array is an unbound-
# variable error under `set -u`.  This wrapper avoids the issue portably.
run_pip_install() {
  if [[ "${#PIP_INSTALL_OPTS[@]}" -gt 0 ]]; then
    "$PYTHON" -m pip install "${PIP_INSTALL_OPTS[@]}" "$@"
  else
    "$PYTHON" -m pip install "$@"
  fi
}

REQ_MAIN="$PROJECT_ROOT/requirements.txt"
REQ_DEV="$PROJECT_ROOT/requirements-dev.txt"

if [[ ! -f "$REQ_MAIN" ]]; then
  log_err "Missing requirements file: $REQ_MAIN"
  exit 1
fi

if [[ "$SKIP_DEV" -eq 1 ]]; then
  log_info "Installing Python packages (main only)"
  run_pip_install -r "$REQ_MAIN"
else
  if [[ ! -f "$REQ_DEV" ]]; then
    log_warn "Dev requirements file not found: $REQ_DEV (continuing with main only)"
    run_pip_install -r "$REQ_MAIN"
  else
    log_info "Installing Python packages (main + dev)"
    run_pip_install -r "$REQ_MAIN" -r "$REQ_DEV"
  fi
fi

# -----------------------------
# Verify critical packages
# -----------------------------
# Some packages (lxml, cryptography) require C compilation or binary wheels.
# Fail fast if they didn't install correctly rather than surfacing cryptic
# ImportErrors at runtime.
CRITICAL_IMPORTS=(
  "pandas"
  "pyarrow"
  "flask"
  "requests"
  "bs4"
  "lxml"
  "cryptography"
  "restrictedpython"
  "apscheduler"
  "aiosmtplib"
  "certifi"
)

_verify_failed=0
for _mod in "${CRITICAL_IMPORTS[@]}"; do
  if ! "$PYTHON" -c "import $_mod" 2>/dev/null; then
    log_err "Critical package missing after install: $_mod"
    _verify_failed=1
  fi
done

if [[ "$_verify_failed" -eq 1 ]]; then
  log_err "One or more critical packages failed to install."
  log_info "Remediation:"
  log_info "  1. Check the pip output above for build errors."
  log_info "  2. Ensure C compiler and dev headers are available:"
  if [[ "$(detect_os)" == "macos" ]]; then
    log_info "       xcode-select --install"
  else
    log_info "       Debian/Ubuntu: sudo apt-get install -y build-essential python3.14-dev libxml2-dev libxslt1-dev"
    log_info "       RHEL/Fedora:   sudo dnf install -y gcc python3.14-devel libxml2-devel libxslt-devel"
  fi
  log_info "  3. Rerun: ./$SCRIPT_NAME --recreate-venv"
  exit 1
fi
log_info "All critical packages verified."

# -----------------------------
# Ensure env file exists + ensure SECRET_KEY is set
# -----------------------------
ensure_env_secret_key() {
  local env_file="$1"

  local env_dir
  env_dir="$(cd "$(dirname "$env_file")" && pwd)"
  if [[ ! -d "$env_dir" ]]; then
    log_info "Creating env directory: $env_dir"
    mkdir -p "$env_dir"
  fi

  if [[ ! -f "$env_file" ]]; then
    log_warn "Env file not found; creating: $env_file"
    : >"$env_file"
  fi

  if grep -qE '^[[:space:]]*SECRET_KEY=' "$env_file"; then
    log_info "SECRET_KEY present in env file."
    return 0
  fi

  local new_key
  new_key="$("$PYTHON" -c 'import secrets; print(secrets.token_urlsafe(48))' 2>/dev/null || true)"
  if [[ -z "$new_key" ]]; then
    log_err "Failed to generate SECRET_KEY."
    exit 1
  fi

  log_warn "SECRET_KEY missing; generating and writing to env file."
  {
    echo ""
    echo "# Generated by setup.sh on $(date -u +"%Y-%m-%dT%H:%M:%SZ")"
    echo "SECRET_KEY=$new_key"
  } >>"$env_file"

  chmod 600 "$env_file" 2>/dev/null || true
  log_info "Wrote SECRET_KEY to: $env_file"
}

ensure_env_secret_key "$ENV_FILE"

# -----------------------------
# Install pre-commit hooks (secret detection)
# -----------------------------
PRE_COMMIT_CFG="$PROJECT_ROOT/.pre-commit-config.yaml"
if [[ -f "$PRE_COMMIT_CFG" ]] && command -v pre-commit >/dev/null 2>&1; then
  log_info "Installing pre-commit hooks (detect-secrets)"
  (cd "$PROJECT_ROOT" && pre-commit install) || log_warn "pre-commit install failed (non-fatal)"
elif [[ -f "$PRE_COMMIT_CFG" ]] && "$PYTHON" -m pre_commit --version >/dev/null 2>&1; then
  log_info "Installing pre-commit hooks via Python module (detect-secrets)"
  (cd "$PROJECT_ROOT" && "$PYTHON" -m pre_commit install) || log_warn "pre-commit install failed (non-fatal)"
else
  log_warn "pre-commit not available - secret detection hook not installed."
  log_info "Install dev dependencies (including pre-commit) with: pip install -r requirements-dev.txt"
fi

log_info "Setup complete."
log_info "Activate the environment with: source \"$VENV_DIR/bin/activate\""
log_info "Env file used: $ENV_FILE"

