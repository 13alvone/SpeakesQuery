#!/usr/bin/env bash
set -euo pipefail

# run_all.sh - Launch all SpeakesQuery services concurrently.
# Usage: ./run_all.sh
# Starts the desktop app server, query engine and scheduled input engine.
# All processes are stopped together when the script exits.

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_ROOT"

# Activate the project venv if present (env/ or .venv/)
if [[ -f "$PROJECT_ROOT/env/bin/activate" ]]; then
  echo "[i] Activating project venv: $PROJECT_ROOT/env/"
  # shellcheck disable=SC1091
  source "$PROJECT_ROOT/env/bin/activate"
elif [[ -f "$PROJECT_ROOT/.venv/bin/activate" ]]; then
  echo "[i] Activating project venv: $PROJECT_ROOT/.venv/"
  # shellcheck disable=SC1091
  source "$PROJECT_ROOT/.venv/bin/activate"
fi

# Surface debugger configuration so developers immediately know whether attach
# mode is active for this launcher.
if [[ "${PYCHARM_ATTACH:-}" =~ ^([Tt]rue|[Yy]es|[Oo]n|1)$ ]]; then
  debug_host="${PYCHARM_DEBUG_HOST:-127.0.0.1}"
  debug_port="${PYCHARM_DEBUG_PORT:-5678}"
  echo "[i] PyCharm debugger attach enabled (PYCHARM_ATTACH=${PYCHARM_ATTACH})"
  echo "[i] Debug server target: ${debug_host}:${debug_port}"
  if [[ "${PYCHARM_DEBUG_PATCH_MULTIPROCESSING:-}" =~ ^([Ff]alse|0|[Nn]o|[Oo]ff)$ ]]; then
    echo "[!] Multiprocessing auto-patching is disabled; child processes must attach manually"
  fi
fi

# Attempt to decrypt repo-specific environment file
REPO_NAME="$(basename "$PROJECT_ROOT")"
ENV_ENC="$PROJECT_ROOT/input_repos/$REPO_NAME/.env.enc"
if [[ -f "$ENV_ENC" ]]; then
  echo "[i] Decrypting environment variables from $ENV_ENC"
  tmp_env="$(mktemp)"
  python utils/env_crypto.py decrypt "$ENV_ENC" > "$tmp_env"
  chmod 600 "$tmp_env"
  export ENV_PATH="$tmp_env"
  set -o allexport
  # shellcheck disable=SC1090
  source "$tmp_env"
  set +o allexport
fi

pids=()

echo "[i] Starting desktop app server..."
python desktop_app/server.py &
pids+=("$!")

echo "[i] Starting query engine..."
python query_engine/QueryEngine.py &
pids+=("$!")

echo "[i] Starting scheduled input engine..."
python scheduled_input_engine/ScheduledInputEngine.py &
pids+=("$!")

cleanup() {
  echo "[i] Shutting down services..."
  for pid in "${pids[@]}"; do
    if kill -0 "$pid" 2>/dev/null; then
      kill "$pid"
      wait "$pid" || true
    fi
  done
  if [[ -n "${tmp_env:-}" && -f "$tmp_env" ]]; then
    rm -f "$tmp_env"
  fi
}

trap cleanup INT TERM EXIT

wait
