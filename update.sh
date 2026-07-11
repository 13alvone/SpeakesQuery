#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# SpeakesQuery - One-Command Docker Update
#
# Replaces the manual five-command workflow you were typing after every
# commit/push cycle on ``the SpeakesQuery host``:
#
#   sudo docker container ls -a
#   sudo docker container stop speakesquery-desktop
#   sudo docker container rm speakesquery-desktop
#   cd ~/speakesquery/
#   ./install.sh
#
# With one command:
#
#   ./update.sh
#
# What this does (in order):
#   1. Pre-flight: verifies Docker is installed + daemon running, install.sh
#      exists + is executable, and the script is running from the project root
#   2. Optional: ``git pull`` if --pull is passed (default off so you can
#      rebuild a local branch that hasn't been pushed)
#   3. Shows current container state (equivalent of your ``container ls -a``)
#   4. Gracefully stops + removes the SpeakesQuery container if present.
#      A container that does NOT exist is treated as already-cleaned, not
#      as an error.
#   5. Runs ./install.sh, forwarding any extra flags (--rebuild, --port N,
#      --no-pull-cache, etc.) so this script is a superset of install.sh
#   6. Shows final container state so you know it's up.
#
# Options:
#   --pull              Run ``git pull --ff-only`` before the rebuild
#   --no-sudo           Assume user is in the docker group (skip sudo)
#   --container NAME    Override container name (default: speakesquery-desktop)
#   --dry-run           Print what would happen without executing anything
#   --skip-stop         Skip the stop+rm step; go straight to install.sh
#                       (only useful if a prior run was interrupted)
#   --no-backup         Skip the pre-update tar.gz of user data (default: backup)
#   --no-snapshot       Skip the pre/post snapshot + regression diff
#   --backup-dir DIR    Where backups + snapshots land
#                       (default: ~/speakesquery-backups)
#   --rollback          Restore the most recent backup tarball INSTEAD of
#                       running an update. Skips git pull, install.sh, the
#                       container stop/start. Forces overwrite.
#   -h, --help          Show this help
#
# Any other flags are forwarded verbatim to ./install.sh - so
#   ./update.sh --rebuild --port 5112
# runs your stop+remove, then ``./install.sh --rebuild --port 5112``.
#
# Exit codes:
#   0 - update completed; container is running
#   1 - pre-flight failure (missing install.sh, docker not installed, etc.)
#   2 - user aborted (Ctrl-C, --dry-run shown)
#   3 - install.sh returned non-zero
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

# ── macOS PATH fix (mirrors install.sh - Docker Desktop credential helpers
#    live in /usr/local/bin or /opt/homebrew/bin on macOS but some shells
#    omit those paths, causing docker-credential-desktop: not found at
#    build time) ─────────────────────────────────────────────────────────
for _p in /usr/local/bin /opt/homebrew/bin; do
  case ":$PATH:" in
    *":$_p:"*) ;;
    *)         PATH="$_p:$PATH" ;;
  esac
done
unset _p

# ── Constants ─────────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$SCRIPT_DIR"
INSTALL_SH="$PROJECT_ROOT/install.sh"
DEFAULT_CONTAINER="speakesquery-desktop"

# ── Colors (match install.sh's palette for visual consistency) ───────────
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

info()   { echo -e "${GREEN}[OK]${NC}  $*"; }
warn()   { echo -e "${YELLOW}[!!]${NC}  $*"; }
err()    { echo -e "${RED}[XX]${NC}  $*" >&2; }
step()   { echo -e "${CYAN}[>>]${NC}  ${BOLD}$*${NC}"; }
detail() { echo -e "      $*"; }

banner() {
  echo ""
  echo -e "${BOLD}┌─────────────────────────────────────────────────────────┐${NC}"
  echo -e "${BOLD}│                                                         │${NC}"
  echo -e "${BOLD}│   ${CYAN}SpeakesQuery${NC}${BOLD} - Docker Update                            │${NC}"
  echo -e "${BOLD}│   Stop → Remove → Rebuild → Start                       │${NC}"
  echo -e "${BOLD}│                                                         │${NC}"
  echo -e "${BOLD}└─────────────────────────────────────────────────────────┘${NC}"
  echo ""
}

