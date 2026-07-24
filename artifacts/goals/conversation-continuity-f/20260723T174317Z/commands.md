# CC-F Verification Commands

```text
.venv/bin/ruff check <CC-F production and test files>
.venv/bin/ruff format --check <CC-F production and test files>
.venv/bin/pytest -q backend/tests/unit/test_action_dispatcher.py \
  backend/tests/unit/test_agent_actions.py \
  backend/tests/unit/test_native_continuity_prompt.py \
  backend/tests/unit/test_native_direct_runtime.py \
  backend/tests/unit/test_native_react_loop.py \
  backend/tests/unit/test_native_plan_loop.py
.venv/bin/pytest -q backend/tests/unit
RUN_INTEGRATION=1 scripts/test-integration.sh
scripts/test.sh
python3 scripts/check-docs.py
git diff --check
```
