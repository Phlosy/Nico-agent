# CC-B Verification Commands

```text
.venv/bin/ruff check <CC-B production and test files>
.venv/bin/ruff format --check <CC-B production and test files>
.venv/bin/pytest -q backend/tests/unit/test_conversation_context.py \
  backend/tests/unit/test_conversation_model_messages.py \
  backend/tests/unit/test_runtime_preparation.py \
  backend/tests/unit/test_native_direct_runtime.py \
  backend/tests/unit/test_native_react_loop.py \
  backend/tests/unit/test_native_plan_loop.py
.venv/bin/pytest -q backend/tests/unit
RUN_INTEGRATION=1 scripts/test-integration.sh
python3 scripts/check-docs.py
git diff --check
```
