#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

fail() {
  printf '[nico-install-test] FAIL: %s\n' "$*" >&2
  exit 1
}

assert_eq() {
  local actual="$1"
  local expected="$2"
  local label="$3"
  [[ "$actual" == "$expected" ]] || fail "$label: expected '$expected', got '$actual'"
}

assert_contains() {
  local value="$1"
  local expected="$2"
  local label="$3"
  [[ "$value" == *"$expected"* ]] || fail "$label: missing '$expected'"
}

export NICO_INSTALLER_SOURCE_ONLY=1
# shellcheck disable=SC1091
source "$ROOT_DIR/scripts/install.sh"
unset NICO_INSTALLER_SOURCE_ONLY

if grep -Eq '(mapfile|readarray)' \
  "$ROOT_DIR/scripts/install.sh" \
  "$ROOT_DIR/scripts/nico-service.sh" \
  "$ROOT_DIR/scripts/demo.sh" \
  "$ROOT_DIR/scripts/lib.sh"; then
  fail "release scripts use commands unavailable in macOS Bash 3.2"
fi

assert_eq "$(normalize_version 0.2.0)" "v0.2.0" "plain version normalization"
assert_eq "$(normalize_version v0.2.0-rc.1)" "v0.2.0-rc.1" "tag normalization"
compose_version_supported v2.24.4 || fail "minimum Compose version was rejected"
compose_version_supported 5.1.0 || fail "new Compose major version was rejected"
if compose_version_supported v2.24.3; then
  fail "unsupported Compose version was accepted"
fi
if normalize_version latest >/dev/null 2>&1; then
  fail "normalize_version accepted the latest alias"
fi
if normalize_version '../v0.2.0' >/dev/null 2>&1; then
  fail "normalize_version accepted an unsafe version"
fi
ORIGINAL_NICO_HOME="$NICO_HOME"
ORIGINAL_BIN_DIR="$BIN_DIR"
NICO_HOME=relative-nico-home
BIN_DIR=relative-bin-dir
resolve_install_paths
assert_eq "$NICO_HOME" "$ROOT_DIR/relative-nico-home" "relative home normalization"
assert_eq "$BIN_DIR" "$ROOT_DIR/relative-bin-dir" "relative bin normalization"
NICO_HOME="$ORIGINAL_NICO_HOME"
BIN_DIR="$ORIGINAL_BIN_DIR"

ENV_FILE="$TMP_DIR/deployment.env"
printf 'POSTGRES_PASSWORD=keep-me\nCUSTOM_VALUE=operator-owned\n' > "$ENV_FILE"
chmod 600 "$ENV_FILE"
set_env_value "$ENV_FILE" POSTGRES_PASSWORD replace-me preserve
set_env_value "$ENV_FILE" NICO_BACKEND_IMAGE ghcr.io/phlosy/nico-agent-backend:v0.2.0 replace
set_env_value "$ENV_FILE" NICO_RUNTIME native replace
assert_contains "$(cat "$ENV_FILE")" "POSTGRES_PASSWORD=keep-me" "existing secret preservation"
assert_contains "$(cat "$ENV_FILE")" "CUSTOM_VALUE=operator-owned" "unrelated environment preservation"
assert_contains "$(cat "$ENV_FILE")" \
  "NICO_BACKEND_IMAGE=ghcr.io/phlosy/nico-agent-backend:v0.2.0" \
  "image replacement"
assert_eq "$(stat -c '%a' "$ENV_FILE" 2>/dev/null || stat -f '%Lp' "$ENV_FILE")" "600" \
  "environment permissions"
if (set_env_value "$ENV_FILE" INVALID_VALUE $'first\nsecond' replace) >/dev/null 2>&1; then
  fail "environment writer accepted a multiline value"
