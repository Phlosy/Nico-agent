# Goal C API 响应索引

- `cli-e2e/chat-first.json`：Conversation 与第一 Turn、Run 事件、最终输出和 usage。
- `cli-e2e/chat-second.json`：同一 Conversation 的第二 Turn。
- `cli-e2e/chat-resume.json`：只读恢复的 Conversation 与两个 Turn。
- `cli-e2e/history.json`：sequence 1、2 的服务端历史。
- `cli-e2e/conversations.json`：按 Project/Agent/status 筛选的最近会话。
- `cli-e2e/chat-cancelled.json`：SIGINT 后权威取消的 ConversationTurn/Run。
- `cli-e2e/slow-agent.json`、`slow-version.json`、`slow-published.json`：取消测试的确定性 Mock AgentVersion 建立过程。

这些响应来自实际 Compose API/Worker 镜像，不是 CLI 内置静态数据。
