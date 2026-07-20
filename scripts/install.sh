#!/usr/bin/env bash

set -euo pipefail

REPOSITORY="${NICO_GITHUB_REPOSITORY:-Phlosy/Nico-agent}"
NICO_HOME="${NICO_HOME:-$HOME/.nico}"
BIN_DIR="${NICO_BIN_DIR:-$HOME/.local/bin}"
REQUESTED_VERSION="latest"
RUNTIME="native"
RUNTIME_EXPLICIT=false
PROVIDER=""
START_SERVICES=true
RUN_DEMO=true
NON_INTERACTIVE=false
DRY_RUN=false
LOCAL_BUNDLE=""
LOCAL_IMAGES=false
LOCAL_COMPOSE_PROJECT_NAME="${NICO_LOCAL_COMPOSE_PROJECT_NAME:-nico-agent-local-release}"
LOCAL_POSTGRES_PORT="${NICO_LOCAL_POSTGRES_PORT:-25432}"
LOCAL_REDIS_PORT="${NICO_LOCAL_REDIS_PORT:-26379}"
LOCAL_MINIO_API_PORT="${NICO_LOCAL_MINIO_API_PORT:-29010}"
LOCAL_MINIO_CONSOLE_PORT="${NICO_LOCAL_MINIO_CONSOLE_PORT:-29011}"
LOCAL_API_PORT="${NICO_LOCAL_API_PORT:-28000}"
LOCAL_WEB_PORT="${NICO_LOCAL_WEB_PORT:-28080}"
INSTALL_TEMPORARY=""

log() {
  printf '[nico-install] %s\n' "$*"
}

die() {
  printf '[nico-install] ERROR: %s\n' "$*" >&2
  exit 1
}

cleanup() {
  if [[ -n "$INSTALL_TEMPORARY" && -d "$INSTALL_TEMPORARY" ]]; then
    rm -rf -- "$INSTALL_TEMPORARY"
  fi
}

usage() {
  cat <<'EOF'
Nico Agent installer

Usage:
  install.sh [options]

Options:
  --version VERSION       Install a release such as v0.2.0 (default: latest)
  --runtime RUNTIME       native or hermes (default: native)
  --provider PROVIDER     openrouter, openai, or anthropic for Hermes
  --dir PATH              Nico data and release directory (default: ~/.nico)
  --bin-dir PATH          Command link directory (default: ~/.local/bin)
  --bundle PATH           Install a local release bundle instead of downloading
  --local-images          Use locally built versioned images and never pull GHCR
  --no-start              Install files and CLI without starting services
  --skip-demo             Start services without bootstrapping Demo resources
  --non-interactive       Never prompt; fail when requested input is unavailable
  --dry-run               Validate options and print the resolved configuration
  -h, --help              Show this help

Provider credentials are read from OPENROUTER_API_KEY, OPENAI_API_KEY, or
ANTHROPIC_API_KEY. The installer never accepts credentials as command arguments.
EOF
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || die "required command not found: $1"
}

normalize_version() {
  local value="$1"
  [[ "$value" == v* ]] || value="v$value"
  [[ "$value" =~ ^v[0-9]+\.[0-9]+\.[0-9]+([.-][0-9A-Za-z][0-9A-Za-z.-]*)?$ ]] || return 1
  printf '%s\n' "$value"
}