fi
set_env_value "$ENV_FILE" NICO_RUNTIME hermes replace
RUNTIME=native
RUNTIME_EXPLICIT=false
resolve_existing_runtime "$ENV_FILE"
assert_eq "$RUNTIME" hermes "existing Runtime preservation"
set_env_value "$ENV_FILE" OPENROUTER_API_KEY stored-provider-secret replace
PROVIDER=openrouter
NON_INTERACTIVE=true
unset OPENROUTER_API_KEY
configure_provider_secret "$ENV_FILE"
assert_contains "$(cat "$ENV_FILE")" "OPENROUTER_API_KEY=stored-provider-secret" \
  "existing Provider secret reuse"
RUNTIME=native
RUNTIME_EXPLICIT=false
PROVIDER=""
NON_INTERACTIVE=false

printf 'verified content\n' > "$TMP_DIR/asset.tar.gz"
(
  cd "$TMP_DIR"
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum asset.tar.gz > SHA256SUMS
  else
    shasum -a 256 asset.tar.gz > SHA256SUMS
  fi
)
verify_release_asset "$TMP_DIR/asset.tar.gz" "$TMP_DIR/SHA256SUMS"
printf 'tampered\n' >> "$TMP_DIR/asset.tar.gz"
if verify_release_asset "$TMP_DIR/asset.tar.gz" "$TMP_DIR/SHA256SUMS" >/dev/null 2>&1; then
  fail "checksum verification accepted a modified asset"
fi

DRY_RUN="$({
  NICO_HOME="$TMP_DIR/nico-home" bash "$ROOT_DIR/scripts/install.sh" \
    --dry-run --runtime hermes --version v0.2.0 --no-start --non-interactive
} 2>&1)"
assert_contains "$DRY_RUN" "version=v0.2.0" "dry-run version"
assert_contains "$DRY_RUN" "runtime=hermes" "dry-run runtime"
assert_contains "$DRY_RUN" "start=false" "dry-run no-start"
if ! LOCAL_DRY_RUN="$({
  NICO_HOME="$TMP_DIR/local-nico-home" bash "$ROOT_DIR/scripts/install.sh" \
    --dry-run --local-images --version v0.2.0 --bundle "$TMP_DIR/unused-bundle.tar.gz" \
    --no-start --non-interactive
} 2>&1)"; then
  fail "installer rejected local image mode: $LOCAL_DRY_RUN"
fi
assert_contains "$LOCAL_DRY_RUN" "image_source=local" "dry-run local image source"
assert_contains "$LOCAL_DRY_RUN" \
  "compose_project=nico-agent-local-release" "dry-run local Compose project"
assert_contains "$LOCAL_DRY_RUN" "api_url=http://localhost:28000" "dry-run local API URL"
assert_contains "$LOCAL_DRY_RUN" "web_url=http://localhost:28080" "dry-run local Web URL"
if bash "$ROOT_DIR/scripts/install.sh" --dry-run --runtime invalid >/dev/null 2>&1; then
  fail "installer accepted an invalid runtime"
fi
NICO_HOME="$TMP_DIR/missing-install" bash "$ROOT_DIR/scripts/nico-service.sh" --help \
  >/dev/null || fail "service help required an existing installation"

LOCAL_ENV_FILE="$TMP_DIR/local-deployment.env"
LOCAL_IMAGES=true
RESOLVED_VERSION=v0.2.0
RUNTIME=native
prepare_environment "$LOCAL_ENV_FILE" "$ROOT_DIR/.env.example"
assert_contains "$(cat "$LOCAL_ENV_FILE")" \
  "NICO_BACKEND_IMAGE=nico-agent-backend:v0.2.0" "local backend image"
assert_contains "$(cat "$LOCAL_ENV_FILE")" \
  "NICO_HERMES_IMAGE=nico-agent-hermes:v0.2.0" "local Hermes image"
assert_contains "$(cat "$LOCAL_ENV_FILE")" \
  "NICO_WEB_IMAGE=nico-agent-web:v0.2.0" "local web image"
