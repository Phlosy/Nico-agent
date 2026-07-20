# Error records

No unresolved error remained in the final verification run. `verify.log` is the authoritative command and failure record. The Goal F E2E also archives expected fail-closed HTTP responses for premature publication/resolution, self-review, and cross-tenant access under `e2e-goal-f/`.

Before the evidence run, the first standalone E2E authoring pass reached its final assertions and failed because the script incorrectly expected one successful Run to emit all four Memory types. The frozen contract emits episodic/semantic/procedural for completed Runs and episodic/working for unsuccessful terminal Runs. The assertion and acceptance wording were corrected; the product code was unchanged, and the complete evidence run then passed. Failed-Run working Memory remains directly covered by the real integration suite.
