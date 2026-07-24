# Goal H Error Records

- A targeted integration invocation first used default localhost credentials,
  then a development database without migration `0031`. The isolated
  integration runner was used as the authoritative environment.
- The first full integration run failed because the expiry test tried to
  mutate immutable `expires_at`. The fixture now creates a one-second request
  and observes natural expiry. The next complete run passed 171 tests.
- The first verifier invocation used an unescaped brace expression in an
  `rg` pattern. It was changed to fixed-string matching before the complete
  verifier run.
- No production defect remained after the final verifier.