assert_contains "$(cat "$LOCAL_ENV_FILE")" "NICO_PULL_POLICY=never" "local pull policy"
assert_contains "$(cat "$LOCAL_ENV_FILE")" \
  "COMPOSE_PROJECT_NAME=nico-agent-local-release" "local Compose project isolation"
assert_contains "$(cat "$LOCAL_ENV_FILE")" "POSTGRES_PORT=25432" "local PostgreSQL port"
assert_contains "$(cat "$LOCAL_ENV_FILE")" "REDIS_PORT=26379" "local Redis port"
assert_contains "$(cat "$LOCAL_ENV_FILE")" "MINIO_API_PORT=29010" "local MinIO API port"
assert_contains "$(cat "$LOCAL_ENV_FILE")" \
  "MINIO_CONSOLE_PORT=29011" "local MinIO Console port"
assert_contains "$(cat "$LOCAL_ENV_FILE")" "API_PORT=28000" "local API port"
assert_contains "$(cat "$LOCAL_ENV_FILE")" "WEB_PORT=28080" "local Web port"
LOCAL_MODEL_SECRETS="$(env_value "$LOCAL_ENV_FILE" NICO_MODEL_SECRETS_FILE)"
[[ -f "$LOCAL_MODEL_SECRETS" ]] || fail "installer did not create the model secret file"
assert_eq "$(stat -c '%a' "$LOCAL_MODEL_SECRETS" 2>/dev/null || stat -f '%Lp' "$LOCAL_MODEL_SECRETS")" \
  "600" "model secret permissions"
LOCAL_IMAGES=false

REMOTE_ENV_FILE="$TMP_DIR/remote-deployment.env"
prepare_environment "$REMOTE_ENV_FILE" "$ROOT_DIR/.env.example"
assert_contains "$(cat "$REMOTE_ENV_FILE")" \
  "NICO_BACKEND_IMAGE=ghcr.io/phlosy/nico-agent-backend:v0.2.0" "remote backend image"
assert_contains "$(cat "$REMOTE_ENV_FILE")" "NICO_PULL_POLICY=always" "remote pull policy"
assert_contains "$(cat "$REMOTE_ENV_FILE")" \
  "COMPOSE_PROJECT_NAME=nico-agent-platform" "remote Compose project"
assert_contains "$(cat "$REMOTE_ENV_FILE")" "API_PORT=18000" "remote API port"

FAKE_WHEEL="$TMP_DIR/nico_agent_platform-0.2.0-py3-none-any.whl"
printf 'fake wheel for archive contract\n' > "$FAKE_WHEEL"
DIST_DIR="$TMP_DIR/dist"
mkdir -p "$DIST_DIR"
printf 'operator file\n' > "$DIST_DIR/keep.txt"
"$ROOT_DIR/scripts/package-release.sh" --tag v0.2.0 --wheel "$FAKE_WHEEL" --output "$DIST_DIR"
[[ -f "$DIST_DIR/keep.txt" ]] || fail "release packaging removed unrelated output"

for asset in install.sh nico-agent-bundle.tar.gz version.txt SHA256SUMS; do
  [[ -f "$DIST_DIR/$asset" ]] || fail "release packaging omitted $asset"
done
assert_eq "$(cat "$DIST_DIR/version.txt")" "v0.2.0" "release version metadata"

INSTALL_INPUT="$TMP_DIR/install-input"
mkdir -p "$INSTALL_INPUT"
cp "$DIST_DIR/nico-agent-bundle.tar.gz" "$INSTALL_INPUT/nico-agent-bundle.tar.gz"
ORIGINAL_NICO_HOME="$NICO_HOME"
NICO_HOME="$TMP_DIR/installed-nico"
RESOLVED_VERSION=v0.2.0
install_release_files "$INSTALL_INPUT"
printf 'preserve immutable release\n' > "$RELEASE_DIR/operator-sentinel"
install_release_files "$INSTALL_INPUT"
[[ -f "$RELEASE_DIR/operator-sentinel" ]] || fail \
  "same-version reinstall replaced the immutable release directory"
