# ADR-0002：PostgreSQL 权威状态与持久化执行

- 状态：Accepted
- 日期：2026-07-16

## 背景

Run 必须持久化、可恢复、可取消和可审计，平台同时需要向量检索、实时事件和大对象存储。

## 问题

需要决定数据库、队列、事件流和 Artifact 的事实来源，避免 Redis/Worker 故障丢失执行状态。

## 候选方案

1. Redis Queue 是任务与状态的唯一来源。
2. Celery/Dramatiq 负责投递，业务状态另存 PostgreSQL。
3. PostgreSQL 保存领域状态和待执行 Run，Worker 使用租约领取；Redis 仅扇出，MinIO 保存大对象。

## 最终选择

选择方案 3。使用 PostgreSQL + pgvector、`FOR UPDATE SKIP LOCKED` 式租约队列、Redis SSE/协调、MinIO 内容寻址 Artifact。

## 选择原因

Run 创建、状态与 Event 可以原子提交；Worker 重启不依赖队列重放；第一版少一个双写一致性问题。pgvector 复用租户与来源过滤。

## 代价

高吞吐队列能力弱于专用 Broker；需要自己实现租约、心跳、退避和幂等，不能滥用数据库轮询。

## 后续影响

Goal B 建立数据库、Redis、MinIO；Goal C/D 实现租约表和 Worker。指标必须监控队列延迟和锁竞争。

## 可逆性

高。权威 Run 状态保持不变，未来可用事务 Outbox 投递到 Dramatiq/Celery/Kafka，同时保留数据库恢复语义。
