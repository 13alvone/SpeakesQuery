#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# SpeakesQuery - One-Command Installer
#
# Usage:  ./install.sh
#
# This script handles everything:
#   1. Verifies Docker is installed (with install instructions if not)
#   2. Verifies Docker daemon is running (with start instructions if not)
#   3. Verifies Docker Compose is available
#   4. Generates a .env file with secure defaults if one doesn't exist
#   5. Builds the Docker image (all deps, C++ components - fully automated)
#   6. Starts the container
#   7. Opens SpeakesQuery in your browser
#
# Options:
#   --port PORT    Override the default port (default: 5111)
#   --rebuild      Force a full image rebuild (no cache)
#   --stop         Stop a running SpeakesQuery container and exit
#   --status       Show container status and exit
#   -h, --help     Show this help message
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

# ── macOS PATH fix ────────────────────────────────────────────────────────
# Docker Desktop installs credential helpers to /usr/local/bin and
# /opt/homebrew/bin.  Some macOS shell configurations omit these from PATH,
# which causes "docker-credential-desktop: not found" during builds.
for _p in /usr/local/bin /opt/homebrew/bin; do
  case ":$PATH:" in
    *":$_p:"*) ;;          # already present
    *)         PATH="$_p:$PATH" ;;
  esac
done
unset _p

# ── Constants ──────────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$SCRIPT_DIR"
COMPOSE_FILE="$PROJECT_ROOT/desktop_app/docker-compose.yml"
ENV_FILE="$PROJECT_ROOT/.env"
ENV_EXAMPLE="$PROJECT_ROOT/.env.example"
CONTAINER_NAME="speakesquery-desktop"
DEFAULT_PORT=5111

# ── Colors (disabled if not a terminal) ────────────────────────────────────
if [[ -t 1 ]]; then
  RED='\033[0;31m'
  GREEN='\033[0;32m'
  YELLOW='\033[1;33m'
  BLUE='\033[0;34m'
  CYAN='\033[0;36m'
  BOLD='\033[1m'
  NC='\033[0m'
else
  RED='' GREEN='' YELLOW='' BLUE='' CYAN='' BOLD='' NC=''
fi

# ── Logging ────────────────────────────────────────────────────────────────
info()    { echo -e "${GREEN}[OK]${NC}  $*"; }
warn()    { echo -e "${YELLOW}[!!]${NC}  $*"; }
err()     { echo -e "${RED}[XX]${NC}  $*" >&2; }
step()    { echo -e "${CYAN}[>>]${NC}  ${BOLD}$*${NC}"; }
detail()  { echo -e "      $*"; }

banner() {
  echo ""
  echo -e "${BOLD}┌─────────────────────────────────────────────────────────┐${NC}"
  echo -e "${BOLD}│                                                         │${NC}"
  echo -e "${BOLD}│   ${CYAN}SpeakesQuery${NC}${BOLD} - Local Search & Ingestion Engine         │${NC}"
  echo -e "${BOLD}│   One-Command Installer                                 │${NC}"
  echo -e "${BOLD}│                                                         │${NC}"
  echo -e "${BOLD}└─────────────────────────────────────────────────────────┘${NC}"
  echo ""
}

# ── Argument parsing ──────────────────────────────────────────────────────
PORT="$DEFAULT_PORT"
REBUILD=0
STOP_ONLY=0
STATUS_ONLY=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --port)
      if [[ $# -lt 2 ]]; then err "--port requires a PORT number"; exit 2; fi
      PORT="$2"; shift 2 ;;
    --rebuild) REBUILD=1; shift ;;
    --stop)    STOP_ONLY=1; shift ;;
    --status)  STATUS_ONLY=1; shift ;;
    -h|--help)
      echo "Usage: ./install.sh [--port PORT] [--rebuild] [--stop] [--status] [-h]"
      echo ""
      echo "Options:"
      echo "  --port PORT   Override the default port (default: 5111)"
      echo "  --rebuild     Force a full image rebuild (no Docker cache)"
      echo "  --stop        Stop a running SpeakesQuery container and exit"
      echo "  --status      Show container status and exit"
      echo "  -h, --help    Show this help message"
      exit 0 ;;
    *)
      err "Unknown argument: $1"
      echo "Run ./install.sh --help for usage."
      exit 2 ;;
  esac
done

# ── Detect OS ─────────────────────────────────────────────────────────────
detect_os() {
  case "$(uname -s 2>/dev/null)" in
    Linux)  echo "linux" ;;
    Darwin) echo "macos" ;;
    MINGW*|MSYS*|CYGWIN*) echo "windows" ;;
    *)      echo "unknown" ;;
  esac
}

