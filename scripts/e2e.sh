#!/usr/bin/env bash

set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

require_command curl
load_env_file
"$ROOT_DIR/scripts/dev.sh" --detach

API_BASE="http://localhost:${API_PORT:-18000}"
WEB_BASE="http://localhost:${WEB_PORT:-18080}"
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

curl --fail --silent --show-error "$API_BASE/api/v1/health/live" >"$TMP_DIR/live.json"
curl --fail --silent --show-error "$API_BASE/api/v1/health/ready" >"$TMP_DIR/ready.json"
curl --fail --silent --show-error "$API_BASE/openapi.json" >"$TMP_DIR/openapi.json"
curl --fail --silent --show-error "$WEB_BASE/" >"$TMP_DIR/index.html"

python3 - "$TMP_DIR" <<'PY'
import json
import pathlib
import sys

root = pathlib.Path(sys.argv[1])
live = json.loads((root / "live.json").read_text())
ready = json.loads((root / "ready.json").read_text())
openapi = json.loads((root / "openapi.json").read_text())
index = (root / "index.html").read_text()

assert live["status"] == "alive"
assert ready["status"] == "ready"
assert set(ready["components"]) == {"postgres", "redis", "minio"}
assert "/api/v1/health/ready" in openapi["paths"]
assert "Nico Agent Platform" in index
PY

worker_id="$("${COMPOSE[@]}" ps --quiet worker)"
init_id="$("${COMPOSE[@]}" ps --all --quiet minio-init)"
[[ "$(docker inspect --format '{{.State.Running}}' "$worker_id")" == "true" ]] \
  || die "worker is not running"
[[ "$(docker inspect --format '{{.State.ExitCode}}' "$init_id")" == "0" ]] \
  || die "MinIO bucket initialization failed"

"${COMPOSE[@]}" ps
log "E2E passed; the platform remains running at $WEB_BASE (cleanup with scripts/cleanup.sh)"
