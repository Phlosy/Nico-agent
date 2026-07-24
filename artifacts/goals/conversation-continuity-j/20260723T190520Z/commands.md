# Goal J Verification Commands

```bash
.venv/bin/pytest -q \
  backend/tests/unit/test_agent_actions.py \
  backend/tests/unit/test_completion_gate.py \
  backend/tests/unit/test_conversation_model_messages.py \
  backend/tests/unit/test_native_direct_runtime.py \
  backend/tests/unit/test_native_react_loop.py \
  backend/tests/unit/test_native_plan_loop.py
scripts/test.sh
scripts/test-integration.sh
RUN_INTEGRATION=1 \
  NICO_EVIDENCE_DIR=artifacts/goals/conversation-continuity-j/20260723T190520Z \
  scripts/verify-conversation-continuity-goal-j.sh
sha256sum -c \
  artifacts/goals/conversation-continuity-j/20260723T190520Z/manifest.sha256
```
