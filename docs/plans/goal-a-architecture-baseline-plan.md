# Goal A：仓库勘察与架构基线计划

## 目标

在零代码仓库上建立可验证、可接续的需求与架构基线，禁止提前进入大规模业务实现。

## 输入

- 附件任务书，归档为 `docs/plans/agent-platform-implementation-taskbook.md`。
- Git 基线 `489899e`。
- 初始仓库唯一文件 `README.md`。

## 实施单元

1. 原样归档任务书并校验 SHA-256。
2. 扫描仓库结构、依赖、代码、文档和脚本，形成能力清单与缺口。
3. 确定部署单元、模块边界、数据权威、Worker、Runtime、Tool、Memory/Skill、Plugin 和租户隔离架构。
4. 定义任务书要求的核心领域对象、不变量与状态机。
5. 记录影响 Goal B–K 的首批 ADR。
6. 初始化 Goal Status、Feature Matrix、Goal A 证据和 Handoff。
7. 运行确定性验证脚本。

## 非目标

- 不搭建 FastAPI、React 或数据库。
- 不声明 Runtime、Hermes、Plugin、Memory、Skill 或量化工作流已经实现。
- 不创建静态接口或 Mock 页面冒充功能。
- 不执行 Goal B–K 的代码实现。

## 验证合同

- 任务书文件与原附件 SHA-256 一致。
- README 可导航到需求、架构、状态和 Handoff。
- 总体架构、领域模型、状态机和至少六项关键 ADR 存在。
- Goal Status 与 Feature Matrix 不夸大实现状态。
- Goal A 证据目录包含命令、日志、版本、错误、非适用项和验收总结。
- `bash scripts/verify-goal-a.sh` 返回 0。

## 完成定义

全部验证合同通过，Goal A 标记 Verified，Handoff 明确 Goal B 的入口和禁止重复实现内容。
