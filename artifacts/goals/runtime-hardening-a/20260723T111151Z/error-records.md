# Error records

- The first complete integration run passed 139 tests and failed 13 Tool Gateway
  tests because the new decision-before-suspension test left its intentionally
  claimable recovery Run in the shared queue. The test now terminates its own
  fixture after assertions; the clean rerun passed all 152 integration tests.
- A direct mixed-version invocation without the integration database environment
  failed authentication against an unrelated localhost PostgreSQL instance. The
  final proof used an isolated Compose PostgreSQL database and passed all seven
  previous-application tests.
- Formal review candidates #1, #4 and #7 were independently rejected as false
  positives: destructive Alembic downgrade is rehearsal-only, the handshake
  lease override is intentional, and expired Tool-wait SQL recovery already had
  end-to-end coverage.
