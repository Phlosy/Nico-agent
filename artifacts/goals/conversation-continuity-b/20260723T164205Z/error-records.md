# CC-B Error Records

- The initial target-contract run failed during collection because the typed
  Conversation message and projection helper did not yet exist. This was the
  intended test-first failure.
- Importing the Conversation package contract directly from Runtime created a
  package initialization cycle. The final implementation places the shared
  provider-neutral DTO in `nico_agent.domain.context` and re-exports it from
  both public contract modules.
- The first projection budget calculation omitted Hash/truncation metadata.
  The final selector measures the complete projection shape and handles
  persisted inputs larger than the DTO limit before validation.
- No unresolved verification failures remain.
