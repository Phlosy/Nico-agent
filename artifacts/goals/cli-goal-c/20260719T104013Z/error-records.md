# CLI Goal C 错误与修正记录

## 实现期

1. SQLAlchemy 首版 flush 尝试同时解析 Conversation last-turn 环和 Task→Run→Turn 依赖，触发排序/外键问题。修正为同一事务内显式按 Task、Run、Turn flush，最后更新 Conversation pointer；没有拆分事务。
2. 验收脚本最初用完整 `/api/v1/...` 路径扫描带 `/api/v1` prefix 的 router 源码，产生静态检查误报。`verify-initial.log` 保留该失败；修正为扫描 router 内相对路径后完整门禁通过。
3. 最终任务书边界复核发现 Turn 的服务端 retry 不应随 CLI `/retry` 一起延后。Goal C 随即补齐冻结 AgentVersion 的 revisioned retry、通用 Run retry 防旁路和真实失败集成测试，再重新执行完整门禁。

## 验收结果

没有剩余测试失败。`chat-cancelled.stderr.txt` 为空，证明预期 SIGINT 取消未输出 traceback 或错误合同。

已知且有意延后的能力：CLI `/retry`、`nico exec`、`nico run watch`、完整 slash commands、高级 Rich/header/coin-cat、summary/context selection、附件和持久化工具审批。服务端 Turn retry 已在 Goal C 补齐。
