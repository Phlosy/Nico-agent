# Error records

- Proof-first expected red: `test_runtime_lifecycle.py` initially failed import
  because `RuntimeLoopState` and the lifecycle authority did not exist.
- A combined affected-integration probe found an unrelated MinIO environment
  failure: localhost:9000 served ClickHouse native protocol. The lifecycle,
  lease, queue, Coordination and persistence selection otherwise passed 21
  tests; the artifact case was then excluded from the focused U1 command.
- No full suite was run by this U1 worker, per delegated authority.
