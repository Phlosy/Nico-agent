# Goal I Verification Commands

```bash
.venv/bin/pytest -q \
  backend/tests/unit/test_agent_actions.py \
  backend/tests/unit/test_clarification_gate.py \
  backend/tests/unit/test_native_continuity_prompt.py \
  backend/tests/unit/test_native_direct_runtime.py \
  backend/tests/unit/test_native_react_loop.py
scripts/test.sh
scripts/test-integration.sh
RUN_INTEGRATION=1 \
  NICO_EVIDENCE_DIR=artifacts/goals/conversation-continuity-i/20260723T184135Z \
  scripts/verify-conversation-continuity-goal-i.sh
sha256sum -c \
  artifacts/goals/conversation-continuity-i/20260723T184135Z/manifest.sha256
```
