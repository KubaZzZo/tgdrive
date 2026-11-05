# TG Drive

自用的 Telegram 网盘：浏览器上传/下载、文件夹、分享链接、视频在线播放。文件实际保存在 Telegram 私有频道，服务端只保存元数据。

> 当前定位是**单用户、个人私有部署的 MVP+**，不是多租户对象存储。重要数据请保留第二份独立备份。

## 技术栈

| 层 | 技术 |
|---|---|
| 后端 | Python 3.12、FastAPI、Pydantic、Telethon |
| 数据 | SQLite WAL、显式 schema 版本迁移 |
| 前端 | Vue 3、TypeScript、Pinia、Vite |
| 质量 | Ruff、Pytest、Vue TSC、GitHub Actions |
| 部署 | Docker 多阶段构建、Caddy 自动 HTTPS、systemd |

## 已实现

- 频道同步：直接发到存储频道的照片、视频、音频和文件自动收录；支持历史导入、断线补同步和去重
- 视频/音频播放器：进度拖动、倍速、快进快退、播放位置记忆、全屏和浏览器画中画
- iPhone 分片视频兼容播放、Telegram 视频封面与本机视频缓存；缓存写入当前手机或电脑，VPS 不保存视频缓存
- 频道视频优先用文字简介命名，编辑简介后自动更新；网盘内手动重命名的文件保留自定义名称

- 自动分类：图片、视频、音频、文档、压缩包、代码、安装包和其他；电脑/手机网页上传共用服务端规则
- 分类导航、分类数量与体积、搜索、排序、分页、列表/网格视图及移动端文件操作菜单
- 历史文件自动补齐分类，保留原有文件夹；支持 PDF 和常见媒体预览，无法预览时提示下载

- 单用户密码登录，登录失败滑动窗口限流
- 文件夹树、重命名、移动、删除
- 浏览器分片上传、暂停/继续、断点续传、网络失败指数退避
- 上传队列去重、批量暂停/继续、批量重试、按文件大小加权的总进度
- Telegram 写入并发限制、运行期间每 5 分钟清理过期临时文件
- Telegram MTProto 上传，大文件按 `TG_SEGMENT_SIZE` 切分为多个消息
- 上传失败时尽力清理已写入的 Telegram 分段
- 服务重启后自动把中断的上传标记为可重试
- 超时未完成上传的临时文件自动清理
- HTTP Range 下载，支持视频拖动进度条
- 分享链接和过期时间
- 图片/音频/视频在线预览
- Bot / 用户账号两种 Telegram 登录模式
- 结构化日志、请求 ID、统一错误响应
- `/api/health/live`、`/api/health/ready` 健康检查
- Vue 组件化前端、上传队列、自定义对话框、响应式移动端布局
- CI：后端 lint/test、前端类型检查/构建、镜像构建

## 快速开始

### 1. 安装后端

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements-dev.txt
Copy-Item .env.example .env
```

至少修改 `.env`：

```dotenv
APP_PASSWORD=你的强密码
APP_SECRET=至少32位随机字符串
```

### 2. 构建前端

```powershell
cd frontend
npm ci
npm run build
cd ..
```

构建产物输出到 `static/dist/`。生产环境由 Docker 自动完成这一步。

### 3. 配置 Telegram

获取 `api_id` / `api_hash`：

1. 打开 https://my.telegram.org
2. 进入 `API development tools`
3. 创建应用并写入 `.env`：

```dotenv
TG_MODE=user
TG_API_ID=123456
TG_API_HASH=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

扫码登录并生成 session：

```powershell
python scripts/login_qr.py
```

创建私有频道、发一条消息，然后获取频道 ID：

```powershell
python scripts/chat_id.py
```

把频道 ID 写入 `.env`：

```dotenv
TG_CHAT_ID=-100xxxxxxxxxx
```

### 4. 启动

```powershell
python -m uvicorn app.main:app --host 127.0.0.1 --port 8787
```

