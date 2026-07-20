# CLI Goal B 错误记录

## 预期错误样例

使用未配置 tenant 的 profile 执行 `nico --json agent list`：

- 退出码：2；
- stdout：空；
- stderr：`cli-e2e/missing-tenant.json`；
- 稳定错误码：`TENANT_CONTEXT_REQUIRED`；
- 信息中没有 URL 凭据、token 或服务端堆栈。

## 验收编排中发现并修复的问题

首次把全量集成回归与 CLI E2E 串联时，集成套件完成数据库完整降级/升级后，原先长期运行的 API/Worker 持有失效连接，随后 Demo Run 超时。产品命令、数据模型和迁移本身的独立验收均已通过，但阶段总验收没有形成可靠的串联结果。

修复：`scripts/e2e-cli-goal-b.sh` 在迁移回放后显式确认依赖服务，并重启 API/Worker、等待健康状态，再创建新的真实 Run。修复后重新从头运行最终 gate，251 项单测、11 项前端测试、75 项集成测试和 CLI E2E 全部通过，退出码为 0。该修复没有隐藏或跳过迁移测试。

## 环境采集说明

直接在宿主机运行未注入 Compose 数据库密码的 `alembic current` 会按默认本地配置失败；最终数据库版本通过 API 容器内的同一 Alembic 配置核验为 `20260718_0016 (head)`。这不是 CLI 产品路径，未计为通过证据。

