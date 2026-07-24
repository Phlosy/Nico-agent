# Goal I Limitations

- Structured confidence calibration remains a model-quality input; the Runtime
  deliberately does not add language-specific phrase or typo classifiers.
- Direct and ReAct share the live Clarification Gate. Plan phase-specific JSON
  remains authoritative until U10 adds the shared semantic Completion floor.
- Legacy plain-text final compatibility remains until U10 proves strict final
  metadata enforcement.
- Protected answers use PostgreSQL RLS and projection separation rather than
  application-level field encryption.
- No credentialed external model behavior is claimed.
