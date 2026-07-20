# ADR-0011：一等入口的薄 CLI 与终端技术栈

- 状态：Accepted
- 日期：2026-07-19

## 背景

Nico 已有持久化 API、Worker、Runtime 和 SSE，但没有面向使用者的 `nico` 命令。CLI 既要提供接近现代 Agent CLI 的交互体验，也不能绕过服务端执行和审计边界。

## 问题

CLI 应该直接运行 Agent、调用仓库内部 service，还是仅作为 HTTP/SSE 客户端？普通滚动终端需要选用怎样的命令、输入和渲染技术？

## 候选方案

1. 在 CLI 进程内建立 Runtime 和 Tool Gateway，离线执行 Agent。
2. CLI 直接导入 ControlPlane/Runtime service 并连接数据库。
3. CLI 只调用公开 REST/SSE；使用 Typer 组织命令、prompt_toolkit 处理交互输入、Rich 渲染、httpx 通信、platformdirs 管理 profile，并自行实现最小 SSE parser。

## 最终选择

选择方案 3。`nico` 与服务端可同包发布，但 CLI 的运行时依赖方向只能指向 HTTP contract。交互采用滚动式终端，不采用全屏 TUI。机器模式不输出 Rich 控制字符。

## 选择原因

REST/SSE 使本地与远程行为一致，CLI 退出不会终止 Worker，所有调用经过同一租户、权限、事件和审计边界。Typer 适合层级命令；prompt_toolkit 提供多行、历史和快捷键；Rich 兼顾 Markdown、表格和无色降级；httpx 已是项目依赖。最小 SSE parser 可以精确遵循当前服务端事件格式并减少额外协议依赖。

## 代价

CLI 必须处理网络错误、重连、版本兼容和认证配置；同机调用也有 HTTP 开销。新增 Typer、prompt_toolkit、Rich 和 platformdirs 依赖，需要测试 TTY/非 TTY/Windows 宽度差异。

## 后续影响

Goal B 建立 `nico_agent.cli`，所有资源命令通过 API client。Goal C/D 增加 chat、exec、watch 和 SSE 渲染。不得从 CLI 导入 Worker、Runtime Provider、Tool Executor 或 SQLAlchemy model 来完成业务操作。

## 可逆性

高。命令框架或渲染库可以在保持 CLI contract 和 HTTP client 边界的前提下替换；薄客户端架构不变。
