# Goal C 终端渲染说明

本阶段验收基础滚动式 chat，不落地 CLI-D 的 coin-cat/header。

- human 模式显示 `RunCreated`、`RunStarted`、终态事件与最终 Nico 输出；事实来自服务端 Event/Turn。
- `--json` 缓冲事件并只输出一个 JSON 文档；`cli-e2e/chat-first.json` 与 `chat-second.json` 不含 ANSI。
- `--resume --read-only` 与 `conversation history` 读取相同持久化 Turn 历史。
- `Ctrl+C` 在活跃 Run 中请求服务端取消；`chat-cancelled.json` 的 Turn/Run 均为 `cancelled`。
- prompt_toolkit 输入历史由单测验证为目录 `0700`、文件 `0600`；Alt+Enter 插入换行，Ctrl+D 退出。