printf 'v9.9.9\n' > "$RELEASE_DIR/version.txt"
if (validate_release_directory "$RELEASE_DIR") >/dev/null 2>&1; then
  fail "release validation accepted mismatched internal version metadata"
fi
printf 'v0.2.0\n' > "$RELEASE_DIR/version.txt"
BUNDLE_RELEASE_DIR="$RELEASE_DIR"
NICO_HOME="$ORIGINAL_NICO_HOME"

UNSAFE_ARCHIVE="$TMP_DIR/unsafe.tar.gz"
python3 - "$UNSAFE_ARCHIVE" <<'PY'
import io
import tarfile
import sys

with tarfile.open(sys.argv[1], "w:gz") as archive:
    payload = b"unsafe\n"
    member = tarfile.TarInfo("nico-agent/../../escaped")
    member.size = len(payload)
    archive.addfile(member, io.BytesIO(payload))
PY
if extract_release_bundle "$UNSAFE_ARCHIVE" "$TMP_DIR/unsafe-output" >/dev/null 2>&1; then
  fail "release extraction accepted a path traversal member"
fi
(
  cd "$DIST_DIR"
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum --check SHA256SUMS >/dev/null
  else
    shasum -a 256 --check SHA256SUMS >/dev/null
  fi
)

tar -tzf "$DIST_DIR/nico-agent-bundle.tar.gz" > "$TMP_DIR/archive-members.txt"
printf '%s\n' \
  nico-agent/ \
  nico-agent/.env.example \
  nico-agent/docker-compose.yml \
  nico-agent/deploy/ \
  nico-agent/deploy/docker-compose.release.yml \
  nico-agent/docs/ \
  nico-agent/docs/installation.md \
  nico-agent/scripts/ \
  nico-agent/scripts/demo.sh \
  nico-agent/scripts/lib.sh \
  nico-agent/scripts/nico-service.sh \
  nico-agent/version.txt \
  nico-agent/wheels/ \
  "nico-agent/wheels/$(basename "$FAKE_WHEEL")" \
  > "$TMP_DIR/expected-archive-members.txt"
if ! diff -u \
  <(sort "$TMP_DIR/expected-archive-members.txt") \
  <(sort "$TMP_DIR/archive-members.txt"); then
  fail "release archive differs from the explicit allowlist"
fi

if "$ROOT_DIR/scripts/package-release.sh" \
  --tag v9.9.9 --wheel "$FAKE_WHEEL" --output "$TMP_DIR/bad-dist" >/dev/null 2>&1; then
  fail "release packaging accepted a tag/package version mismatch"
fi

FAKE_BIN="$TMP_DIR/fake-bin"
FAKE_DOCKER_LOG="$TMP_DIR/fake-docker.log"
mkdir -p "$FAKE_BIN"
printf '%s\n' \
  '#!/usr/bin/env bash' \
  'printf "%s\n" "$*" >> "$FAKE_DOCKER_LOG"' \
  'if [[ "$*" == "compose version --short" ]]; then printf "2.24.4\n"; fi' \
  'exit 0' \
  > "$FAKE_BIN/docker"
printf '%s\n' '#!/usr/bin/env bash' 'exit 0' > "$FAKE_BIN/curl"
chmod 755 "$FAKE_BIN/docker" "$FAKE_BIN/curl"

: > "$FAKE_DOCKER_LOG"
if FAKE_DOCKER_LOG="$FAKE_DOCKER_LOG" PATH="$FAKE_BIN:$PATH" \
  make --no-print-directory -C "$ROOT_DIR" release \
    TAG=v9.9.9 DOCKER=docker \
    RELEASE_DIR="$TMP_DIR/invalid-tag-release" \
    WHEEL_DIR="$TMP_DIR/invalid-tag-wheels" >/dev/null 2>&1; then
  fail "make release accepted a tag/package version mismatch"
