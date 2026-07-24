# Goal G acceptance summary

- U7 / CC-G durable UserInputRequest backend implemented.
- Migration `0031` adds composite tenant/run bindings, single-active-request
  constraints, guarded terminal resolution, FORCE RLS, cancellation trigger,
  lifecycle parity, and least-privilege reconciliation.
- String, choice, and structured answers resume one persisted Action exactly
  once; invalid, expired, cross-tenant, conflicting, and late answers do not.
- Request retry, Worker suspension, lease release, cancellation, expiry, and
  answered-but-unwoken recovery are covered on PostgreSQL.
- Protected answer content is absent from public projections, Events, Audits,
  evidence, and documentation.
- `ask_user` remains absent from the live model Schema; API and CLI are U8.