打开 http://127.0.0.1:8787 。

## 前端开发模式

后端：

```powershell
python -m uvicorn app.main:app --host 127.0.0.1 --port 8787 --reload
```

前端：

```powershell
cd frontend
npm run dev
```

打开 http://127.0.0.1:5173 。Vite 会把 `/api` 和 `/s` 代理到 8787。

## Bot 模式

1. 找 @BotFather 创建 Bot。
2. 把 Bot 加入私有频道并设为管理员，允许发消息。
3. 配置 `.env`：

```dotenv
TG_MODE=bot
TG_API_ID=123456
TG_API_HASH=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
TG_BOT_TOKEN=123456:ABC...
TG_CHAT_ID=-100xxxxxxxxxx
```

Bot 模式仍使用 Telethon/MTProto，因此同样需要 `TG_API_ID` / `TG_API_HASH`。

## 常用命令

```powershell
# 后端 lint + 测试
python -m ruff check app tests
python -m pytest

# 前端类型检查 + 生产构建
cd frontend
# 前端测试要求 Node.js 22.15+（CI 使用 Node 22）
npm test
npm run typecheck
npm run build
```

或使用 `make check`（Linux/macOS）。

## 目录结构

```text
app/
  api/routes/         auth / items / uploads / files / system 路由
  core/               日志、请求上下文、限流、异常处理
  services/uploads.py 后台上传任务、临时文件清理
  config.py           环境配置与校验
  db.py               SQLite schema、版本迁移、查询
  storage.py          Telegram 上传/下载/删除
  main.py             FastAPI app factory
frontend/
  src/api/            API client 与类型
  src/stores/         Pinia stores
  src/components/     Vue 组件
  src/styles/         全局设计系统
tests/                API、数据库、Range 单元/集成测试
static/dist/          前端构建产物（不提交）
deploy/               Docker/Caddy/systemd 部署辅助
```

详细说明见[架构文档](docs/ARCHITECTURE.md)与[视频播放和本机缓存指南](docs/video-playback.md)。历史验证记录：[频道同步与播放器](docs/CHANNEL_SYNC_AND_PLAYER_TEST.md)、[前端分类](docs/FRONTEND_CLASSIFICATION_TEST.md)、[真实传输](docs/LIVE_TRANSFER_TEST.md)。