parse_args() {
  while (($#)); do
    case "$1" in
      --version)
        (($# >= 2)) || die "--version requires a value"
        REQUESTED_VERSION="$2"
        shift 2
        ;;
      --runtime)
        (($# >= 2)) || die "--runtime requires a value"
        RUNTIME="$2"
        RUNTIME_EXPLICIT=true
        shift 2
        ;;
      --provider)
        (($# >= 2)) || die "--provider requires a value"
        PROVIDER="$2"
        shift 2
        ;;
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
      --bundle)
        (($# >= 2)) || die "--bundle requires a value"
        LOCAL_BUNDLE="$2"
        shift 2
        ;;
      --local-images)
        LOCAL_IMAGES=true
        shift
        ;;
      --no-start)
        START_SERVICES=false
        shift
        ;;
      --skip-demo)
        RUN_DEMO=false
        shift
        ;;
      --non-interactive)
        NON_INTERACTIVE=true
        shift
        ;;
      --dry-run)
        DRY_RUN=true
        shift
        ;;
      -h | --help)
        usage
        exit 0
        ;;
      *)
        die "unknown option: $1"
        ;;
    esac
  done

  case "$RUNTIME" in
    native | hermes) ;;
    *) die "unsupported runtime '$RUNTIME'; expected native or hermes" ;;
  esac
  case "$PROVIDER" in
    "" | openrouter | openai | anthropic) ;;
    *) die "unsupported provider '$PROVIDER'; expected openrouter, openai, or anthropic" ;;
  esac
  if [[ -n "$PROVIDER" && "$RUNTIME" != "hermes" ]]; then
    die "--provider is valid only with --runtime hermes"
  fi
  if [[ "$REQUESTED_VERSION" != "latest" ]]; then
    REQUESTED_VERSION="$(normalize_version "$REQUESTED_VERSION")" || die \
      "invalid version '$REQUESTED_VERSION'; expected vX.Y.Z"
  fi
  if [[ -n "$LOCAL_BUNDLE" && "$REQUESTED_VERSION" == "latest" ]]; then
    die "--bundle requires an explicit --version"
  fi
  if [[ "$LOCAL_IMAGES" == true && -z "$LOCAL_BUNDLE" ]]; then
    die "--local-images requires --bundle"
  fi
}

set_env_value() {
  local file="$1"
  local key="$2"
  local value="$3"
  local mode="${4:-replace}"
  [[ "$key" =~ ^[A-Z][A-Z0-9_]*$ ]] || die "invalid environment key: $key"
  [[ "$mode" == "replace" || "$mode" == "preserve" ]] || die "invalid env update mode"
  [[ "$value" != *$'\n'* && "$value" != *$'\r'* ]] || die \
    "environment value for $key must be a single line"

  if [[ "$mode" == "preserve" ]] && grep -q "^${key}=" "$file" 2>/dev/null; then
    chmod 600 "$file"
    return 0
  fi

  local temporary
  temporary="$(mktemp "${file}.XXXXXX")"
  local found=false
  if [[ -f "$file" ]]; then
    while IFS= read -r line || [[ -n "$line" ]]; do
      if [[ "$line" == "${key}="* ]]; then
        if [[ "$found" == false ]]; then
          printf '%s=%s\n' "$key" "$value" >> "$temporary"
          found=true
        fi
      else
        printf '%s\n' "$line" >> "$temporary"
      fi
    done < "$file"
  fi
  if [[ "$found" == false ]]; then
    printf '%s=%s\n' "$key" "$value" >> "$temporary"
  fi
  chmod 600 "$temporary"
  mv "$temporary" "$file"
}

env_value() {
  local file="$1"
  local key="$2"
  local fallback="${3:-}"
  local line
  line="$(grep -E "^${key}=" "$file" 2>/dev/null | tail -n 1 || true)"
  if [[ -n "$line" ]]; then
    printf '%s\n' "${line#*=}"
  else
    printf '%s\n' "$fallback"
  fi
}

random_secret() {
  python3 -c 'import secrets; print(secrets.token_urlsafe(32))'
}

sha256_file() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" | awk '{print $1}'
  else
    shasum -a 256 "$1" | awk '{print $1}'
  fi
}

verify_release_asset() {
  local asset="$1"
  local manifest="$2"
  local name expected actual
  name="$(basename "$asset")"
  expected="$(awk -v name="$name" '$2 == name || $2 == "*" name {print $1; exit}' "$manifest")"
  [[ -n "$expected" ]] || return 1
  actual="$(sha256_file "$asset")"
  [[ "$actual" == "$expected" ]]
}

download_file() {
  local url="$1"
  local destination="$2"
  curl --fail --silent --show-error --location --retry 3 --retry-delay 2 \
    --connect-timeout 10 --max-time 300 --output "$destination" "$url"
}

