#!/usr/bin/env bash

set -euo pipefail

TAG=""
ASSETS=""
DOCKER="${DOCKER:-docker}"

die() {
  printf '[nico-release] ERROR: %s\n' "$*" >&2
  exit 1
}

while (($#)); do
  case "$1" in
    --tag)
      (($# >= 2)) || die "--tag requires a value"
      TAG="$2"
      shift 2
      ;;
    --assets)
      (($# >= 2)) || die "--assets requires a directory"
      ASSETS="$2"
      shift 2
      ;;
    *) die "unknown option: $1" ;;
  esac
done

[[ "$TAG" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "--tag must use vX.Y.Z format"
[[ -n "${GH_REPO:-}" ]] || die "GH_REPO is required"
[[ -d "$ASSETS" ]] || die "asset directory not found: $ASSETS"
[[ -s "$ASSETS/images.txt" ]] || die "image manifest not found"

wheel=("$ASSETS"/nico_agent_platform-*.whl)
[[ -f "${wheel[0]}" && "${#wheel[@]}" -eq 1 ]] || die "expected exactly one CLI wheel"
assets=(
  "$ASSETS/install.sh"
  "$ASSETS/nico-agent-bundle.tar.gz"
  "$ASSETS/version.txt"
  "$ASSETS/images.txt"
  "$ASSETS/SHA256SUMS"
  "${wheel[0]}"
)
for asset in "${assets[@]}"; do
  [[ -f "$asset" ]] || die "release asset not found: $asset"
done

(
  cd "$ASSETS"
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum --check SHA256SUMS >/dev/null
  else
    shasum -a 256 --check SHA256SUMS >/dev/null
  fi
)

set +e
lookup="$(gh api --include "repos/$GH_REPO/releases/tags/$TAG" 2>&1)"
lookup_status=$?
set -e
http_status="$(printf '%s\n' "$lookup" | awk \
  '$1 ~ /^HTTP\// && $2 ~ /^[0-9][0-9][0-9]$/ {status = $2} END {print status}')"
if ((lookup_status == 0)); then
  release_exists=true
elif [[ "$http_status" == "404" ]]; then
  release_exists=false
else
  printf '%s\n' "$lookup" >&2
  die "could not determine whether release $TAG exists"
fi

while IFS= read -r immutable_reference; do
  target="${immutable_reference%@*}"
  repository="${target%:*}"
  digest="${immutable_reference##*@}"
  [[ "$target" == *":$TAG" ]] || die "image target does not match release tag: $target"
  "$DOCKER" buildx imagetools create --tag "$target" "$repository@$digest"
done < "$ASSETS/images.txt"

if [[ "$release_exists" == true ]]; then
  gh release upload "$TAG" "${assets[@]}" --clobber --repo "$GH_REPO"
else
  gh release create "$TAG" "${assets[@]}" --generate-notes --verify-tag --repo "$GH_REPO"
fi