fi
[[ ! -s "$FAKE_DOCKER_LOG" ]] || fail \
  "make release built images before validating the release version"

SERVICE_HOME="$TMP_DIR/service-home"
mkdir -p "$SERVICE_HOME/current" "$SERVICE_HOME/config"
printf 'NICO_RUNTIME=native\nNICO_PULL_POLICY=never\n' \
  > "$SERVICE_HOME/config/deployment.env"
FAKE_DOCKER_LOG="$FAKE_DOCKER_LOG" PATH="$FAKE_BIN:$PATH" NICO_HOME="$SERVICE_HOME" \
  "$ROOT_DIR/scripts/nico-service.sh" up >/dev/null
if grep -Eq '(^| )pull($| )' "$FAKE_DOCKER_LOG"; then
  fail "local service startup attempted to pull images"
fi
grep -q ' up --detach --no-build ' "$FAKE_DOCKER_LOG" || fail \
  "local service startup did not use the installed release Compose stack"

: > "$FAKE_DOCKER_LOG"
FAKE_DOCKER_LOG="$FAKE_DOCKER_LOG" PATH="$FAKE_BIN:$PATH" NICO_HOME="$SERVICE_HOME" \
  "$ROOT_DIR/scripts/nico-service.sh" down >/dev/null
grep -q ' down --remove-orphans' "$FAKE_DOCKER_LOG" || fail \
  "service shutdown did not use the installed release Compose stack"
if grep -q -- '--volumes' "$FAKE_DOCKER_LOG"; then
  fail "service shutdown removed Docker data volumes"
fi

MAKE_RELEASE_DIR="$TMP_DIR/make-release"
MAKE_INSTALL_LOG="$TMP_DIR/make-install.log"
mkdir -p "$MAKE_RELEASE_DIR"
printf '%s\n' \
  '#!/usr/bin/env bash' \
  'printf "%s\n" "$*" > "$MAKE_INSTALL_LOG"' \
  > "$MAKE_RELEASE_DIR/install.sh"
chmod 755 "$MAKE_RELEASE_DIR/install.sh"
printf 'bundle\n' > "$MAKE_RELEASE_DIR/nico-agent-bundle.tar.gz"
printf 'checksums\n' > "$MAKE_RELEASE_DIR/SHA256SUMS"
printf 'v0.2.0\n' > "$MAKE_RELEASE_DIR/version.txt"
: > "$FAKE_DOCKER_LOG"
MAKE_INSTALL_LOG="$MAKE_INSTALL_LOG" FAKE_DOCKER_LOG="$FAKE_DOCKER_LOG" \
  PATH="$FAKE_BIN:$PATH" make --no-print-directory -C "$ROOT_DIR" install \
    RELEASE_DIR="$MAKE_RELEASE_DIR" DOCKER=docker \
    NICO_HOME="$TMP_DIR/make-home" NICO_BIN_DIR="$TMP_DIR/make-bin" \
    INSTALL_ARGS='--no-start --non-interactive' >/dev/null
MAKE_ARGS="$(cat "$MAKE_INSTALL_LOG")"
assert_contains "$MAKE_ARGS" "--bundle $MAKE_RELEASE_DIR/nico-agent-bundle.tar.gz" \
  "make install local bundle"
assert_contains "$MAKE_ARGS" "--local-images" "make install local image mode"
assert_contains "$MAKE_ARGS" "--runtime native" "make install Runtime"
assert_eq "$(wc -l < "$FAKE_DOCKER_LOG" | tr -d '[:space:]')" "1" \
  "make install image reuse checks"