show_help() {
  cat <<'HELP'
SpeakesQuery - One-Command Docker Update

Replaces the manual five-command workflow after every commit/push cycle:
    sudo docker container ls -a
    sudo docker container stop speakesquery-desktop
    sudo docker container rm   speakesquery-desktop
    cd ~/speakesquery/
    ./install.sh

With one command:
    ./update.sh [options] [-- install.sh flags]

What it does (in order):
  1. Pre-flight checks (install.sh present, docker reachable)
  2. Optional git pull --ff-only (pass --pull to enable)
  3. Show current container state
  4. Stop + remove the SpeakesQuery container (skipped if it doesn't exist)
  5. Run ./install.sh, forwarding any extra flags
  6. Show final container state

Options:
  --pull              Run ``git pull --ff-only`` before rebuilding
  --no-sudo           Assume user is in the docker group (skip sudo)
  --sudo              Force sudo for docker commands
  --container NAME    Override container name (default: speakesquery-desktop)
  --dry-run           Print planned actions without executing them
  --skip-stop         Skip the stop+rm step (prior run was interrupted)
  --no-backup         Skip the pre-update tarball of user data
  --no-snapshot       Skip the pre/post snapshot + regression diff
  --backup-dir DIR    Where backups + snapshots land
                      (default: ~/speakesquery-backups)
  --rollback          Restore the most recent backup tarball INSTEAD of
                      running an update. Skips git pull, install.sh, and
                      the container stop/start. Forces overwrite.
  -h, --help          Show this help

Anything not matched above is forwarded to install.sh, so e.g.
    ./update.sh --pull --rebuild --port 5112
does: git pull → stop → rm → ./install.sh --rebuild --port 5112

Exit codes:
  0 - update completed, container running
  1 - pre-flight failure (missing install.sh, docker not installed)
  2 - user aborted
  3 - install.sh or docker rm returned non-zero
HELP
}

# ── Argument parsing ─────────────────────────────────────────────────────
DO_PULL=0
USE_SUDO="auto"           # auto | yes | no
CONTAINER_NAME="$DEFAULT_CONTAINER"
DRY_RUN=0
SKIP_STOP=0
DO_BACKUP=1               # default ON - pre-update tarball safety net
DO_SNAPSHOT=1             # default ON - pre/post diff for regression detection
DO_ROLLBACK=0
BACKUP_DIR="${HOME}/speakesquery-backups"
PRE_SNAP=""               # populated by snapshot_pre()
# Forwarded verbatim to install.sh - anything we don't recognise goes here.
INSTALL_ARGS=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --pull)
      DO_PULL=1
      shift
      ;;
    --no-sudo)
      USE_SUDO="no"
      shift
      ;;
    --sudo)
      USE_SUDO="yes"
      shift
      ;;
    --container)
      if [[ $# -lt 2 ]]; then
        err "--container requires an argument"
        exit 1
      fi
      CONTAINER_NAME="$2"
      shift 2
      ;;
    --container=*)
      CONTAINER_NAME="${1#*=}"
      shift
      ;;
    --dry-run)
      DRY_RUN=1
      shift
      ;;
    --skip-stop)
      SKIP_STOP=1
      shift
      ;;
    --no-backup)
      DO_BACKUP=0
      shift
      ;;
    --no-snapshot)
      DO_SNAPSHOT=0
      shift
      ;;
    --backup-dir)
      if [[ $# -lt 2 ]]; then
        err "--backup-dir requires a directory path"
        exit 1
      fi
      BACKUP_DIR="$2"
      shift 2
      ;;
    --backup-dir=*)
      BACKUP_DIR="${1#*=}"
      shift
      ;;
    --rollback)
      DO_ROLLBACK=1
      shift
      ;;
    -h|--help)
      show_help
      exit 0
      ;;
    *)
      # Forward anything else to install.sh (e.g. --rebuild --port 5112)
      INSTALL_ARGS+=("$1")
      shift
      ;;
  esac
done