check_python() {
  require_command python3
  python3 - <<'PY' || die "Python 3.11 or newer is required"
import sys
raise SystemExit(0 if sys.version_info >= (3, 11) else 1)
PY
}

absolute_path() {
  python3 - "$1" <<'PY'
from pathlib import Path
import sys

print(Path(sys.argv[1]).expanduser().resolve())
PY
}

resolve_install_paths() {
  NICO_HOME="$(absolute_path "$NICO_HOME")"
  BIN_DIR="$(absolute_path "$BIN_DIR")"
  [[ "$NICO_HOME" != / ]] || die "--dir cannot be the filesystem root"
  [[ "$BIN_DIR" != / ]] || die "--bin-dir cannot be the filesystem root"
}

compose_version_supported() {
  local version="${1#v}"
  version="${version%%-*}"
  local major minor patch
  IFS=. read -r major minor patch <<< "$version"
  [[ "$major" =~ ^[0-9]+$ && "$minor" =~ ^[0-9]+$ && "$patch" =~ ^[0-9]+$ ]] || return 1
  ((major > 2)) || ((major == 2 && (minor > 24 || (minor == 24 && patch >= 4))))
}

check_docker() {
  require_command docker
  docker compose version >/dev/null 2>&1 || die "Docker Compose v2 is required"
  local compose_version
  compose_version="$(docker compose version --short 2>/dev/null)" || die \
    "could not determine the Docker Compose version"
  compose_version_supported "$compose_version" || die \
    "Docker Compose 2.24.4 or newer is required (found $compose_version)"
  docker info >/dev/null 2>&1 || die "Docker daemon is unavailable or not accessible"
}

provider_env_name() {
  case "$1" in
    openrouter) printf 'OPENROUTER_API_KEY\n' ;;
    openai) printf 'OPENAI_API_KEY\n' ;;
    anthropic) printf 'ANTHROPIC_API_KEY\n' ;;
    *) return 1 ;;
  esac
}

choose_hermes_provider() {
  [[ "$RUNTIME" == "hermes" && "$RUNTIME_EXPLICIT" == true ]] || return 0
  if [[ -z "$PROVIDER" && "$NON_INTERACTIVE" == false && -r /dev/tty ]]; then
    local answer
    printf 'Hermes provider [openrouter/openai/anthropic/skip] (skip): ' > /dev/tty
    IFS= read -r answer < /dev/tty || answer=""
    case "$answer" in
      "" | skip) PROVIDER="" ;;
      openrouter | openai | anthropic) PROVIDER="$answer" ;;
      *) die "unsupported Hermes provider: $answer" ;;
    esac
  fi
}

resolve_existing_runtime() {
  local env_file="$1"
  [[ "$RUNTIME_EXPLICIT" == false && -f "$env_file" ]] || return 0
  local existing
  existing="$(env_value "$env_file" NICO_RUNTIME)"
  case "$existing" in
    native | hermes) RUNTIME="$existing" ;;
    "") ;;
    *) die "existing NICO_RUNTIME must be native or hermes in $env_file" ;;
  esac
}

configure_provider_secret() {
  local env_file="$1"
  [[ -n "$PROVIDER" ]] || return 0
  local variable value
  variable="$(provider_env_name "$PROVIDER")"
  value="${!variable:-}"
  if [[ -z "$value" ]]; then
    value="$(env_value "$env_file" "$variable")"
  fi
  if [[ -z "$value" && "$NON_INTERACTIVE" == false && -r /dev/tty ]]; then
    printf '%s: ' "$variable" > /dev/tty
    IFS= read -r -s value < /dev/tty || value=""
    printf '\n' > /dev/tty
  fi
  [[ -n "$value" ]] || die "$variable is required for --provider $PROVIDER"
  set_env_value "$env_file" "$variable" "$value" replace
  unset value
}