detect_linux_distro() {
  if [[ -r /etc/os-release ]]; then
    # shellcheck disable=SC1091
    . /etc/os-release
    local combined="${ID:-} ${ID_LIKE:-}"
    if echo "$combined" | grep -qiE 'ubuntu|debian'; then echo "debian"; return; fi
    if echo "$combined" | grep -qiE 'fedora|rhel|centos|rocky|alma'; then echo "rhel"; return; fi
    if echo "$combined" | grep -qiE 'arch'; then echo "arch"; return; fi
    if echo "$combined" | grep -qiE 'suse|opensuse'; then echo "suse"; return; fi
  fi
  echo "unknown"
}

OS="$(detect_os)"

# ═════════════════════════════════════════════════════════════════════════════
#  PREFLIGHT CHECKS
# ═════════════════════════════════════════════════════════════════════════════

banner

# ── 1. Docker installed? ──────────────────────────────────────────────────
step "Checking for Docker..."

if ! command -v docker &>/dev/null; then
  err "Docker is not installed."
  echo ""
  echo -e "  ${BOLD}Please install Docker and re-run this script.${NC}"
  echo ""

  case "$OS" in
    macos)
      detail "macOS (recommended):"
      detail "  Download Docker Desktop: https://www.docker.com/products/docker-desktop/"
      detail ""
      detail "Or via Homebrew:"
      detail "  brew install --cask docker"
      ;;
    linux)
      distro="$(detect_linux_distro)"
      case "$distro" in
        debian)
          detail "Debian/Ubuntu - install via the official convenience script:"
          detail "  curl -fsSL https://get.docker.com | sudo sh"
          detail "  sudo usermod -aG docker \$USER"
          detail "  newgrp docker"
          detail ""
          detail "Or follow the official guide:"
          detail "  https://docs.docker.com/engine/install/ubuntu/"
          ;;
        rhel)
          detail "RHEL/Fedora/CentOS/Rocky:"
          detail "  sudo dnf install -y dnf-plugins-core"
          detail "  sudo dnf config-manager --add-repo https://download.docker.com/linux/fedora/docker-ce.repo"
          detail "  sudo dnf install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin"
          detail "  sudo systemctl enable --now docker"
          detail "  sudo usermod -aG docker \$USER"
          detail "  newgrp docker"
          ;;
        arch)
          detail "Arch Linux:"
          detail "  sudo pacman -Sy --noconfirm docker docker-compose"
          detail "  sudo systemctl enable --now docker"
          detail "  sudo usermod -aG docker \$USER"
          detail "  newgrp docker"
          ;;
        suse)
          detail "openSUSE:"
          detail "  sudo zypper install docker docker-compose"
          detail "  sudo systemctl enable --now docker"
          detail "  sudo usermod -aG docker \$USER"
          ;;
        *)
          detail "Install Docker Engine:"
          detail "  https://docs.docker.com/engine/install/"
          ;;
      esac
      ;;
    windows)
      detail "Windows:"
      detail "  Download Docker Desktop: https://www.docker.com/products/docker-desktop/"
      detail "  Ensure WSL 2 backend is enabled."
      ;;
    *)
      detail "Install Docker: https://docs.docker.com/engine/install/"
      ;;
  esac

  echo ""
  detail "After installing, re-run:  ${BOLD}./install.sh${NC}"
  exit 1
fi

info "Docker is installed: $(docker --version)"

# ── 2. Docker daemon running? ─────────────────────────────────────────────
step "Checking if Docker daemon is running..."

if ! docker info &>/dev/null 2>&1; then
  err "Docker daemon is not running."
  echo ""

  case "$OS" in
    macos)
      detail "Start Docker Desktop from your Applications folder, or:"
      detail "  open -a Docker"
      detail ""
      detail "Wait for the Docker icon in the menu bar to show \"Docker Desktop is running\","
      detail "then re-run:  ${BOLD}./install.sh${NC}"
      ;;
    linux)
      detail "Start the Docker service:"
      detail "  sudo systemctl start docker"
      detail ""
      detail "To start Docker automatically on boot:"
      detail "  sudo systemctl enable docker"
      detail ""
      detail "If you get a permissions error, add your user to the docker group:"
      detail "  sudo usermod -aG docker \$USER"
      detail "  newgrp docker"
      detail ""
      detail "Then re-run:  ${BOLD}./install.sh${NC}"
      ;;
    *)
      detail "Start the Docker daemon and re-run:  ${BOLD}./install.sh${NC}"
      ;;
  esac

  exit 1
