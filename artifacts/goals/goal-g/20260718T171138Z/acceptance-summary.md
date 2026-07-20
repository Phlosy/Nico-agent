# Goal G acceptance summary

- Runtime protocol v2 and v1 terminal compatibility: passed.
- `nico_native` direct runtime without Hermes: passed.
- OpenAI-compatible streaming, usage, tool-delta parsing, retry and redaction: passed.
- ModelCall and ContextSnapshot RLS/immutability persistence: passed.
- Resumable SSE event query: passed.
- Hermetic Compose fake-model acceptance and zero credential leakage scan: passed.
- External operator model acceptance: required separately when `NICO_TEST_MODEL_*` credentials are supplied.