prepare_environment() {
  local env_file="$1"
  local template="$2"
  install -d -m 700 "$(dirname "$env_file")"
  if [[ ! -f "$env_file" ]]; then
    cp "$template" "$env_file"
    chmod 600 "$env_file"
  fi

  local current
  current="$(env_value "$env_file" POSTGRES_PASSWORD)"
  if [[ -z "$current" || "$current" == "nico-change-me" ]]; then
    set_env_value "$env_file" POSTGRES_PASSWORD "$(random_secret)" replace
  fi
  current="$(env_value "$env_file" MINIO_ROOT_PASSWORD)"
  if [[ -z "$current" || "$current" == "nico-minio-change-me" ]]; then
    set_env_value "$env_file" MINIO_ROOT_PASSWORD "$(random_secret)" replace
  fi
  current="$(env_value "$env_file" NICO_SANDBOX_RUNNER_TOKEN)"
  if [[ -z "$current" || "$current" == "nico-sandbox-development-token" ]]; then
    set_env_value "$env_file" NICO_SANDBOX_RUNNER_TOKEN "$(random_secret)" replace
  fi

  set_env_value "$env_file" NICO_RUNTIME "$RUNTIME" replace
  local image_prefix pull_policy
  if [[ "$LOCAL_IMAGES" == true ]]; then
    image_prefix="nico-agent"
    pull_policy=never
    set_env_value "$env_file" COMPOSE_PROJECT_NAME "$LOCAL_COMPOSE_PROJECT_NAME" replace
    set_env_value "$env_file" POSTGRES_PORT "$LOCAL_POSTGRES_PORT" replace
    set_env_value "$env_file" REDIS_PORT "$LOCAL_REDIS_PORT" replace
    set_env_value "$env_file" MINIO_API_PORT "$LOCAL_MINIO_API_PORT" replace
    set_env_value "$env_file" MINIO_CONSOLE_PORT "$LOCAL_MINIO_CONSOLE_PORT" replace
    set_env_value "$env_file" API_PORT "$LOCAL_API_PORT" replace
    set_env_value "$env_file" WEB_PORT "$LOCAL_WEB_PORT" replace
  else
    image_prefix="ghcr.io/phlosy/nico-agent"
    pull_policy=always
  fi
  set_env_value "$env_file" NICO_BACKEND_IMAGE \
    "${image_prefix}-backend:${RESOLVED_VERSION}" replace
  set_env_value "$env_file" NICO_HERMES_IMAGE \
    "${image_prefix}-hermes:${RESOLVED_VERSION}" replace
  set_env_value "$env_file" NICO_WEB_IMAGE \
    "${image_prefix}-web:${RESOLVED_VERSION}" replace
  set_env_value "$env_file" NICO_PULL_POLICY "$pull_policy" replace
  configure_provider_secret "$env_file"
  chmod 600 "$env_file"
}

resolve_release() {
  local temporary="$1"
  if [[ "$REQUESTED_VERSION" == "latest" ]]; then
    local latest_base="https://github.com/${REPOSITORY}/releases/latest/download"
    download_file "$latest_base/version.txt" "$temporary/version.txt"
    RESOLVED_VERSION="$(tr -d '[:space:]' < "$temporary/version.txt")"
    RESOLVED_VERSION="$(normalize_version "$RESOLVED_VERSION")" || die \
      "latest release returned invalid version metadata"
  else
    RESOLVED_VERSION="$REQUESTED_VERSION"
  fi

  if [[ -n "$LOCAL_BUNDLE" ]]; then
    [[ -f "$LOCAL_BUNDLE" ]] || die "local bundle not found: $LOCAL_BUNDLE"
    cp "$LOCAL_BUNDLE" "$temporary/nico-agent-bundle.tar.gz"
    local local_manifest="$(dirname "$LOCAL_BUNDLE")/SHA256SUMS"
    [[ -f "$local_manifest" ]] || die "local bundle requires adjacent SHA256SUMS"
    cp "$local_manifest" "$temporary/SHA256SUMS"
  else
    local base="https://github.com/${REPOSITORY}/releases/download/${RESOLVED_VERSION}"
    download_file "$base/nico-agent-bundle.tar.gz" "$temporary/nico-agent-bundle.tar.gz"
    download_file "$base/SHA256SUMS" "$temporary/SHA256SUMS"
  fi
  verify_release_asset "$temporary/nico-agent-bundle.tar.gz" "$temporary/SHA256SUMS" || die \
    "release bundle checksum verification failed"
}

