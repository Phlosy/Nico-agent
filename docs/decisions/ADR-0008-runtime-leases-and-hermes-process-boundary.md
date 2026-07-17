# ADR-0008：最小权限 Run 租约与 Hermes 进程边界

- 状态：Accepted
- 日期：2026-07-17

## 背景

Goal C 已有 FORCE RLS 的租户表和待执行 Run，但 Worker 既需要跨租户发现队列头，又不能长期使用数据库 Owner 读取所有租户正文。Hermes 是功能丰富且依赖较多的同步 Agent Runtime，也需要可取消和可替换。

## 问题

如何让 Worker 安全领取跨租户 Run，并在不污染核心依赖、不泄漏 Hermes 内部类型的前提下运行、取消和导出轨迹。

## 候选方案

1. Worker 使用数据库 Owner 扫描所有 Run，并在进程内直接 import Hermes。
2. Redis 队列投递 tenant/run ID，Worker 相信消息并直接启动 Hermes。
3. PostgreSQL SECURITY DEFINER claim 函数只暴露最小租约标识；正文处理切回 TenantContext。Hermes 通过 CLI 子进程 Adapter 接入，平台协议只使用规范化 DTO/Event。

## 最终选择

选择方案 3。创建无业务表直接权限的 claimer 角色与固定 search path 的原子 claim 函数。Worker 领取后使用 `nico_runtime` RLS 事务处理正文。Hermes Adapter 管理独立进程组、参数映射、输出事件、取消和脱敏轨迹。

## 选择原因

数据库仍是唯一权威来源，claim 与租约竞争可原子验证；Worker 无需持有绕过 RLS 的常规查询能力。进程边界隔离 Hermes 的依赖、全局环境、同步循环和工具执行，终止进程组也比试图中断 Python 线程更可靠。Provider DTO 使 Mock、Hermes 和未来远程 Runtime 共用合同。

## 代价

SECURITY DEFINER 函数需要严格的 owner、search path、授权和迁移测试。子进程启动有开销，流式事件粒度受 Hermes CLI 输出约束；当前 Hermes 无法诚实提供运行中 pause。Hermes 的完整结构化 tool 事件在 Goal E Tool Gateway 接管前只能作为 Runtime 原始事件记录。

## 后续影响

Goal D 实现 claimer、RuntimeSession 和 subprocess Adapter。Goal E 不得让 Hermes 绕过平台 Tool Gateway；若未来 Hermes 提供稳定的远程/嵌入式服务协议，可新增 Provider 而不修改领域状态。生产可将 Hermes 拆为独立容器并保留相同合同。

## 可逆性

高。claim 函数可替换为事务 Outbox/专用调度器而不改变 Run；Hermes subprocess Provider 可替换为远程或嵌入式 Provider。RuntimeSession 持久化的 provider/version/capabilities 保留了迁移依据。

