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
- `app/api/schemas/`：按路由模块组织 HTTP 请求数据模型。没有请求体的路由无需对应的模型文件。
- `app/services/`：文件分类、人物识别与名称规则、频道同步等业务逻辑。
- `app/services/uploads.py`：后台上传任务、哈希、临时文件生命周期。
- `app/infrastructure/storage.py`：Telegram 客户端、分段上传、Range 读取、消息删除。
- `app/infrastructure/db/session.py`：SQLite 连接、PRAGMA 初始化和共享锁。
- `app/infrastructure/db/repositories/`：文件树、回收站、上传、账号、频道同步、人物、图床、分享、流量等领域的 SQLite 查询。`Database` 对外保留统一接口，让跨领域操作继续使用同一个连接和锁。
- `app/infrastructure/db/migrations.py`：按 `PRAGMA user_version` 顺序运行版本迁移。打开数据库时自动升级，各版本沿用原有事务边界，包括兼容旧版 Bot/CDN 数据库的迁移。
- `app/core/`：日志、请求 ID、会话认证、登录限流、异常处理、代理配置与 HTTP Range 解析。
- `app/main.py`：app factory，便于测试时注入临时数据库和 fake storage。

## 前端分层

- `src/api/`：fetch 封装和 TypeScript 类型，统一错误与 401 处理。
- `src/router/`：Vue Router 路由、登录守卫与旧版片段链接跳转。网盘的目录、搜索与分页状态保存在 `/drive` 的查询参数中。
- `src/views/`：登录、网盘、人物视频、图床、流量与频道同步视图，由路由按需加载。FastAPI 为这些页面路径提供 SPA 入口。
- `src/stores/`：Pinia 管理认证、文件列表、上传队列、Toast。
- `src/components/`：业务组件与媒体交互。`src/components/ui/` 存放通用弹窗、图标和提示组件。
- `src/styles/`：全局设计变量、基础样式和跨组件共享样式。页面布局和组件专用规则由各组件的 scoped 样式定义。

## 后台任务与功能开关

上传完成、人物模型识别和 Telegram 清理沿用各自的后台任务与 SQLite 状态。人物模型的 HTTP 调用是异步的，结果解析和队列数据库操作在线程中执行。永久删除写入清理队列后立即返回，由当前频道的后台任务删除远端消息。

上传转封装与视频抽帧共用一个 FFmpeg 并发名额，频道切换沿用该限制。缩略图请求最多接纳 8 个未完成任务，其中最多 2 个同时准备输入。满载或超时时返回无封面结果，浏览器可显示占位图。图片处理在线程中执行，FFmpeg 在子进程中运行。

`PEOPLE_ENABLED`、`IMAGE_HOST_ENABLED` 和 `TRAFFIC_ENABLED` 默认开启。将对应值设为 `false` 并重启可关闭入口和后台处理。人物标签和已发布图片的数据保留，重新开启后可以继续使用。关闭流量模块停止应用内采集和查询。独立安装的宿主机采集服务由部署端管理，CDN 限流继续按 CDN 配置运行。

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

`app/services/categories.py` 是所有上传客户端共用的分类规则。已知扩展名优先，其次根据 MIME 类型识别，未知格式归为 `other`。创建文件和重命名时更新 `items.category`；创建时为通用 MIME 推断具体类型，供预览使用。

schema v2 为历史文件补齐分类和通用 MIME，并创建分类、创建时间索引；DDL 与回填在同一事务中执行。文件所属目录和 Telegram 消息记录均保留。

`GET /api/items` 保留默认目录查询，支持 `scope=folder|all|recent`、`category`、`q`、`sort`、`direction`、`limit`、`offset`，返回 `categories` 分类数量/体积和 `total`。排序列由白名单选择，其余条件参数化，搜索中的 `%` 与 `_` 按字面处理。默认分页 60 项，最多 200 项。

前端 Drive store 统一管理分类、目录、搜索、排序与页码，取消过期请求并阻止旧响应覆盖当前视图。上传完成后刷新当前筛选及分类计数，不跳回根目录。

## 频道同步与播放器