validate_release_directory() {
  local directory="$1"
  for required in docker-compose.yml deploy/docker-compose.release.yml .env.example \
    scripts/demo.sh scripts/lib.sh scripts/nico-service.sh version.txt; do
    [[ -e "$directory/$required" ]] || die "release bundle is missing $required"
  done
  compgen -G "$directory/wheels/*.whl" >/dev/null || die \
    "release bundle contains no CLI wheel"
  local bundled_version
  bundled_version="$(tr -d '[:space:]' < "$directory/version.txt")"
  [[ "$bundled_version" == "$RESOLVED_VERSION" ]] || die \
    "release bundle version $bundled_version differs from $RESOLVED_VERSION"
}

extract_release_bundle() {
  local archive="$1"
  local destination="$2"
  python3 - "$archive" "$destination" <<'PY'
from pathlib import Path, PurePosixPath
import inspect
import sys
import tarfile

archive = Path(sys.argv[1])
destination = Path(sys.argv[2]).resolve()
with tarfile.open(archive, "r:gz") as source:
    members = source.getmembers()
    for member in members:
        path = PurePosixPath(member.name)
        if (
            path.is_absolute()
            or not path.parts
            or path.parts[0] != "nico-agent"
            or any(part in {"", ".", ".."} for part in path.parts)
            or not (member.isfile() or member.isdir())
        ):
            raise SystemExit(f"unsafe release archive member: {member.name}")
        target = (destination / member.name).resolve()
        if destination not in target.parents and target != destination:
            raise SystemExit(f"release archive member escapes destination: {member.name}")
    options = {"filter": "fully_trusted"} if "filter" in inspect.signature(source.extractall).parameters else {}
    source.extractall(destination, members=members, **options)
PY
}

install_release_files() {
  local temporary="$1"
  local releases_dir="$NICO_HOME/releases"
  local release_dir="$releases_dir/$RESOLVED_VERSION"
  local staged="$releases_dir/.${RESOLVED_VERSION}.tmp.$$"
  install -d -m 700 "$NICO_HOME" "$releases_dir"
  extract_release_bundle "$temporary/nico-agent-bundle.tar.gz" "$temporary"
  local source="$temporary/nico-agent"
  validate_release_directory "$source"

  if [[ -e "$release_dir" || -L "$release_dir" ]]; then
    [[ -d "$release_dir" && ! -L "$release_dir" ]] || die \
      "existing release path is not a directory: $release_dir"
    validate_release_directory "$release_dir"
    RELEASE_DIR="$release_dir"
    log "reusing installed immutable release $RESOLVED_VERSION"
    return 0
  fi

  rm -rf "$staged"
  install -d -m 700 "$staged"
  cp -R "$source/." "$staged/"
  mv "$staged" "$release_dir"
  RELEASE_DIR="$release_dir"
}

install_cli() {
  local wheel
  wheel="$(find "$RELEASE_DIR/wheels" -maxdepth 1 -type f -name '*.whl' -print -quit)"
  if [[ ! -x "$RELEASE_DIR/venv/bin/python" ]]; then
    python3 -m venv "$RELEASE_DIR/venv"
  fi
  "$RELEASE_DIR/venv/bin/python" -m pip install --disable-pip-version-check \
    --upgrade --force-reinstall "$wheel"
  install -d -m 700 "$NICO_HOME/bin"
  install -m 755 "$RELEASE_DIR/scripts/nico-service.sh" "$NICO_HOME/bin/nico-service"
  install -d "$BIN_DIR"
}