## API 摘要

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/meta` | 应用和 Telegram 状态 |
| POST | `/api/login` | 登录 |
| POST | `/api/logout` | 退出 |
| GET | `/api/items` | 文件列表 |
| POST | `/api/folders` | 新建文件夹 |
| PATCH | `/api/items/{id}` | 重命名/移动 |
| DELETE | `/api/items/{id}` | 删除 |
| POST | `/api/items/{id}/share` | 创建分享 |
| POST | `/api/uploads` | 创建上传任务 |
| PATCH | `/api/uploads/{id}` | 上传分片 |
| POST | `/api/uploads/{id}/complete` | 提交到 Telegram |
| GET | `/api/files/{id}/download` | 下载/Range 流 |
| GET | `/api/files/{id}/thumbnail` | 需要登录的视频封面，缺少缩略图时返回 404 |
| GET | `/api/health/live` | 进程存活 |
| GET | `/api/health/ready` | DB + Telegram 就绪 |

开发环境可访问 `/api/docs` 查看 OpenAPI。

## 工程约定

- 业务接口错误统一返回 `{"detail": "...", "request_id": "..."}`。
- 每个响应带 `X-Request-ID`，日志可通过该 ID 追踪。
- 上传接口不会把服务端临时文件路径返回给浏览器。
- SQLite 使用 WAL、`busy_timeout` 和 schema 版本号。
- 生产环境强制校验非默认密码和至少 32 位 `APP_SECRET`。
- 上传临时文件只通过 `TEMP_DIR` 管理，失败和过期任务会清理。
- 前端严格 TypeScript，组件、状态、API 层分离。

## 已知限制

- 单用户，没有多租户隔离和权限系统。
- 没有服务端转码或秒传；视频封面使用 Telegram 缩略图或浏览器首帧。
- 分享链接没有密码保护。
- 上传中断后的分段级续传还没有实现。
- Telegram 不是专业对象存储，没有 SLA，请务必保留独立备份。

## 安全提示

- `session` 文件等同于 Telegram 账号登录凭证，不能提交到 Git 或发给他人。
- 不要公开传播文件，不要做公共 CDN，不要高并发刷上传/下载。
- 优先使用专用 Telegram 账号；更看重账号安全时优先 Bot 模式。
- 云端聊天不是端到端加密，敏感文件请自行加密后上传。

## 传输控制

- 上传队列可暂停浏览器传输，继续时会读取服务器已提交的 offset。正在写入 Telegram 的任务会继续保存，不能暂停或取消。
- 同一文件（目录、名称、大小、修改时间相同）不会重复加入活动队列。
- 刷新页面后，重新选择原文件可续传；如果任务已在 Telegram 保存中或已完成，会直接接回原任务状态。
- 暂停保留在当前页面中；刷新后需要重新选择原文件。浏览器不会在刷新后保留本地文件读取权限。
- `UPLOAD_CONCURRENCY=2` 控制服务端同时写入 Telegram 的任务数，多余任务等待空位。浏览器同时运行最多 2 个上传任务。
- `UPLOAD_STALE_HOURS=72` 控制未完成上传的保留时间；启动时和运行期间每 5 分钟清理，设为 `0` 可关闭自动清理。暂停任务同样受此时长限制。
- 当前部署使用单个 Uvicorn worker；同一临时目录请勿启动多个应用进程，上传互斥锁和并发限制位于进程内。

## 文件分类与页面使用

- 文件完成上传后，服务端根据扩展名和 MIME 类型自动分类。手机常见 HEIC/HEIF、MOV、录音和通用 MIME 文件都纳入同一规则。
- 分类是覆盖所有目录的聚合视图；文件仍保留在上传时选择的文件夹中。分类页面上传的文件保存到根目录，并按实际格式归类。
- “我的文件”展示目录结构；根目录搜索覆盖所有目录，进入文件夹后的搜索限定当前目录。各分类中的搜索限定当前分类。
- 支持最新/最早上传、名称、大小排序，每页 60 个项目。列表和网格视图共用搜索与分页状态。
- 修改扩展名后会重新分类；分类不是内容分析，也不会转换文件格式。HEIC 等格式的预览取决于浏览器支持，无法预览时可下载。
- 元数据 schema 升级到 v2，为已有文件补齐分类和通用 MIME 信息。迁移采用事务，失败后可重新执行。升级部署前应按既有备份流程保存 SQLite 元数据。

## Telegram 频道同步

默认启用 `CHANNEL_SYNC_ENABLED=true`。使用当前用户 session 连接存储频道后，服务会在后台扫描历史，并监听新消息、编辑和删除事件；每 `CHANNEL_SYNC_INTERVAL=30` 秒补同步新增记录。网页每 5 秒检查同步变化并更新当前列表，不需要手动刷新页面。

手机或电脑 Telegram 直接发送到频道的照片、视频、音频和文件，会显示在网盘根目录并自动分类。相册中的每条媒体消息分别收录，压缩照片采用 Telegram 保存的图片版本。聊天文字、链接预览以及网盘自己发送的 `tgdrive:v1:` 文件分段不会导入。

右上角“频道同步”可查看收录数量、扫描进度和异常，支持“立即同步”与“重新扫描历史”。历史扫描完整成功后才会清理已从频道删除的同步文件；断线期间的编辑或删除可通过启动扫描/手动历史扫描补齐。单纯修改消息说明不会覆盖网盘中自定义的文件名。

同步仅登记文件元数据，不将整个频道下载到服务器。删除网盘文件仍沿用原有行为：同时删除对应 Telegram 消息。删除记录会保留同步标记，避免清理失败或历史重扫把它重新导入。

完整历史扫描以用户账号模式验证；Bot 模式受 Telegram 历史读取接口限制，可能无法扫描历史，但不影响原有网页上传功能。

## 视频与音频播放

点击视频/音频文件打开播放器。支持原生播放控制、进度拖动、0.5–2 倍速、前后跳转 10 秒、音量、全屏，以及浏览器支持时的画中画。

普通媒体在当前浏览器记录最多 100 个文件的播放位置，重新打开会恢复进度，也可选择从头播放。关闭弹窗会停止播放并释放传输连接。聚焦播放器后，可使用空格播放/暂停、左右方向键快进快退、M 静音、F 全屏。

文件直接通过 HTTP Range 从 Telegram 流式读取，无需先下载完整视频。实际可播放的容器、视频和音频编码由浏览器决定；播放器不执行服务端转码，不支持的编码会提示下载到本地播放。

对于 H.264/AAC 分片 MP4，支持 MediaSource 或 ManagedMediaSource 的浏览器会使用兼容流式播放器，避免原生播放器先扫描整段视频。此模式从头播放，支持在已缓冲范围内拖动进度；默认预缓冲 60 秒，可选择 120 秒，iPhone 还会遵守系统缓冲窗口。普通播放单次在途请求最多 2 MiB，关闭弹窗立即取消读取。其他文件或浏览器使用原生播放。

需要完整缓存时，在播放器点击“缓存到本机”；缓存保存在当前设备，首次传输仍经 VPS 转发。操作和限制见[视频播放与本机缓存](docs/video-playback.md)。
## VPS 每日流量

登录后点击顶部的流量按钮，可查看每天入站＋出站总量、剩余额度和近 7 天记录。
默认额度为 **100 GB/天**（100,000,000,000 字节），按北京时间 00:00 重置，额度可在页面修改。
达到 80% 和 100% 会提示；此功能不自动中断传输。

VPS 用量读取**宿主机公网网卡**，包括 Telegram、网页分享、其他程序和网络重传。
项目有效载荷另列明细，不代替 VPS 用量。未安装宿主机服务时显示“流量待接入”。
已有部署请参考 [DEPLOY.md](DEPLOY.md) 的流量采集安装步骤。
## 切换 Telegram 账号与存储频道

登录网盘后，点击顶部 **Telegram 账号**（手机上显示“账号”），选择“扫码添加账号与频道”。
用目标账号的 Telegram 在“设置 → 设备 → 连接桌面设备”扫码；如启用两步验证，在页面输入 Telegram 密码。
登录成功后选择已加入的频道，也可输入频道 ID 或 `@用户名`，再确认切换。
新账号必须拥有该频道发送和删除消息的管理员权限。

- 每个频道独立保存文件目录、分享、上传任务和同步记录；切回原频道可恢复原内容。
- 分享链接仅在对应频道处于当前连接时可用，切换期间其他频道的分享返回不存在。
- 已保存的账号/频道组合可再次切换，无需重复扫码；最多保存 10 个组合。
- 正在写入 Telegram 的上传完成前不能切换；请先暂停浏览器上传队列。未完成上传留在原频道，可切回续传。
- 已开始的下载继续使用原连接；若该连接尚未释放，立即切回时会提示稍后重试。
- 网页连接配置持久化到主数据库，重启后自动恢复。VPS 流量统计与额度保持全局累计。
- 登录任务 10 分钟后失效；取消或过期会清理临时会话。两步验证密码不落盘。

此功能使用 `.env` 中已有的 `TG_API_ID/TG_API_HASH`，支持个人账号扫码；原 `.env` 用户或 Bot 连接作为默认连接保留。
新增会话位于 `DATA_DIR/accounts/`，频道数据库位于 `DATA_DIR/channels/`，都必须和主数据库一起备份。
仅运行一个应用进程；账号切换与上传任务调度在进程内协调。