MISSING_RELEASE_DIR="$TMP_DIR/missing-make-release"
FAKE_SUBMAKE="$TMP_DIR/fake-submake"
FAKE_SUBMAKE_LOG="$TMP_DIR/fake-submake.log"
printf '%s\n' \
  '#!/usr/bin/env bash' \
  'printf "%s\n" "$*" > "$FAKE_SUBMAKE_LOG"' \
  'mkdir -p "$FAKE_RELEASE_DIR"' \
  'cp "$FAKE_INSTALL_TEMPLATE" "$FAKE_RELEASE_DIR/install.sh"' \
  'printf "bundle\n" > "$FAKE_RELEASE_DIR/nico-agent-bundle.tar.gz"' \
  'printf "checksums\n" > "$FAKE_RELEASE_DIR/SHA256SUMS"' \
  'printf "v0.2.0\n" > "$FAKE_RELEASE_DIR/version.txt"' \
  > "$FAKE_SUBMAKE"
chmod 755 "$FAKE_SUBMAKE"
FAKE_RELEASE_DIR="$MISSING_RELEASE_DIR" \
FAKE_INSTALL_TEMPLATE="$MAKE_RELEASE_DIR/install.sh" \
FAKE_SUBMAKE_LOG="$FAKE_SUBMAKE_LOG" MAKE_INSTALL_LOG="$MAKE_INSTALL_LOG" \
FAKE_DOCKER_LOG="$FAKE_DOCKER_LOG" PATH="$FAKE_BIN:$PATH" \
  make --no-print-directory -C "$ROOT_DIR" install \
    MAKE="$FAKE_SUBMAKE" RELEASE_DIR="$MISSING_RELEASE_DIR" DOCKER=docker \
    NICO_HOME="$TMP_DIR/generated-make-home" NICO_BIN_DIR="$TMP_DIR/generated-make-bin" \
    INSTALL_ARGS='--no-start --non-interactive' >/dev/null
assert_contains "$(cat "$FAKE_SUBMAKE_LOG")" "release" \
  "make install generates a missing release"
assert_contains "$(cat "$MAKE_INSTALL_LOG")" \
  "--bundle $MISSING_RELEASE_DIR/nico-agent-bundle.tar.gz" \
  "make install uses the generated bundle"

UNINSTALL_HOME="$TMP_DIR/uninstall-home"
UNINSTALL_BIN="$TMP_DIR/uninstall-bin"
UNINSTALL_LOG="$TMP_DIR/uninstall.log"
mkdir -p \
  "$UNINSTALL_HOME/bin" \
  "$UNINSTALL_HOME/config" \
  "$UNINSTALL_HOME/releases/v0.2.0" \
  "$UNINSTALL_BIN"
printf 'COMPOSE_PROJECT_NAME=nico-agent-local-release\n' \
  > "$UNINSTALL_HOME/config/deployment.env"
printf '%s\n' \
  '#!/usr/bin/env bash' \
  'printf "%s\n" "$*" > "$UNINSTALL_LOG"' \
  > "$UNINSTALL_HOME/bin/nico-service"
chmod 755 "$UNINSTALL_HOME/bin/nico-service"
ln -s "$UNINSTALL_HOME/releases/v0.2.0" "$UNINSTALL_HOME/current"
ln -s "$UNINSTALL_HOME/current/venv/bin/nico" "$UNINSTALL_BIN/nico"
ln -s "$UNINSTALL_HOME/bin/nico-service" "$UNINSTALL_BIN/nico-service"
printf 'preserve me\n' > "$UNINSTALL_BIN/operator-file"

UNINSTALL_LOG="$UNINSTALL_LOG" make --no-print-directory -C "$ROOT_DIR" uninstall \
  NICO_HOME="$UNINSTALL_HOME" NICO_BIN_DIR="$UNINSTALL_BIN" >/dev/null
assert_contains "$(cat "$UNINSTALL_LOG")" "down" "make uninstall stops installed services"
[[ ! -e "$UNINSTALL_HOME" ]] || fail "make uninstall retained the installation directory"
[[ ! -e "$UNINSTALL_BIN/nico" && ! -L "$UNINSTALL_BIN/nico" ]] || fail \
  "make uninstall retained the Nico CLI link"
