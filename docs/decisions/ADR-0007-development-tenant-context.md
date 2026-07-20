# ADR-0007：认证前的开发租户上下文

- 状态：Accepted
- 日期：2026-07-17

## 背景

Goal C 必须验证真实多租户 Repository、复合外键与 RLS，但 API Key/JWT 按路线图在 Goal K 实现。领域请求不能接受并信任请求体中的 `tenant_id`。

## 问题

在正式认证尚未实现时，如何让自动化测试和本地调用显式选择 TenantContext，同时不把临时机制误认为生产安全边界。

## 候选方案

1. 暂时关闭 API 多租户访问，只测 Repository。
2. 在每个请求体携带 `tenant_id` 并直接信任。
3. 仅在 development/test 环境接受受信网关头 `X-Tenant-ID`，生产环境拒绝，领域请求体始终不含 tenant_id。

## 最终选择

选择方案 3。TenantContext 由统一 FastAPI dependency 解析；Repository 再通过数据库运行角色和 `app.tenant_id` 会话变量执行 FORCE RLS。租户 bootstrap API 同样只在 development/test 开放。

## 选择原因

它允许 Goal C 完整验证 API 到数据库的租户链路，又清楚隔离了临时开发入口。未来认证只需替换上下文解析器，不需要修改领域服务与 Repository。

## 代价

Goal C 的 HTTP 接口不能公开部署；本地调用必须显式携带 Header；在 Goal K 完成前不能声称具备认证或防冒充能力。

## 后续影响

文档、OpenAPI 和 E2E 必须标记该 Header 为 development/test only。Goal K 将 API Key/JWT claims 映射到同一个 TenantContext，并删除或永久禁用生产环境的 Header 路径。

## 可逆性

高。TenantContext 与下游服务接口保持不变，只替换入口解析策略。
