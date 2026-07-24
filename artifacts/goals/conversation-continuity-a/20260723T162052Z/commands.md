# Verification commands

```text
.venv/bin/ruff check \
  backend/tests/unit/test_conversation_model_messages.py \
  backend/tests/integration/test_conversation_model_request_audit.py
.venv/bin/ruff format --check \
  backend/tests/unit/test_conversation_model_messages.py \
  backend/tests/integration/test_conversation_model_request_audit.py
.venv/bin/pytest -q backend/tests/unit/test_conversation_model_messages.py
scripts/test-integration.sh
RUN_INTEGRATION=1 \
  NICO_EVIDENCE_DIR=artifacts/goals/conversation-continuity-a/20260723T162052Z \
  scripts/verify-conversation-continuity-goal-a.sh
python3 scripts/check-docs.py
git diff --check
```

The integration and focused verifier use an isolated PostgreSQL database
migrated from zero to the live `20260723_0029` head, including the repository's
configured downgrade/reapply rehearsal.
