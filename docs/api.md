# 基础 API

Goal B 只提供基础设施可观测接口。领域 REST API、认证、SSE 和 SDK 从后续 Goal 开始实现。

## OpenAPI

- 规范：`GET /openapi.json`
- Swagger UI：`GET /docs`

## Liveness

```http
GET /api/v1/health/live
```

该接口不访问外部依赖。进程能够处理请求时返回 `200`：

```json
{
  "status": "alive",
  "service": "Nico Agent Platform",
  "version": "0.1.0"
}
```

## Readiness

```http
GET /api/v1/health/ready
```

API 并发探测 PostgreSQL、Redis 和 MinIO，每个探针有独立超时。全部可用返回 `200` 与 `ready`；任一不可用返回 `503` 与 `not_ready`，同时保留各组件结果：

```json
{
  "status": "ready",
  "checked_at": "2026-07-16T15:37:25.580402Z",
  "components": {
    "postgres": {"status": "up", "latency_ms": 4.683, "detail": null},
    "redis": {"status": "up", "latency_ms": 1.785, "detail": null},
    "minio": {"status": "up", "latency_ms": 3.023, "detail": null}
  }
}
```

所有 HTTP 响应（包括未处理的 500）包含 `X-Request-ID`。调用方提供的 ID 只有在满足安全字符与长度限制时才被沿用，否则服务生成 UUID。依赖失败只公开异常类别，不回显可能含凭据的异常原文；未处理的 500 使用固定安全响应。应用与 Uvicorn 运行日志使用单行 JSON；Alembic CLI 保留其标准迁移日志格式。

## 当前安全边界

健康接口在 Goal B 未启用认证，只应暴露在开发或受信运维网络。API Key/JWT、租户权限与限额属于后续 Goal；不得将本阶段部署直接作为公网生产控制面。
