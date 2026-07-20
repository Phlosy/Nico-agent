# Expected gap samples

These are audited product gaps, not failures hidden by the verification run.

- No `nico` project script exists; only API, Worker and Sandbox Runner entry points exist.
- OpenAPI reports no path containing `conversation`.
- No `Conversation`, `ConversationTurn` or `ToolApprovalRequest` class exists.
- Current approval paths are Growth Approval paths for Memory/Skill publication.
- Current Artifact upload requires a Run ID, so pre-Turn `/attach` is not available.

Goal A intentionally leaves these items `NOT_IMPLEMENTED`. Unexpected verification errors: none.
