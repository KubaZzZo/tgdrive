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

- 单用户密码登录，登录失败滑动窗口限流
- 文件夹树、重命名、移动、删除
- 浏览器分片上传、断点续传、网络失败指数退避
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
- 没有缩略图、转码、秒传。
- 分享链接没有密码保护。
- 上传中断后的分段级续传还没有实现。
- Telegram 不是专业对象存储，没有 SLA，请务必保留独立备份。

## 安全提示

- `session` 文件等同于 Telegram 账号登录凭证，不能提交到 Git 或发给他人。
- 不要公开传播文件，不要做公共 CDN，不要高并发刷上传/下载。
- 优先使用专用 Telegram 账号；更看重账号安全时优先 Bot 模式。
- 云端聊天不是端到端加密，敏感文件请自行加密后上传。
