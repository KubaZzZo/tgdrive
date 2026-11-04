# 架构说明

## 请求链路

```text
浏览器 Vue SPA
  -> FastAPI /api/*
  -> SQLite（元数据、上传状态、分享 token）
  -> Telethon（MTProto）
  -> Telegram 私有频道（文件内容）
```

## 后端分层

- `app/api/routes/`：HTTP 边界，只做参数校验、依赖注入和响应组装。
- `app/services/uploads.py`：后台上传任务、哈希、临时文件生命周期。
- `app/storage.py`：Telegram 客户端、分段上传、Range 读取、消息删除。
- `app/db.py`：SQLite schema 版本、事务和查询。
- `app/core/`：日志、请求 ID、安全中间件、登录限流、异常处理。
- `app/main.py`：app factory，便于测试时注入临时数据库和 fake storage。

## 前端分层

- `src/api/`：fetch 封装和 TypeScript 类型，统一错误与 401 处理。
- `src/stores/`：Pinia 管理认证、文件列表、上传队列、Toast。
- `src/components/`：展示组件，不直接拼 URL 或发请求。
- `src/styles/`：CSS 变量和响应式设计系统。

## 上传状态机

```text
uploading -> processing -> done
     |            |
     +-> error <--+
```

- 浏览器按 `UPLOAD_CHUNK_SIZE` 发送分片，服务端用 `Upload-Offset` 做幂等续传。
- 分片全部到齐后，后台任务计算 SHA-256 并按 `TG_SEGMENT_SIZE` 上传 Telegram。
- 失败时保留临时文件与 SQLite 记录，可按 offset 重试。
- 服务重启会把 `processing` 标记为可重试错误；超过 `UPLOAD_STALE_HOURS` 的任务会清理。

## 可观测性

- 每个请求生成或透传 `X-Request-ID`。
- `LOG_JSON=true` 时输出单行 JSON，方便 Loki/ELK 采集。
- `/api/health/live` 用于容器存活；`/api/health/ready` 同时检查 DB 与 Telegram。