# ── Sudo autodetect ──────────────────────────────────────────────────────
# If user is in the docker group OR is root, we don't need sudo.  On many
# Linux installs the group membership is standard; Docker Desktop on macOS
# never needs sudo.  Autodetect by trying a no-op docker call without sudo.
resolve_sudo() {
  if [[ "$USE_SUDO" == "yes" ]]; then
    SUDO_CMD="sudo"
  elif [[ "$USE_SUDO" == "no" ]]; then
    SUDO_CMD=""
  else
    # auto
    if [[ "$(id -u)" -eq 0 ]]; then
      SUDO_CMD=""
    elif docker info >/dev/null 2>&1; then
      SUDO_CMD=""
    elif command -v sudo >/dev/null 2>&1 && sudo -n docker info >/dev/null 2>&1; then
      SUDO_CMD="sudo"
    elif command -v sudo >/dev/null 2>&1; then
      # sudo exists but needs a password - prompt by invoking sudo once
      SUDO_CMD="sudo"
    else
      SUDO_CMD=""
    fi
  fi
  if [[ -n "$SUDO_CMD" ]]; then
    detail "Using ${BOLD}sudo${NC} for docker commands (override with --no-sudo)"
  else
    detail "Running docker without sudo"
  fi
}

# Wrapper that runs the command with the resolved sudo prefix, respecting
# --dry-run.  Uses an array so arguments with spaces/quotes survive.
docker_cmd() {
  if [[ $DRY_RUN -eq 1 ]]; then
    echo -e "    ${YELLOW}[dry]${NC} $SUDO_CMD docker $*"
    return 0
  fi
  if [[ -n "$SUDO_CMD" ]]; then
    "$SUDO_CMD" docker "$@"
  else
    docker "$@"
  fi
}

# ── Persistence helpers ──────────────────────────────────────────────────
# Wraps tools/persistence.py so the wave-1 audit + backup runs around every
# update. Stdlib-only on the Python side, so the host doesn't need the
# project venv activated.
have_python() { command -v python3 >/dev/null 2>&1; }

persistence_run() {
  # Run tools.persistence as a module; cwd matters because the tool reads
  # PROJECT_ROOT from its own __file__ location, but invoking it as
  # `python -m` requires the project root on sys.path either way.
  (cd "$PROJECT_ROOT" && python3 -m tools.persistence "$@")
}

snapshot_pre() {
  if [[ $DO_SNAPSHOT -eq 0 ]]; then return 0; fi
  if ! have_python; then
    warn "python3 not found - skipping pre-update snapshot"
    DO_SNAPSHOT=0
    return 0
  fi
  step "Recording pre-update snapshot of user data"
  if [[ $DRY_RUN -eq 1 ]]; then
    echo -e "    ${YELLOW}[dry]${NC} python3 -m tools.persistence snapshot --output ..."
    return 0
  fi
  mkdir -p "$BACKUP_DIR"
  local stamp; stamp="$(date -u +%Y%m%dT%H%M%SZ)"
  PRE_SNAP="$BACKUP_DIR/snapshot-pre-$stamp.json"
  if persistence_run snapshot --output "$PRE_SNAP" --quiet; then
    detail "Snapshot: $PRE_SNAP"
  else
    warn "Pre-update snapshot failed - proceeding anyway"
    PRE_SNAP=""
  fi
}

backup_userdata() {
  if [[ $DO_BACKUP -eq 0 ]]; then
    detail "Skipping backup (--no-backup)"
    return 0
  fi
  if ! have_python; then
    warn "python3 not found - skipping pre-update backup"
    return 0
  fi
  step "Backing up user data (tar.gz; pre-update safety net)"
  if [[ $DRY_RUN -eq 1 ]]; then
    echo -e "    ${YELLOW}[dry]${NC} python3 -m tools.persistence backup --output ..."
    return 0
  fi
  mkdir -p "$BACKUP_DIR"
  local stamp; stamp="$(date -u +%Y%m%dT%H%M%SZ)"
  local tarball="$BACKUP_DIR/speakesquery-userdata-$stamp.tar.gz"
  if ! persistence_run backup --output "$tarball" --quiet; then
    warn "Backup tarball failed - continuing without it"
    return 0
  fi
  detail "Backup: $tarball"
}