fi

info "Docker daemon is running."

# ── 3. Docker Compose available? ──────────────────────────────────────────
step "Checking for Docker Compose..."

COMPOSE_CMD=""
if docker compose version &>/dev/null 2>&1; then
  COMPOSE_CMD="docker compose"
elif command -v docker-compose &>/dev/null 2>&1; then
  COMPOSE_CMD="docker-compose"
fi

if [[ -z "$COMPOSE_CMD" ]]; then
  err "Docker Compose is not available."
  echo ""

  case "$OS" in
    macos)
      detail "Docker Desktop for Mac includes Compose. Ensure Docker Desktop is up to date."
      ;;
    linux)
      detail "Install the Docker Compose plugin:"
      detail "  sudo apt-get install -y docker-compose-plugin    # Debian/Ubuntu"
      detail "  sudo dnf install -y docker-compose-plugin        # Fedora/RHEL"
      detail ""
      detail "Or install standalone:"
      detail "  sudo curl -L \"https://github.com/docker/compose/releases/latest/download/docker-compose-\$(uname -s)-\$(uname -m)\" -o /usr/local/bin/docker-compose"
      detail "  sudo chmod +x /usr/local/bin/docker-compose"
      ;;
    *)
      detail "Install Docker Compose: https://docs.docker.com/compose/install/"
      ;;
  esac

  echo ""
  detail "Then re-run:  ${BOLD}./install.sh${NC}"
  exit 1
fi

info "Docker Compose is available: $($COMPOSE_CMD version 2>/dev/null | head -1)"

# ── macOS: credential-helper fix ─────────────────────────────────────────
# Docker Desktop sets "credsStore":"desktop" in ~/.docker/config.json but
# the docker-credential-desktop binary may not be on the daemon's PATH
# (common after Docker Desktop upgrades or on stripped-down shells).
# When the helper is unreachable, *every* build/pull fails.  Detect this
# and temporarily patch config.json in-place (restored on exit).
_docker_cfg="${DOCKER_CONFIG:-$HOME/.docker}"
_creds_store=""
_patched_docker_cfg=""
if [[ -f "$_docker_cfg/config.json" ]]; then
  _creds_store=$(sed -n 's/.*"credsStore" *: *"\([^"]*\)".*/\1/p' "$_docker_cfg/config.json")
fi
if [[ -n "$_creds_store" ]] && ! command -v "docker-credential-${_creds_store}" &>/dev/null; then
  warn "Docker credential helper \"docker-credential-${_creds_store}\" is not on PATH."
  detail "Temporarily patching ~/.docker/config.json (original backed up)."
  cp "$_docker_cfg/config.json" "$_docker_cfg/config.json.bak"
  sed -i.tmp '/"credsStore"/d' "$_docker_cfg/config.json" && rm -f "$_docker_cfg/config.json.tmp"
  _patched_docker_cfg="$_docker_cfg"
  # Restore original config on exit (normal or error).
  trap 'if [[ -n "$_patched_docker_cfg" && -f "$_patched_docker_cfg/config.json.bak" ]]; then mv "$_patched_docker_cfg/config.json.bak" "$_patched_docker_cfg/config.json"; fi' EXIT
fi
unset _docker_cfg _creds_store

# ── Handle --status ───────────────────────────────────────────────────────
if [[ "$STATUS_ONLY" -eq 1 ]]; then
  echo ""
  step "Container status:"
  if docker ps -a --filter "name=^${CONTAINER_NAME}$" --format "table {{.Status}}\t{{.Ports}}" | grep -q .; then
    docker ps -a --filter "name=^${CONTAINER_NAME}$" --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"
  else
    detail "No SpeakesQuery container found."
  fi
  exit 0
fi

# ── Handle --stop ─────────────────────────────────────────────────────────
if [[ "$STOP_ONLY" -eq 1 ]]; then
  echo ""
  step "Stopping SpeakesQuery..."
  if docker ps --filter "name=^${CONTAINER_NAME}$" --format '{{.Names}}' | grep -q .; then
    $COMPOSE_CMD -f "$COMPOSE_FILE" down
    info "SpeakesQuery stopped."
  else
    detail "No running SpeakesQuery container found."
  fi
  exit 0
fi

# ═════════════════════════════════════════════════════════════════════════════
#  ENVIRONMENT SETUP
# ═════════════════════════════════════════════════════════════════════════════

step "Setting up environment configuration..."

