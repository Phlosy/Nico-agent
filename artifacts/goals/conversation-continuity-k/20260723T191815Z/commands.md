# Commands

The final acceptance run used:

```bash
NICO_EVIDENCE_DIR=artifacts/goals/conversation-continuity-k/20260723T191815Z \
  RUN_INTEGRATION=1 \
  scripts/verify-conversation-continuity-goal-k.sh
```

The verifier covers these principal commands:

```bash
python3 scripts/check-docs.py
ruff check backend/src backend/tests scripts/eval-conversation-continuity.py
ruff format --check backend/src backend/tests scripts/eval-conversation-continuity.py
pytest -q backend/tests/unit/test_conversation_continuity_eval.py
scripts/e2e-conversation-continuity.sh
scripts/test.sh
scripts/test-integration.sh
git diff --check
```

Evidence integrity is checked with:

```bash
cd artifacts/goals/conversation-continuity-k/20260723T191815Z
sha256sum -c manifest.sha256
```
