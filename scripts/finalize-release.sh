#!/usr/bin/env bash

set -euo pipefail

ASSETS=""
DIGESTS=""

die() {
  printf '[nico-release] ERROR: %s\n' "$*" >&2
  exit 1
}

while (($#)); do
  case "$1" in
    --assets)
      (($# >= 2)) || die "--assets requires a directory"
      ASSETS="$2"
      shift 2
      ;;
    --digests)
      (($# >= 2)) || die "--digests requires a directory"
      DIGESTS="$2"
      shift 2
      ;;
    *) die "unknown option: $1" ;;
  esac
done

[[ -d "$ASSETS" ]] || die "asset directory not found: $ASSETS"
[[ -d "$DIGESTS" ]] || die "image digest directory not found: $DIGESTS"
for name in backend hermes web; do
  digest_file="$DIGESTS/$name.txt"
  [[ -s "$digest_file" ]] || die "image digest is missing: $name"
  reference="$(<"$digest_file")"
  [[ "$reference" =~ ^ghcr\.io/[^[:space:]@]+/nico-agent-${name}:v[0-9]+\.[0-9]+\.[0-9]+@sha256:[0-9a-f]{64}$ ]] || \
    die "invalid $name image reference: $reference"
done

temporary="$(mktemp "$ASSETS/.images.txt.XXXXXX")"
trap 'rm -f -- "$temporary"' EXIT
sort "$DIGESTS/backend.txt" "$DIGESTS/hermes.txt" "$DIGESTS/web.txt" > "$temporary"
mv -f -- "$temporary" "$ASSETS/images.txt"

(
  cd "$ASSETS"
  wheel=(nico_agent_platform-*.whl)
  [[ -f "${wheel[0]}" && "${#wheel[@]}" -eq 1 ]] || die "expected exactly one CLI wheel"
  files=(install.sh nico-agent-bundle.tar.gz version.txt images.txt "${wheel[0]}")
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "${files[@]}" > SHA256SUMS
  else
    shasum -a 256 "${files[@]}" > SHA256SUMS
  fi
)
printf '[nico-release] finalized release assets in %s\n' "$ASSETS"