[[ ! -e "$UNINSTALL_BIN/nico-service" && ! -L "$UNINSTALL_BIN/nico-service" ]] || fail \
  "make uninstall retained the Nico service link"
[[ -f "$UNINSTALL_BIN/operator-file" ]] || fail \
  "make uninstall removed an unrelated bin directory file"
printf 'operator command\n' > "$UNINSTALL_BIN/nico"
make --no-print-directory -C "$ROOT_DIR" uninstall \
  NICO_HOME="$UNINSTALL_HOME" NICO_BIN_DIR="$UNINSTALL_BIN" >/dev/null || fail \
  "make uninstall is not idempotent"
[[ -f "$UNINSTALL_BIN/nico" && ! -L "$UNINSTALL_BIN/nico" ]] || fail \
  "make uninstall removed a non-symlink Nico command"

UNSAFE_UNINSTALL_HOME="$TMP_DIR/not-a-nico-install"
mkdir -p "$UNSAFE_UNINSTALL_HOME"
printf 'operator data\n' > "$UNSAFE_UNINSTALL_HOME/sentinel"
if make --no-print-directory -C "$ROOT_DIR" uninstall \
  NICO_HOME="$UNSAFE_UNINSTALL_HOME" NICO_BIN_DIR="$UNINSTALL_BIN" \
  >/dev/null 2>&1; then
  fail "make uninstall removed an unrecognized directory"
fi
[[ -f "$UNSAFE_UNINSTALL_HOME/sentinel" ]] || fail \
  "make uninstall deleted an unrecognized directory"

command -v docker >/dev/null 2>&1 || fail "Docker is required for Compose contract tests"
docker compose version >/dev/null 2>&1 || fail "Docker Compose is required for contract tests"
COMPOSE=(docker compose --project-directory "$ROOT_DIR" --file "$ROOT_DIR/docker-compose.yml")
RELEASE_FILE="$ROOT_DIR/deploy/docker-compose.release.yml"
"${COMPOSE[@]}" config --quiet
"${COMPOSE[@]}" --file "$RELEASE_FILE" --profile native config --quiet
"${COMPOSE[@]}" --file "$RELEASE_FILE" --profile hermes config --quiet
NATIVE_CONFIG="$("${COMPOSE[@]}" --file "$RELEASE_FILE" --profile native config)"
HERMES_CONFIG="$("${COMPOSE[@]}" --file "$RELEASE_FILE" --profile hermes config)"
TEST_MODEL_SECRETS="$TMP_DIR/compose-model-secrets.env"
printf 'NICO_MODEL_SECRET_COMPOSE_CANARY=compose-canary-value\n' > "$TEST_MODEL_SECRETS"
chmod 600 "$TEST_MODEL_SECRETS"
NATIVE_SECRET_CONFIG="$(NICO_MODEL_SECRETS_FILE="$TEST_MODEL_SECRETS" \
  "${COMPOSE[@]}" --file "$RELEASE_FILE" --profile native config)"
HERMES_SECRET_CONFIG="$(NICO_MODEL_SECRETS_FILE="$TEST_MODEL_SECRETS" \
  "${COMPOSE[@]}" --file "$RELEASE_FILE" --profile hermes config)"
BUNDLE_CONFIG="$(docker compose \
  --project-directory "$BUNDLE_RELEASE_DIR" \
  --file "$BUNDLE_RELEASE_DIR/docker-compose.yml" \
  --file "$BUNDLE_RELEASE_DIR/deploy/docker-compose.release.yml" \
  --profile native config)"
if grep -Eq '^[[:space:]]{4}build:' <<< "$NATIVE_CONFIG$HERMES_CONFIG$BUNDLE_CONFIG"; then
  fail "release Compose retained a source build context"
