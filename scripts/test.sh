#!/usr/bin/env bash

set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

log "running local development and version workflow contracts"
"$ROOT_DIR/scripts/test-dev-workflow.sh"

log "preparing backend environment"
ensure_python_environment
log "running backend lint, format check and unit tests"
"$ROOT_DIR/.venv/bin/ruff" check "$ROOT_DIR/backend"
# Applied migrations are immutable deployment history. Revision 0026 predates
# the current Ruff formatter output, so validate every other backend file
# without rewriting that already-applied baseline.
"$ROOT_DIR/.venv/bin/ruff" format --check \
  --exclude "20260722_0026_chat_session_controls.py" \
  "$ROOT_DIR/backend"
"$ROOT_DIR/.venv/bin/pytest" "$ROOT_DIR/backend/tests/unit"

log "preparing frontend environment"
ensure_frontend_environment
log "running frontend component tests and production build"
npm --prefix "$ROOT_DIR/frontend" test
npm --prefix "$ROOT_DIR/frontend" run build
log "local test suite passed"
