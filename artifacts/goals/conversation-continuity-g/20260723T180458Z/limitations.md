# Limitations

- No public UserInput API or CLI interaction exists until U8.
- `ask_user` stays out of the live model Schema until U9 installs the
  deterministic Clarification Gate.
- Protected answer separation is enforced through PostgreSQL, FORCE RLS, and
  redacted projections; application-level field encryption is outside scope.
- No credentialed external-model quality claim is made.
