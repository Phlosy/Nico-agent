# CLI Goal C 验收摘要

状态：PASS / Verified（100%）

- 268 项后端单元测试通过。
- 11 项前端测试、TypeScript 检查与 production build 通过。
- 78 项 PostgreSQL/pgvector、Redis、MinIO 与 Runtime 真实集成测试通过，包含失败 Turn 的冻结版本 retry。
- Alembic 从 `20260719_0017` 完整降到 base，再完整升级到 head 通过。
- Conversation/Turn 的 RLS、复合外键、不可变身份、幂等、归档、冻结 AgentVersion 和 Run 权威投影通过。
- CLI-B 真实 API 回归通过。
- CLI-C 真实镜像 E2E 完成两个 Turn、`--continue`、`--resume --read-only`、history、SSE 与无 ANSI JSON。
- GNU timeout 向前台 `nico chat` 发送真实 SIGINT；CLI 调用服务端取消，最终 Turn 与 Run 均为 `cancelled`。
- 文档链接/发布面、Markdown、Ruff、格式、Shell、Compose 和 diff 检查通过。

完整输出见 `verify.log`；实际 API/CLI JSON 位于 `cli-e2e/`。
