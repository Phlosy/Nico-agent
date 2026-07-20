#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
NICO_HOME="${NICO_HOME:-$HOME/.nico}"
BIN_DIR="${NICO_BIN_DIR:-$HOME/.local/bin}"

log() {
  printf '[nico-uninstall] %s\n' "$*"
}

die() {
  printf '[nico-uninstall] ERROR: %s\n' "$*" >&2
  exit 1
}

usage() {
  cat <<'EOF'
Usage: uninstall.sh [options]

Options:
  --dir PATH      Nico data and release directory (default: ~/.nico)
  --bin-dir PATH  Command link directory (default: ~/.local/bin)
  -h, --help      Show this help

Stops installed services and removes Nico program files. Docker data volumes
are preserved. Run `nico-service purge --yes` before uninstalling to delete data.
EOF
}

parse_args() {
  while (($#)); do
    case "$1" in
      --dir)
        (($# >= 2)) || die "--dir requires a value"
        NICO_HOME="$2"
        shift 2
        ;;
      --bin-dir)
        (($# >= 2)) || die "--bin-dir requires a value"
        BIN_DIR="$2"
        shift 2
        ;;
      -h | --help)
        usage
        exit 0
        ;;
      *) die "unknown option: $1" ;;
    esac
  done
}

absolute_path() {
  python3 - "$1" <<'PY'
from pathlib import Path
import sys

print(Path(sys.argv[1]).expanduser().resolve())
PY
}

resolve_paths() {
  command -v python3 >/dev/null 2>&1 || die "python3 is required"
  NICO_HOME="$(absolute_path "$NICO_HOME")"
  BIN_DIR="$(absolute_path "$BIN_DIR")"
  local home
  home="$(absolute_path "$HOME")"
  [[ "$NICO_HOME" != / && "$NICO_HOME" != "$home" && "$NICO_HOME" != "$ROOT_DIR" ]] || die \
    "refusing unsafe installation directory: $NICO_HOME"
  [[ "$BIN_DIR" != / && "$BIN_DIR" != "$home" ]] || die \
    "refusing unsafe command directory: $BIN_DIR"
}

validate_installation() {
  [[ -L "$NICO_HOME/current" ]] || die \
    "refusing to remove unrecognized directory (missing current link): $NICO_HOME"
  [[ -f "$NICO_HOME/config/deployment.env" ]] || die \
    "refusing to remove unrecognized directory (missing deployment config): $NICO_HOME"
  [[ -x "$NICO_HOME/bin/nico-service" ]] || die \
    "refusing to remove unrecognized directory (missing service command): $NICO_HOME"
}

remove_owned_link() {
  local link="$1"
  [[ -L "$link" ]] || {
    [[ ! -e "$link" ]] || log "preserving non-symlink command: $link"
    return 0
  }

  local target
  target="$(absolute_path "$link")"
  case "$target" in
    "$NICO_HOME" | "$NICO_HOME"/*)
      rm -f -- "$link"
      ;;
    *) log "preserving command link outside $NICO_HOME: $link" ;;
  esac
}

main() {
  parse_args "$@"
  resolve_paths

  if [[ -e "$NICO_HOME" ]]; then
    [[ -d "$NICO_HOME" ]] || die "installation path is not a directory: $NICO_HOME"
    validate_installation
    log "stopping installed services"
    NICO_HOME="$NICO_HOME" "$NICO_HOME/bin/nico-service" down
  fi

  remove_owned_link "$BIN_DIR/nico"
  remove_owned_link "$BIN_DIR/nico-service"

  if [[ -d "$NICO_HOME" ]]; then
    rm -rf -- "$NICO_HOME"
    log "removed Nico program files from $NICO_HOME"
  else
    log "Nico is already uninstalled from $NICO_HOME"
  fi
  log "Docker data volumes were preserved"
}

main "$@"