snapshot_post_and_diff() {
  if [[ $DO_SNAPSHOT -eq 0 ]]; then return 0; fi
  if [[ -z "$PRE_SNAP" || ! -f "$PRE_SNAP" ]]; then
    detail "No pre-update snapshot - skipping diff"
    return 0
  fi
  if [[ $DRY_RUN -eq 1 ]]; then
    return 0
  fi
  step "Verifying user-data persistence (post-update diff)"
  local stamp; stamp="$(date -u +%Y%m%dT%H%M%SZ)"
  local post="$BACKUP_DIR/snapshot-post-$stamp.json"
  if ! persistence_run snapshot --output "$post" --quiet; then
    warn "Post-update snapshot failed - cannot verify persistence"
    return 0
  fi
  if persistence_run diff --before "$PRE_SNAP" --after "$post"; then
    info "No regressions detected - all user data persisted across rebuild"
  else
    err "PERSISTENCE REGRESSION DETECTED - see diff above"
    err "Restore the most recent backup with:  ./update.sh --rollback"
  fi
}

rollback() {
  step "Rolling back from latest backup tarball"
  if ! have_python; then
    err "python3 not found - cannot run restore"
    exit 1
  fi
  if [[ ! -d "$BACKUP_DIR" ]]; then
    err "No backup dir at $BACKUP_DIR"
    exit 1
  fi
  local latest
  latest=$(ls -t "$BACKUP_DIR"/speakesquery-userdata-*.tar.gz 2>/dev/null | head -1)
  if [[ -z "$latest" ]]; then
    err "No backup tarballs in $BACKUP_DIR"
    exit 1
  fi
  detail "Restoring from: $latest"
  if [[ $DRY_RUN -eq 1 ]]; then
    echo -e "    ${YELLOW}[dry]${NC} python3 -m tools.persistence restore --tarball $latest --force --yes"
    return 0
  fi
  if ! persistence_run restore --tarball "$latest" --force --yes; then
    err "Restore failed - inspect $latest manually"
    exit 1
  fi
  info "Rollback complete. Container state was NOT touched - restart it"
  info "manually if it was offline:  ./update.sh"
}

# ── Pre-flight ───────────────────────────────────────────────────────────
preflight() {
  step "Pre-flight checks"

  if [[ ! -f "$INSTALL_SH" ]]; then
    err "install.sh not found at $INSTALL_SH"
    err "Are you running this from the project root?"
    exit 1
  fi
  if [[ ! -x "$INSTALL_SH" ]]; then
    warn "install.sh is not executable - fixing"
    chmod +x "$INSTALL_SH"
  fi
  detail "install.sh: ${GREEN}OK${NC}"

  if ! command -v docker >/dev/null 2>&1; then
    err "docker is not installed or not on PATH"
    err "See install.sh output for Docker install instructions"
    exit 1
  fi
  detail "docker binary: ${GREEN}OK${NC}"

  # Resolve sudo once so the rest of the script uses the same decision
  resolve_sudo

  if ! docker_cmd info >/dev/null 2>&1; then
    if [[ $DRY_RUN -eq 0 ]]; then
      err "Docker daemon is not reachable."
      err "Start Docker Desktop (macOS) or 'sudo systemctl start docker' (Linux)."
      exit 1
    fi
  fi
  detail "docker daemon: ${GREEN}reachable${NC}"

  if [[ $DO_PULL -eq 1 ]]; then
    # `.git` is a directory in a normal checkout but a FILE pointing to
    # the parent repo's gitdir in a worktree. Use git rev-parse so both
    # forms qualify as a git checkout.
    if ! git -C "$PROJECT_ROOT" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
      warn "--pull passed but $PROJECT_ROOT is not a git checkout; skipping"
      DO_PULL=0
    fi
  fi
}

# ── Git pull (opt-in) ────────────────────────────────────────────────────
git_pull() {
  if [[ $DO_PULL -eq 0 ]]; then
    return 0
  fi
  step "git pull --ff-only"
  if [[ $DRY_RUN -eq 1 ]]; then
    echo -e "    ${YELLOW}[dry]${NC} git -C $PROJECT_ROOT pull --ff-only"
    return 0
  fi
  if ! git -C "$PROJECT_ROOT" pull --ff-only; then
    err "git pull failed - aborting update (fix conflicts, then re-run)"
    exit 1
  fi
  info "Repository is at latest origin/$(git -C "$PROJECT_ROOT" rev-parse --abbrev-ref HEAD)"
}

