# CC-F Error Records

- A direct targeted integration invocation initially used the local default
  PostgreSQL credential and was rejected. Subsequent targeted and full
  verification used the repository environment and isolated Compose database;
  no credential value was printed or stored in evidence.
- The first PostgreSQL commit-barrier run exposed an awaited projection written
  as a generator, producing an async generator that `tuple()` could not
  consume. It was replaced with an explicit awaited loop. The targeted
  production path and full integration suite then passed.
- Initial compatibility tests still expected U5 to return a JSON final envelope
  verbatim. They were updated to the U6 contract: valid structured metadata is
  parsed and the authoritative `content` is dispatched, while legacy plain
  text remains unchanged.
- Ruff identified import ordering, one loop-closure binding, and one unused
  variable during implementation. All were corrected before final verification.
- No unresolved verification failure remains.
