#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TAG=""
WHEEL=""
OUTPUT="$ROOT_DIR/dist/release"
VALIDATE_ONLY=false

die() {
  printf '[nico-release] ERROR: %s\n' "$*" >&2
  exit 1
}

usage() {
  cat <<'EOF'
Usage: package-release.sh --tag vX.Y.Z [--validate-only]
       package-release.sh --tag vX.Y.Z --wheel PATH [--output PATH]

Assembles the constant-name installer assets uploaded by the Tag Release workflow.
EOF
}

while (($#)); do
  case "$1" in
    --tag)
      (($# >= 2)) || die "--tag requires a value"
      TAG="$2"
      shift 2
      ;;
    --wheel)
      (($# >= 2)) || die "--wheel requires a value"
      WHEEL="$2"
      shift 2
      ;;
    --output)
      (($# >= 2)) || die "--output requires a value"
      OUTPUT="$2"
      shift 2
      ;;
    --validate-only)
      VALIDATE_ONLY=true
      shift
      ;;
    -h | --help)
      usage
      exit 0
      ;;
    *) die "unknown option: $1" ;;
  esac
done

[[ "$TAG" =~ ^v[0-9]+\.[0-9]+\.[0-9]+([.-][0-9A-Za-z][0-9A-Za-z.-]*)?$ ]] || die \
  "--tag must use vX.Y.Z format"
"$ROOT_DIR/scripts/version.sh" check-tag "$TAG" >/dev/null
PACKAGE_VERSION="${TAG#v}"
if [[ "$VALIDATE_ONLY" == true ]]; then
  printf '[nico-release] validated %s\n' "$TAG"
  exit 0
fi

[[ -f "$WHEEL" ]] || die "wheel not found: $WHEEL"
[[ "$(basename "$WHEEL")" == *"-${PACKAGE_VERSION}-"* ]] || die \
  "wheel filename does not contain package version $PACKAGE_VERSION"

for required in \
  docker-compose.yml \
  deploy/docker-compose.release.yml \
  .env.example \
  scripts/install.sh \
  scripts/nico-service.sh \
  scripts/demo.sh \
  scripts/lib.sh \
  docs/installation.md; do
  [[ -f "$ROOT_DIR/$required" ]] || die "required release input is missing: $required"
done

TEMPORARY="$(mktemp -d "${TMPDIR:-/tmp}/nico-release.XXXXXX")"
trap 'rm -rf "$TEMPORARY"' EXIT
STAGE="$TEMPORARY/nico-agent"
mkdir -p "$STAGE/deploy" "$STAGE/scripts" "$STAGE/docs" "$STAGE/wheels"
cp "$ROOT_DIR/docker-compose.yml" "$STAGE/docker-compose.yml"
cp "$ROOT_DIR/deploy/docker-compose.release.yml" "$STAGE/deploy/docker-compose.release.yml"
cp "$ROOT_DIR/.env.example" "$STAGE/.env.example"
cp "$ROOT_DIR/scripts/nico-service.sh" "$STAGE/scripts/nico-service.sh"
cp "$ROOT_DIR/scripts/demo.sh" "$STAGE/scripts/demo.sh"
cp "$ROOT_DIR/scripts/lib.sh" "$STAGE/scripts/lib.sh"
cp "$ROOT_DIR/docs/installation.md" "$STAGE/docs/installation.md"
cp "$WHEEL" "$STAGE/wheels/$(basename "$WHEEL")"
printf '%s\n' "$TAG" > "$STAGE/version.txt"
chmod 755 "$STAGE/scripts/"*.sh

mkdir -p "$OUTPUT"
rm -f -- \
  "$OUTPUT/install.sh" \
  "$OUTPUT/nico-agent-bundle.tar.gz" \
  "$OUTPUT/version.txt" \
  "$OUTPUT/SHA256SUMS" \
  "$OUTPUT"/nico_agent_platform-*.whl
if tar --version 2>/dev/null | grep -q 'GNU tar'; then
  SOURCE_DATE_EPOCH="${SOURCE_DATE_EPOCH:-0}"
  [[ "$SOURCE_DATE_EPOCH" =~ ^[0-9]+$ ]] || die "SOURCE_DATE_EPOCH must be an integer"
  command -v gzip >/dev/null 2>&1 || die "gzip is required to create a release archive"
  tar --sort=name --mtime="@$SOURCE_DATE_EPOCH" --owner=0 --group=0 --numeric-owner \
    -cf - -C "$TEMPORARY" nico-agent | gzip -n > "$OUTPUT/nico-agent-bundle.tar.gz"
else
  tar -czf "$OUTPUT/nico-agent-bundle.tar.gz" -C "$TEMPORARY" nico-agent
fi
cp "$ROOT_DIR/scripts/install.sh" "$OUTPUT/install.sh"
cp "$WHEEL" "$OUTPUT/$(basename "$WHEEL")"
chmod 755 "$OUTPUT/install.sh"
printf '%s\n' "$TAG" > "$OUTPUT/version.txt"
(
  cd "$OUTPUT"
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum install.sh nico-agent-bundle.tar.gz version.txt \
      "$(basename "$WHEEL")" > SHA256SUMS
  else
    shasum -a 256 install.sh nico-agent-bundle.tar.gz version.txt \
      "$(basename "$WHEEL")" > SHA256SUMS
  fi
)
printf '[nico-release] packaged %s in %s\n' "$TAG" "$OUTPUT"
