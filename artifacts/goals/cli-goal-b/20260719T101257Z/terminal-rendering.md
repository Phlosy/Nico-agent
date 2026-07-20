# 终端渲染证据说明

Goal B 的终端验收采用可复查文本而非录屏：

- `cli-e2e/no-color.txt`：真实 readiness 响应的 Rich Panel 人类可读渲染；在 `NO_COLOR=1` 下生成，自动检查不含 ANSI 控制序列。
- `cli-e2e/*.json`：同一真实 API 路径的机器可读结果，每条命令严格输出一个 JSON 文档。
- `cli-e2e/missing-tenant.json`：机器可读 stderr 错误样例。
- `cli-e2e/demo-output.txt`：Task → Run → Worker → Mock Runtime → completed 的真实终端摘要。

coin-cat logo 在 Goal A 只完成视觉方案，终端渲染明确属于 Goal D，因此本阶段没有以静态草图冒充已实现功能。

