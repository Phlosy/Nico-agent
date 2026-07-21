# Known Review Residuals: dev

Source review run: `20260721-071858-468382b3`

## Project task aggregation

- Severity: P2 (performance)
- Location: `backend/src/nico_agent/cli/project.py:291`
- Finding: `nico project tasks` reads every cursor page for every Project Session, then sorts and slices locally. Very large Projects can therefore require `sessions * pages` API calls and hold all timeline entries in CLI memory.
- Decision: accepted for the current dual-mode milestone. Correct cursor traversal is now guaranteed and each API response remains bounded, while a real fix requires a new project-wide, paginated task projection and public API contract.
- Follow-up trigger: add that projection before supporting Projects whose combined Session timelines routinely exceed tens of thousands of events.