# ── Container state ──────────────────────────────────────────────────────
show_container_state() {
  step "Current container state"
  local rows
  # shellcheck disable=SC2086
  if rows=$(docker_cmd ps -a --filter "name=^/${CONTAINER_NAME}$" \
    --format 'table {{.Names}}\t{{.Status}}\t{{.Image}}' 2>/dev/null); then
    echo "$rows"
  fi
}

# Returns 0 if container exists (running OR stopped), 1 otherwise.
container_exists() {
  if [[ $DRY_RUN -eq 1 ]]; then
    # Assume it exists so the dry-run shows both stop+rm steps
    return 0
  fi
  local found
  found=$(docker_cmd ps -a --filter "name=^/${CONTAINER_NAME}$" \
    --format '{{.Names}}' 2>/dev/null || true)
  [[ "$found" == "$CONTAINER_NAME" ]]
}

container_is_running() {
  if [[ $DRY_RUN -eq 1 ]]; then
    return 0
  fi
  local found
  found=$(docker_cmd ps --filter "name=^/${CONTAINER_NAME}$" \
    --format '{{.Names}}' 2>/dev/null || true)
  [[ "$found" == "$CONTAINER_NAME" ]]
}

# ── Stop + remove ────────────────────────────────────────────────────────
stop_and_remove() {
  if [[ $SKIP_STOP -eq 1 ]]; then
    warn "--skip-stop passed; leaving container alone"
    return 0
  fi

  step "Stopping + removing '$CONTAINER_NAME'"

  if ! container_exists; then
    info "No container named '$CONTAINER_NAME' - nothing to clean up"
    return 0
  fi

  if container_is_running; then
    detail "Container is running - sending stop signal (timeout 30s)"
    if [[ $DRY_RUN -eq 1 ]]; then
      # Don't swallow the [dry] trace, and don't claim "[OK] Stopped"
      # for a stop that never happened.
      docker_cmd stop --time 30 "$CONTAINER_NAME"
    elif ! docker_cmd stop --time 30 "$CONTAINER_NAME" >/dev/null; then
      warn "docker stop returned non-zero - attempting rm anyway"
    else
      info "Stopped '$CONTAINER_NAME'"
    fi
  else
    detail "Container exists but is not running"
  fi

  if [[ $DRY_RUN -eq 1 ]]; then
    docker_cmd rm "$CONTAINER_NAME"
    return 0
  fi
  if ! docker_cmd rm "$CONTAINER_NAME" >/dev/null; then
    err "docker rm failed - container may be in an inconsistent state"
    err "Investigate with:  docker inspect $CONTAINER_NAME"
    exit 3
  fi
  info "Removed '$CONTAINER_NAME'"
}

# ── Rebuild + start ──────────────────────────────────────────────────────
run_install() {
  step "Running install.sh ${INSTALL_ARGS[*]:-}"
  if [[ $DRY_RUN -eq 1 ]]; then
    echo -e "    ${YELLOW}[dry]${NC} cd $PROJECT_ROOT && ./install.sh ${INSTALL_ARGS[*]:-}"
    return 0
  fi
  if ! (cd "$PROJECT_ROOT" && "$INSTALL_SH" "${INSTALL_ARGS[@]}"); then
    err "install.sh returned non-zero"
    exit 3
  fi
}

# ── Post-run summary ─────────────────────────────────────────────────────
summary() {
  step "Post-update state"
  show_container_state
  echo ""
  if container_is_running; then
    info "SpeakesQuery update complete. Container '$CONTAINER_NAME' is running."
  elif [[ $DRY_RUN -eq 1 ]]; then
    info "Dry-run complete. No changes made."
  else
    warn "Container is NOT running after install.sh. Check its logs:"
    detail "  ${BOLD}${SUDO_CMD:+$SUDO_CMD }docker logs $CONTAINER_NAME${NC}"
  fi
}

# ── Main ─────────────────────────────────────────────────────────────────
if [[ $DO_ROLLBACK -eq 1 ]]; then
  banner
  rollback
  exit 0
fi

banner
preflight
git_pull
snapshot_pre
backup_userdata
show_container_state
stop_and_remove
run_install
snapshot_post_and_diff
summary