fi
if grep -q 'OPENROUTER_API_KEY' <<< "$NATIVE_CONFIG"; then
  fail "native release profile received a Hermes Provider credential"
fi
grep -q 'OPENROUTER_API_KEY' <<< "$HERMES_CONFIG" || fail \
  "Hermes release profile omitted Provider credentials"
grep -q 'NICO_MODEL_SECRET_COMPOSE_CANARY' <<< "$NATIVE_SECRET_CONFIG" || fail \
  "Native Worker omitted the protected model-secret file"
if grep -q 'NICO_MODEL_SECRET_COMPOSE_CANARY' <<< "$HERMES_SECRET_CONFIG"; then
  fail "Hermes profile inherited the Native model-secret file"
fi
DEFAULT_SERVICES="$("${COMPOSE[@]}" config --services)"
NATIVE_SERVICES="$("${COMPOSE[@]}" --file "$RELEASE_FILE" --profile native config --services)"
HERMES_SERVICES="$("${COMPOSE[@]}" --file "$RELEASE_FILE" --profile hermes config --services)"
grep -Fxq worker <<< "$DEFAULT_SERVICES" || fail "source Compose default omitted worker"
if grep -Fxq worker-hermes <<< "$DEFAULT_SERVICES"; then
  fail "source Compose default included Hermes worker"
fi
grep -Fxq worker <<< "$NATIVE_SERVICES" || fail "native release profile omitted worker"
if grep -Fxq worker-hermes <<< "$NATIVE_SERVICES"; then
  fail "native release profile included Hermes worker"
fi
grep -Fxq worker-hermes <<< "$HERMES_SERVICES" || fail "Hermes profile omitted worker-hermes"
if grep -Fxq worker <<< "$HERMES_SERVICES"; then
  fail "Hermes release profile included native worker"
fi

python3 - "$ROOT_DIR" <<'PY'
from pathlib import Path
import re
import sys

root = Path(sys.argv[1])
ci = (root / ".github/workflows/ci.yml").read_text()
release = (root / ".github/workflows/release.yml").read_text()
pyproject = (root / "backend/pyproject.toml").read_text()

assert re.search(r"push:\s*\n\s+branches:\s*\[main\]", ci)
assert re.search(r"pull_request:\s*\n\s+branches:\s*\[main\]", ci)
assert "workflow_call:" in ci
assert "scripts/test.sh" in ci
assert "scripts/test-install.sh" in ci
assert "contents: write" not in ci
assert "packages: write" not in ci

assert 'tags: ["v*"]' in release
assert "branches:" not in release
assert "uses: ./.github/workflows/ci.yml" in release
assert "packages: write" in release
assert "contents: write" in release
assert "scripts/package-release.sh" in release
assert "gh release" in release
assert "--clobber" not in release
assert "actionlint/cmd/actionlint@v1.7.12" in ci
assert "!reset null" in (root / "deploy/docker-compose.release.yml").read_text()
assert "NICO_PULL_POLICY" in (root / "deploy/docker-compose.release.yml").read_text()

makefile = (root / "Makefile").read_text()
assert re.search(r"^release:", makefile, re.MULTILINE)
assert re.search(r"^install:", makefile, re.MULTILINE)
assert re.search(r"^uninstall:", makefile, re.MULTILINE)
assert "--local-images" in makefile
assert "scripts/package-release.sh" in makefile
assert "scripts/uninstall.sh" in makefile
assert '"prompt-toolkit==3.0.52"' in pyproject
assert '"rich==14.3.3"' in pyproject

for workflow in (ci, release):
    for action in re.findall(r"uses:\s+([^\s#]+)", workflow):
        if action.startswith("./"):
            continue
        assert re.search(r"@[0-9a-f]{40}$", action), f"unpinned action: {action}"
PY

printf '[nico-install-test] all installer and release contracts passed\n'