if [[ ! -f "$ENV_FILE" ]]; then
  if [[ -f "$ENV_EXAMPLE" ]]; then
    cp "$ENV_EXAMPLE" "$ENV_FILE"
    info "Created .env from .env.example"
  else
    : >"$ENV_FILE"
    info "Created empty .env"
  fi
fi

chmod 600 "$ENV_FILE" 2>/dev/null || true
info "Environment file ready: .env"

# ═════════════════════════════════════════════════════════════════════════════
#  PERSISTENT DATA DIRECTORIES
# ═════════════════════════════════════════════════════════════════════════════

step "Ensuring persistent data directories exist..."

# Directory mounts - bind-mounted into the container so user data survives
# container rebuild.  Each must exist on the host before `docker compose up`
# or Docker will create directories with root ownership inside the volume.
mkdir -p \
  "$PROJECT_ROOT/indexes" \
  "$PROJECT_ROOT/indexes/IMMUTABLE" \
  "$PROJECT_ROOT/lookups" \
  "$PROJECT_ROOT/saved_searches" \
  "$PROJECT_ROOT/macros" \
  "$PROJECT_ROOT/alert_groups" \
  "$PROJECT_ROOT/default_alert_groups" \
  "$PROJECT_ROOT/boilerplate_prompts" \
  "$PROJECT_ROOT/email_groups" \
  "$PROJECT_ROOT/analyzer_prompts" \
  "$PROJECT_ROOT/models" \
  "$PROJECT_ROOT/default_models" \
  "$PROJECT_ROOT/notebooks" \
  "$PROJECT_ROOT/default_notebooks" \
  "$PROJECT_ROOT/notebook_cache" \
  "$PROJECT_ROOT/jobs" \
  "$PROJECT_ROOT/youtube_profile" \
  "$PROJECT_ROOT/scheduled_input_scripts" \
  "$PROJECT_ROOT/executed_scheduled_searches"

# File mounts - Docker bind-mounts treat a missing source as a directory,
# which corrupts SQLite.  Touch them as empty files first so the container
# sees real (zero-byte) files and the engine's _init_db() routines populate
# the schema on first run.
touch \
  "$PROJECT_ROOT/global_settings.yaml" \
  "$PROJECT_ROOT/credentials.sqlite" \
  "$PROJECT_ROOT/last_chance.sqlite" \
  "$PROJECT_ROOT/scheduled_inputs.db" \
  "$PROJECT_ROOT/scheduled_inputs_history.db" \
  "$PROJECT_ROOT/saved_searches.db" \
  "$PROJECT_ROOT/saved_search_history.db" \
  "$PROJECT_ROOT/alert_group_runs.sqlite" \
  "$PROJECT_ROOT/claude_api_history.sqlite" \
  "$PROJECT_ROOT/analyzer_results.sqlite" \
  "$PROJECT_ROOT/llm_call_history.sqlite" \
  "$PROJECT_ROOT/notebook_cache.sqlite"

# Credential vault dir - shared with bare-metal install via the host's
# real home dir, so the same Fernet master key works in both modes.
mkdir -p "$HOME/.speakes-query"
chmod 700 "$HOME/.speakes-query" 2>/dev/null || true

# Access token (weakness audit W11b, 2026-07-12 - the Jupyter model).
# The Docker container binds 0.0.0.0 internally, which activates the
# server-side token gate; generate the token here (if absent) so we can
# print the ready-to-open URL below. The server reads the same file via
# the ~/.speakes-query bind mount, so the two always agree.
TOKEN_FILE="$HOME/.speakes-query/access_token"
if [[ ! -s "$TOKEN_FILE" ]]; then
  umask 077
  head -c 32 /dev/urandom | base64 | tr -d '/+=\n' > "$TOKEN_FILE"
  umask 022
  detail "Generated access token at $TOKEN_FILE"
fi
chmod 600 "$TOKEN_FILE" 2>/dev/null || true
ACCESS_TOKEN="$(cat "$TOKEN_FILE")"

info "Data directories + state files ready."

# ── Export host UID/GID for Docker volume permissions ─────────────────────
# On Linux, Docker volume mounts preserve host ownership.  The compose file
# uses DOCKER_UID / DOCKER_GID to run the container as the same user that
# owns the data directories, avoiding permission-denied errors.
export DOCKER_UID="$(id -u)"
export DOCKER_GID="$(id -g)"
detail "Container will run as UID=$DOCKER_UID GID=$DOCKER_GID"

# ═════════════════════════════════════════════════════════════════════════════
#  BUILD & LAUNCH
# ═════════════════════════════════════════════════════════════════════════════

