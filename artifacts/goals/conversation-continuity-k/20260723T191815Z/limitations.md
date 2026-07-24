# Limitations

- No configured external-provider credentials were available. The external
  report is an honest unverified skip, not a pass.
- `baseline-v0` is a deterministic counterfactual for Gate behavior and
  overhead comparison. It is not production telemetry or a former-model
  quality claim.
- Hermetic scripted Actions verify Runtime protocol and policy behavior, not
  semantic calibration of a live model.
- Exact latency values are environment-dependent and therefore remain in the
  JSON report instead of being treated as a universal threshold.
- The companion continuity track is closed, but parent RH-H still owns
  unrelated Tool, approval, Artifact, budget and database obligations.

Run the same AE1-AE11 suite against each deployment's configured
OpenAI-compatible provider before using its results as provider-quality
evidence. Add an Intent Resolver only if repeated credentialed runs show
stable wrong-intent or unnecessary-clarification failures.
