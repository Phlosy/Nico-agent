# Goal H Verification Commands

```bash
./scripts/e2e-runtime-user-input.sh
.venv/bin/pytest -q backend/tests/unit
./scripts/test-integration.sh
RUN_INTEGRATION=1 \
  NICO_EVIDENCE_DIR=artifacts/goals/conversation-continuity-h/20260723T182154Z \
  ./scripts/verify-conversation-continuity-goal-h.sh
sha256sum -c \
  artifacts/goals/conversation-continuity-h/20260723T182154Z/manifest.sha256
```
