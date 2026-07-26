# Regression catalog

The Nico regression catalog turns every confirmed failure into a traceable
problem family. It is intentionally a catalog of real tests, not a second test
framework: each case points to one or more existing pytest node IDs.

## Triage workflow

1. Remove secrets and tenant data from the symptom.
2. Classify it against the catalog:

   ```bash
   scripts/regression.py classify "sanitized error and observed behavior"
   ```

3. If a family matches, add an `existing_family` incident to that family and
   run its tests:

   ```bash
   scripts/test-regressions.sh --family runtime.agent_action.unoffered_tool
   ```

4. If no family matches, add a new family, first record its incident as
   `new_family`, and add a failing regression test before the implementation.
5. Run `scripts/test-regressions.sh` and then the normal project gate.

Classification is a deterministic candidate search, not an automatic root-cause
decision. Review the candidate invariant and root cause before reusing a family.
Extend an existing case only when the old case does not cover the new boundary;
do not add a duplicate test for the same invariant.

## Catalog guarantees

Run the structural check directly:

```bash
scripts/regression.py check
```

The check rejects:

- duplicate family, incident, or case IDs;
- identical normalized matchers owned by different families;
- invalid regular expressions or classifications;
- incidents that reference unknown cases;
- families without exactly one originating `new_family` incident;
- test node IDs whose file or test function no longer exists.
- test node IDs owned by more than one case, unreferenced cases, and incident
  symptoms that cannot be classified back into their family.

The standard `scripts/test.sh` gate runs this check, so refactors cannot silently
orphan the catalog. The source of truth is
[`catalog.json`](catalog.json); schema and validation behavior live in
`backend/src/nico_agent/regressions/catalog.py`.

## Selecting tests

Run every registered unit regression:

```bash
scripts/test-regressions.sh
```

Select a tier with `--tier unit`, `--tier integration`, `--tier e2e`, or
`--tier all`. New integration and E2E entries should reference pytest tests that
remain runnable through the project environment for that tier. A registered
test that skips makes the regression run fail; skipped evidence never counts as
a passing backtest.
