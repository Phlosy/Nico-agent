# Goal H acceptance summary

- Native ReAct multi-round model/tool/observation loop: passed.
- Exact-version Tool Gateway authorization, budgets and deterministic observations: passed.
- Versioned integrity-checked pre-action and post-observation checkpoints: passed.
- PostgreSQL migration 0011 upgrade, full downgrade-to-base and reapply: passed.
- RunStep/ToolCall/ModelCall/ContextSnapshot/Event/Audit matching facts: passed.
- Real Compose Worker SIGKILL after successful `file.write`: passed.
- Expired-lease takeover by a distinct Worker: passed.
- Successful side effect replay prevention through stable idempotency key/cache: passed.
- Recovered Python sandbox call and final model answer: passed.
- Runtime event sequence continuity and evidence credential scan: passed.
- Nico Native Direct regression without Hermes: passed.
- External operator-model acceptance: required separately when a real endpoint and credential ref are supplied.