`ChannelSyncService` 注册当前频道的 NewMessage / MessageEdited / MessageDeleted 监听，后台循环执行固定上界快照扫描。启动时完整扫描，运行期间按持久化消息游标增量补同步；手动完整扫描可排队，不会因已有扫描运行而丢弃请求。

schema v3 增加文件来源、频道消息映射与游标表；v4 记录远端编辑版本。消息映射按频道/消息 ID 唯一，历史与实时事件共用事务导入；网盘上传标记和已有分段关联用于去重。删除后的消息映射保留，历史扫描只在完整成功后核对缺失文件。远端版本比较防止较旧的历史结果覆盖较新的实时编辑。文件替换会更新分段大小、类型和分类，并失效消息缓存。

schema v7 为频道消息保存 `remote_name`：普通字符串表示最近一次自动名称，空字符串表示网盘内手动重命名，NULL 表示待保守识别的旧记录。视频名称取消息自带说明的首个有效段落，说明编辑与完整扫描更新自动名称；旧记录只在名称来源可确认时回填。媒体替换继续沿用更新文件信息的行为。

schema v12 新增 `video_people`。频道同步在导入事务中维护视频与人物的关联，删除视频时同步清除关联。人物筛选使用规范化后的原名，中文显示名仅用于展示和搜索。

schema v13 为可选的模型识别保存待分析描述、结果和重试状态。相同描述与模型配置复用结果，更新时仅修改仍引用该描述的视频。译名和别名用于搜索，人物详情继续以原名筛选。

schema v14 保存手动标签及单个视频的关联调整。列表和人物详情读取合并后的关联，频道同步继续维护自动识别结果。删除标签后会保留删除记录，防止后续同步恢复已移除的关联。

需要登录的 `GET /api/people` 支持 `q`、`limit` 和 `offset`，返回标签键、原名、显示名、视频数和标签总数。`POST /api/people` 新增标签，`DELETE /api/people?name=原名` 删除标签。`GET /api/items/{id}/people` 返回当前关联，`PATCH /api/items/{id}/people` 用 `add` 和 `remove` 修改关联。人物详情使用 `GET /api/items?scope=all&person=原名` 查询视频，响应包含 `person_display_name`。

照片大小与 Telethon 实际选用的最后一个 photo size 一致，支持原生照片和文档两类下载。下载请求采用 4 KiB–512 KiB 的二次幂块长，满足 Telegram 无 precise 标志时的参数限制；HTTP Range 的最终输出仍严格裁剪到请求长度。HEAD 仅返回元数据，不请求文件内容。客户端拖动/断开时，在取消保护范围内关闭下载流，归还借用的 Telegram sender。

前端 Sync store 定期读取鉴权后的 `/api/sync`，只在 revision 变化时刷新当前列表；旧会话的异步响应在退出登录后丢弃。MediaPlayer 使用浏览器媒体元素，不做转码；进度在本地存储中限量保存，关闭时暂停并移除媒体源。

视频通过浏览器原生媒体元素请求 HTTP Range。后端提供磁盘切片缓存、MP4 索引预热和顺序读取预读，分享直链复用相同的 Range 响应。鉴权后的封面接口读取 Telegram 静态缩略图。
## Telegram 连接与频道空间

主数据库保存账号/频道连接清单、活动连接、频道数据库映射，以及全局流量统计。
原存储频道继续使用原数据库；新频道使用 `DATA_DIR/channels/` 下的独立数据库。
账号登录使用独立的临时 Telethon 会话，扫码与两步验证不会影响当前连接。
确认切换时验证频道权限、检查后台上传、停止原同步，再持久化新连接并切换服务引用；失败时恢复旧服务。

ASGI 中间件为每个 HTTP 请求固定数据库、存储和上传服务引用，直到响应结束。
旧连接在最后一个响应结束后才断开，后台上传进行中则拒绝切换。
前端 API 使用频道标识检测旧页面请求，原生下载通过查询参数携带标识；频道变化返回 409 并刷新页面。
断点续传映射按频道隔离，原频道保留已有 localStorage 键以兼容升级前的续传任务。
分享查询使用当前频道空间，其他频道的分享仅在切回后恢复。
