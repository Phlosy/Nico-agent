# Goal H Limitations

- `ask_user` remains absent from the live model Action Schema until U9 adds
  the deterministic Clarification Gate.
- The PTY interaction uses an explicit Action HTTP fixture; real PostgreSQL
  suspend/wake and crash recovery are covered by the U7/U8 integration suites.
- Protected answers rely on PostgreSQL RLS and projection separation rather
  than application-level field encryption.
- No credentialed external model behavior is claimed.
