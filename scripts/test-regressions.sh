#!/usr/bin/env bash

set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

family=""
tier="unit"
check_only=false

usage() {
  printf '%s\n' \
    'usage: scripts/test-regressions.sh [--family FAMILY] [--tier unit|integration|e2e|all] [--check-only]'
}

while (($#)); do
  case "$1" in
    --family)
      [[ $# -ge 2 ]] || { usage >&2; exit 2; }
      family="$2"
      shift 2
      ;;
    --tier)
      [[ $# -ge 2 ]] || { usage >&2; exit 2; }
      tier="$2"
      shift 2
      ;;
    --check-only)
      check_only=true
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      usage >&2
      exit 2
      ;;
  esac
done

case "$tier" in
  unit|integration|e2e|all) ;;
  *) usage >&2; exit 2 ;;
esac

ensure_python_environment
"$ROOT_DIR/.venv/bin/python" "$ROOT_DIR/scripts/regression.py" check
if [[ "$check_only" == true ]]; then
  exit 0
fi

selection=(tests)
if [[ -n "$family" ]]; then
  selection+=(--family "$family")
fi
if [[ "$tier" != all ]]; then
  selection+=(--tier "$tier")
fi
mapfile -t nodeids < <(
  "$ROOT_DIR/.venv/bin/python" "$ROOT_DIR/scripts/regression.py" "${selection[@]}"
)
if ((${#nodeids[@]} == 0)); then
  printf '[nico-regression] no tests matched the requested selection\n' >&2
  exit 2
fi

log "running ${#nodeids[@]} registered regression tests"
cd "$ROOT_DIR"
"$ROOT_DIR/.venv/bin/pytest" \
  -p nico_agent.regressions.pytest_plugin \
  "${nodeids[@]}"