# Stop any existing container first
if docker ps -a --filter "name=^${CONTAINER_NAME}$" --format '{{.Names}}' | grep -q .; then
  step "Stopping previous SpeakesQuery container..."
  $COMPOSE_CMD -f "$COMPOSE_FILE" down 2>/dev/null || true
  info "Previous container stopped."
fi

step "Building SpeakesQuery Docker image (this may take a few minutes on first run)..."
echo ""

BUILD_ARGS=()
if [[ "$REBUILD" -eq 1 ]]; then
  BUILD_ARGS+=(--no-cache)
  detail "Full rebuild requested (--no-cache)."
fi

if ! PORT="$PORT" $COMPOSE_CMD -f "$COMPOSE_FILE" build ${BUILD_ARGS[@]+"${BUILD_ARGS[@]}"} ; then
  echo ""
  err "Docker build failed. Check the output above for errors."
  detail "Common fixes:"
  detail "  - Ensure you have internet access (Docker pulls base images)"
  detail "  - Try again with: ./install.sh --rebuild"
  detail "  - Check Docker disk space: docker system df"
  exit 1
fi

echo ""
info "Docker image built successfully."

step "Starting SpeakesQuery..."

if ! PORT="$PORT" $COMPOSE_CMD -f "$COMPOSE_FILE" up -d; then
  echo ""
  err "Failed to start SpeakesQuery container."
  detail "Check logs: docker logs $CONTAINER_NAME"
  exit 1
fi

# ── Wait for the server to be ready ───────────────────────────────────────
step "Waiting for SpeakesQuery to be ready..."

MAX_WAIT=30
WAITED=0
while [[ "$WAITED" -lt "$MAX_WAIT" ]]; do
  # /healthz is exempt from the access-token gate - probing / would 401.
  if curl -s -o /dev/null -w '%{http_code}' "http://localhost:${PORT}/healthz" 2>/dev/null | grep -qE '^(200|302)'; then
    break
  fi
  sleep 1
  WAITED=$((WAITED + 1))
done

if [[ "$WAITED" -ge "$MAX_WAIT" ]]; then
  warn "Server hasn't responded after ${MAX_WAIT}s. It may still be starting up."
  detail "Check logs: docker logs $CONTAINER_NAME -f"
else
  info "SpeakesQuery is ready!"
fi

# ── Open browser ──────────────────────────────────────────────────────────
# The ?token= form authenticates the browser session once (the server
# promotes it to a cookie); plain http://localhost:PORT works afterwards.
URL="http://localhost:${PORT}/?token=${ACCESS_TOKEN}"

case "$OS" in
  macos)   open "$URL" 2>/dev/null || true ;;
  linux)   xdg-open "$URL" 2>/dev/null || true ;;
  windows) start "$URL" 2>/dev/null || true ;;
esac

# ── Done ──────────────────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}┌─────────────────────────────────────────────────────────┐${NC}"
echo -e "${BOLD}│                                                         │${NC}"
echo -e "${BOLD}│   ${GREEN}SpeakesQuery is running!${NC}${BOLD}                                │${NC}"
echo -e "${BOLD}│                                                         │${NC}"
echo -e "${BOLD}│   ${CYAN}Stop:${NC}     ./install.sh --stop                          ${BOLD}│${NC}"
echo -e "${BOLD}│   ${CYAN}Status:${NC}   ./install.sh --status                        ${BOLD}│${NC}"
echo -e "${BOLD}│   ${CYAN}Rebuild:${NC}  ./install.sh --rebuild                       ${BOLD}│${NC}"
echo -e "${BOLD}│   ${CYAN}Logs:${NC}     docker logs speakesquery-desktop -f            ${BOLD}│${NC}"
echo -e "${BOLD}│                                                         │${NC}"
echo -e "${BOLD}└─────────────────────────────────────────────────────────┘${NC}"
echo ""
echo -e "${CYAN}Open (first visit authenticates your browser; plain localhost works after):${NC}"
echo "    ${URL}"
echo ""
echo -e "${CYAN}Access token${NC} (kept at ~/.speakes-query/access_token, never in the repo):"
echo "    ${ACCESS_TOKEN}"
echo ""
echo -e "${CYAN}Optional next step - local LLM dispatch (Phase 2 / Bet 3):${NC}"
echo "    python -m tools.ollama_bootstrap"
echo ""
echo "Detects an Ollama daemon (install separately if needed), pulls the"
echo "registered model if missing, and verifies | llm dispatch end-to-end."
echo "Skip this if you only plan to use cloud providers."
echo ""
