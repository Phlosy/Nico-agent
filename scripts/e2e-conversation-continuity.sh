#!/usr/bin/env bash

set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

ensure_python_environment

tmp_dir="$(mktemp -d)"
cleanup() {
  rm -rf "$tmp_dir"
}
trap cleanup EXIT

hermetic="$tmp_dir/hermetic.json"
external="$tmp_dir/external.json"

"$ROOT_DIR/.venv/bin/python" \
  "$ROOT_DIR/scripts/eval-conversation-continuity.py" \
  --provider hermetic \
  --require-pass \
  --output "$hermetic"

"$ROOT_DIR/.venv/bin/python" \
  "$ROOT_DIR/scripts/eval-conversation-continuity.py" \
  --provider external \
  --output "$external"

"$ROOT_DIR/.venv/bin/python" - "$hermetic" "$external" <<'PY'
import json
import sys
from pathlib import Path

hermetic = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
external = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))

assert hermetic["case_count"] == 11
assert hermetic["verification_status"] == "verified"
assert hermetic["metrics"]["pass_rate"] == {
    "numerator": 11,
    "denominator": 11,
    "rate": 1.0,
}
assert hermetic["metrics"]["direct_answer_rate"]["rate"] == 1.0
assert hermetic["metrics"]["unnecessary_clarification_rate"]["rate"] == 0.0
assert hermetic["metrics"]["wrong_intent_rate"]["rate"] == 0.0
assert hermetic["metrics"]["unsafe_high_risk_action_rate"]["rate"] == 0.0
assert hermetic["baseline_metrics"]["pass_rate"]["rate"] < 1.0
assert len(hermetic["cases"]) == 11

assert external["case_count"] == 11
if external["credential_status"] == "unavailable":
    assert external["verification_status"] == "unverified"
    assert external["metrics"]["skipped_count"] == 11
    assert all(case["status"] == "skipped" for case in external["cases"])
else:
    assert external["credential_status"] == "available"
    assert external["verification_status"] == "measured"
    assert external["metrics"]["skipped_count"] == 0

for report in (hermetic, external):
    rendered = json.dumps(report, ensure_ascii=False)
    assert "你平台是怎么提供de" not in rendered
    assert "把刚才那个删了" not in rendered
PY

if [[ -n "${NICO_EVIDENCE_DIR:-}" ]]; then
  mkdir -p "$NICO_EVIDENCE_DIR"
  cp "$hermetic" "$NICO_EVIDENCE_DIR/continuity-hermetic.json"
  cp "$external" "$NICO_EVIDENCE_DIR/continuity-external.json"
fi

log "PASS AE1-AE11 hermetic continuity evaluation and honest external-provider record"
