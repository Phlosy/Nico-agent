# Goal K acceptance summary

- Tenant ∩ AgentVersion Memory/Skill policies with explicit scopes and caps: passed.
- Published, active, unexpired Memory recall through pgvector: passed.
- Published stable Skill resolution with candidate/draft exclusion: passed.
- Exact source ID, version and content hash frozen per Run: passed.
- Published knowledge is rendered as untrusted data, never as permission: passed.
- ContextSnapshot and ModelCall consumption linkage with counters: passed.
- Terminal outcome/effect metadata and Growth trajectory propagation: passed.
- Child knowledge policy can only narrow Parent and delegation permissions: passed.
- Public runtime API omits the private content-bearing selection snapshot: passed.
- Full downgrade-to-base and reapply, FORCE RLS and legacy regressions: passed.
- External operator-model acceptance: required separately with a real endpoint and credential ref.
