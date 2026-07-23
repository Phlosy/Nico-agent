# Verification commands

```text
scripts/test.sh
scripts/test-integration.sh
NICO_PREVIOUS_REF=HEAD scripts/verify-runtime-hardening-goal-a-mixed-version.sh
RUN_INTEGRATION=1 NICO_PREVIOUS_REF=HEAD \
  NICO_EVIDENCE_DIR=artifacts/goals/runtime-hardening-a/20260723T111151Z \
  scripts/verify-runtime-hardening-goal-a.sh
python3 scripts/check-docs.py
git diff --cached --check
```

The integration and Goal verifier commands used isolated PostgreSQL databases
migrated from zero to `20260723_0027`.