switch_current_release() {
  local env_file="$NICO_HOME/config/deployment.env"
  for command_link in "$BIN_DIR/nico" "$BIN_DIR/nico-service"; do
    [[ ! -e "$command_link" || -L "$command_link" ]] || die \
      "refusing to replace non-symlink command: $command_link"
  done
  [[ ! -e "$NICO_HOME/current" || -L "$NICO_HOME/current" ]] || die \
    "refusing to replace non-symlink current path: $NICO_HOME/current"
  rm -f "$RELEASE_DIR/.env"
  ln -s "$env_file" "$RELEASE_DIR/.env"
  ln -sfn "$RELEASE_DIR" "$NICO_HOME/current"
  ln -sfn "$NICO_HOME/current/venv/bin/nico" "$BIN_DIR/nico"
  ln -sfn "$NICO_HOME/bin/nico-service" "$BIN_DIR/nico-service"
}

bootstrap_cli_profile() {
  local state_file="$NICO_HOME/state/demo-state.json"
  NICO_HOME="$NICO_HOME" NICO_DEMO_STATE_DIR="$NICO_HOME/state" \
    "$RELEASE_DIR/scripts/demo.sh"
  local tenant project agent version
  read -r tenant project agent version < <(
    python3 - "$state_file" <<'PY'
import json
import sys

payload = json.load(open(sys.argv[1]))
print(payload["tenant_id"], payload["project_id"], payload["agent_id"], payload["agent_version_id"])
PY
  )
  "$RELEASE_DIR/venv/bin/nico" config set local \
    --api-url "http://localhost:$(env_value "$NICO_HOME/config/deployment.env" API_PORT 18000)" \
    --tenant-id "$tenant" --actor-id local-operator
  "$RELEASE_DIR/venv/bin/nico" config use local
  "$RELEASE_DIR/venv/bin/nico" doctor
  printf '\n[nico-install] Ready to chat:\n'
  printf '  nico chat --project %s --agent %s --version %s\n' "$project" "$agent" "$version"
}

main() {
  parse_args "$@"
  check_python
  resolve_install_paths
  if [[ "$DRY_RUN" == true ]]; then
    printf 'version=%s\n' "$REQUESTED_VERSION"
    printf 'runtime=%s\n' "$RUNTIME"
    printf 'provider=%s\n' "${PROVIDER:-none}"
    if [[ "$LOCAL_IMAGES" == true ]]; then
      printf 'image_source=local\n'
      printf 'compose_project=%s\n' "$LOCAL_COMPOSE_PROJECT_NAME"
      printf 'api_url=http://localhost:%s\n' "$LOCAL_API_PORT"
      printf 'web_url=http://localhost:%s\n' "$LOCAL_WEB_PORT"
    else
      printf 'image_source=ghcr\n'
    fi
    printf 'home=%s\n' "$NICO_HOME"
    printf 'bin_dir=%s\n' "$BIN_DIR"
    printf 'start=%s\n' "$START_SERVICES"
    printf 'demo=%s\n' "$RUN_DEMO"
    return 0
  fi

  require_command curl
  check_docker
  resolve_existing_runtime "$NICO_HOME/config/deployment.env"
  choose_hermes_provider

  INSTALL_TEMPORARY="$(mktemp -d "${TMPDIR:-/tmp}/nico-install.XXXXXX")"
  trap cleanup EXIT
  log "resolving Nico release"
  resolve_release "$INSTALL_TEMPORARY"
  log "verified release $RESOLVED_VERSION"
  install_release_files "$INSTALL_TEMPORARY"
  install_cli
  prepare_environment "$NICO_HOME/config/deployment.env" "$RELEASE_DIR/.env.example"
  switch_current_release

  if [[ "$START_SERVICES" == true ]]; then
    log "starting the $RUNTIME runtime profile"
    NICO_HOME="$NICO_HOME" "$NICO_HOME/bin/nico-service" up
    if [[ "$RUN_DEMO" == true ]]; then
      bootstrap_cli_profile
    fi
  fi

  log "installed Nico $RESOLVED_VERSION"
  if [[ ":$PATH:" != *":$BIN_DIR:"* ]]; then
    log "add $BIN_DIR to PATH before running nico or nico-service"
  fi
}

if [[ "${NICO_INSTALLER_SOURCE_ONLY:-0}" != "1" ]]; then
  main "$@"
fi
