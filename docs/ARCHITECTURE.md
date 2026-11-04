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

- 浏览器按 `UPLOAD_CHUNK_SIZE` 发送分片，服务端用 `Upload-Offset` 做幂等续传。写入、完成、取消与定时清理共用每任务异步锁；部署要求单进程。
- 浏览器队列支持暂停/继续，单任务运行结束前不会启动同一任务的下一次重试；状态查询失败时保留原任务 ID。
- Telegram 写入由 `UPLOAD_CONCURRENCY` 信号量限制并发，取消时清理已知的分段消息；数据库提交失败时也清理未登记的消息。
- 分片全部到齐后，后台任务计算 SHA-256 并按 `TG_SEGMENT_SIZE` 上传 Telegram。
- 失败时保留临时文件与 SQLite 记录，可按 offset 重试。
- 服务重启会把 `processing` 标记为可重试错误；超过 `UPLOAD_STALE_HOURS` 的任务会清理。

## 可观测性

- 每个请求生成或透传 `X-Request-ID`。
- `LOG_JSON=true` 时输出单行 JSON，方便 Loki/ELK 采集。
- `/api/health/live` 用于容器存活；`/api/health/ready` 同时检查 DB 与 Telegram。

## 下载资源控制

下载使用 512 KiB Telegram 请求，将字节范围换算为分块数并严格裁剪尾块。迭代结束或客户端中断时关闭下载流，消息元数据缓存最多保留 256 项。

## 自动分类与查询

`app/categories.py` 是所有上传客户端共用的分类规则。已知扩展名优先，其次根据 MIME 类型识别，未知格式归为 `other`。创建文件和重命名时更新 `items.category`；创建时为通用 MIME 推断具体类型，供预览使用。

schema v2 为历史文件补齐分类和通用 MIME，并创建分类、创建时间索引；DDL 与回填在同一事务中执行。文件所属目录和 Telegram 消息记录均保留。

`GET /api/items` 保留默认目录查询，支持 `scope=folder|all|recent`、`category`、`q`、`sort`、`direction`、`limit`、`offset`，返回 `categories` 分类数量/体积和 `total`。排序列由白名单选择，其余条件参数化，搜索中的 `%` 与 `_` 按字面处理。默认分页 60 项，最多 200 项。

前端 Drive store 统一管理分类、目录、搜索、排序与页码，取消过期请求并阻止旧响应覆盖当前视图。上传完成后刷新当前筛选及分类计数，不跳回根目录。

## 频道同步与播放器

`ChannelSyncService` 注册当前频道的 NewMessage / MessageEdited / MessageDeleted 监听，后台循环执行固定上界快照扫描。启动时完整扫描，运行期间按持久化消息游标增量补同步；手动完整扫描可排队，不会因已有扫描运行而丢弃请求。

schema v3 增加文件来源、频道消息映射与游标表；v4 记录远端编辑版本。消息映射按频道/消息 ID 唯一，历史与实时事件共用事务导入；网盘上传标记和已有分段关联用于去重。删除后的消息映射保留，历史扫描只在完整成功后核对缺失文件。远端版本比较防止较旧的历史结果覆盖较新的实时编辑。文件替换会更新分段大小、类型和分类，并失效消息缓存。

照片大小与 Telethon 实际选用的最后一个 photo size 一致，支持原生照片和文档两类下载。下载请求采用 4 KiB–512 KiB 的二次幂块长，满足 Telegram 无 precise 标志时的参数限制；HTTP Range 的最终输出仍严格裁剪到请求长度。HEAD 仅返回元数据，不请求文件内容。客户端拖动/断开时，在取消保护范围内关闭下载流，归还借用的 Telegram sender。

前端 Sync store 定期读取鉴权后的 `/api/sync`，只在 revision 变化时刷新当前列表；旧会话的异步响应在退出登录后丢弃。MediaPlayer 使用浏览器媒体元素，不做转码；进度在本地存储中限量保存，关闭时暂停并移除媒体源。
