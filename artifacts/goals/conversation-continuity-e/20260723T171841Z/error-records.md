# CC-E Error Records

- A direct Alembic invocation initially used the local default database
  credential and was rejected. Verification then used the repository's
  isolated Compose integration database without exposing credentials.
- The first checkpoint edit matched two earlier Plan recovery constructors;
  those references were removed and placed only at post-Action checkpoints
  before tests.
- Initial Ruff output identified import ordering, two long lines, and a missing
  classmethod receiver; all were corrected before verification.
- The first full verifier run found two integration assertions still pinned to
  migration head `0029`. The lifecycle assertion failed before terminalizing
  its seeded Run, which caused 13 later Tool Gateway cases to claim the
  leftover Run and fail in a cascade. The expected head, table set, FORCE RLS,
  and same-Run FK assertions were updated for `0030`; the complete rerun passed
  all 161 integration tests.
- No unresolved verification failure remains.
