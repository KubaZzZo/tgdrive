# TG Drive

自用的 Telegram 网盘 MVP：浏览器上传/下载、文件夹、分享链接、视频在线播放，文件实际保存在 Telegram 私有频道。

## 已实现

- 单用户密码登录
- 文件夹树、重命名、删除
- 浏览器分片上传，可断点续传（同一上传任务）
- Telegram MTProto 分片上传（Telethon 内部 512 KB 一片）
- 大文件按 `TG_SEGMENT_SIZE` 切分为多个 Telegram 消息
- HTTP Range 下载，支持视频拖动进度条
- 分享链接
- Bot / 用户账号两种 Telegram 登录模式

## 分片逻辑

上传一条大视频会经过两层分片：

1. 浏览器按 `UPLOAD_CHUNK_SIZE`（默认 8 MB）传给后端；
2. 后端把临时文件按 `TG_SEGMENT_SIZE`（默认 1.8 GB）切成若干段，每段作为一条 Telegram 文件消息上传。

Telegram MTProto 自身还会把每段按 512 KB 请求上传。下载时后端把各段按字节范围拼成连续流，所以浏览器看到的是一个完整文件，视频可以直接 Range 播放。

## 安全与封号

没有方法能保证绝对不封号。建议：

- 使用一个专用 Telegram 账号，不要用主账号跑自动化。
- 只放自己的文件，不公开传播、不做公共 CDN、不批量转发。
- 不要高并发刷上传/下载，脚本已经设置了重试和 FloodWait 等待。
- 私有频道只添加必要的账号；Bot 模式要把 Bot 加为频道管理员。
- `session` 文件等同于账号登录凭证，不要提交到 Git、不要发给别人。
- 云端聊天不是端到端加密；敏感文件请自行加密后再上传。

如果最看重账号安全，优先考虑 Bot 模式；如果最看重稳定的大文件能力，用专用账号 session。Bot 通过 Telethon/MTProto 上传的实际大小限制取决于账号和 Telegram 服务端，若被限制可改用专用账号或自建 Local Bot API Server。

## 安装

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
```

编辑 `.env`，至少设置：

```dotenv
APP_PASSWORD=你的强密码
APP_SECRET=至少32位随机字符串
```

## 获取 Telegram API 凭证

1. 打开 https://my.telegram.org
2. 进入 `API development tools`
3. 创建应用，拿到 `api_id` 和 `api_hash`
4. 写入 `.env`：

```dotenv
TG_MODE=user
TG_API_ID=123456
TG_API_HASH=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

## 建立 session（扫码登录）

```powershell
python scripts/login_qr.py
```

脚本会生成 `data/login_qr.png`，用手机 Telegram 扫一下即可。若账号有两步验证，脚本会提示输入 2FA 密码。session 会保存为 `data/tgdrive.session`。

> 不能直接从 Telegram Desktop 的本地数据中复用登录状态；Telethon 需要自己的 session。

## 建私有频道并填频道 ID

1. 在 Telegram 建一个私有频道，例如 `My TG Drive`。
2. 在频道里随便发一条消息。
3. 运行：

```powershell
python scripts/chat_id.py
```

把输出中对应频道的 ID 填到 `.env`：

```dotenv
TG_CHAT_ID=-100xxxxxxxxxx
```

## Bot 模式

1. 找 @BotFather 创建 Bot，拿到 token。
2. 把 Bot 加进私有频道，设为管理员，允许发消息。
3. `.env`：

```dotenv
TG_MODE=bot
TG_API_ID=123456
TG_API_HASH=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
TG_BOT_TOKEN=123456:ABC...
TG_CHAT_ID=-100xxxxxxxxxx
```

Bot 模式仍然需要 `TG_API_ID/TG_API_HASH`，因为这里使用的是 Telethon 的 MTProto 客户端，而不是 HTTP Bot API。

## 启动

```powershell
python -m uvicorn app.main:app --host 127.0.0.1 --port 8787
```

打开 http://127.0.0.1:8787 。

## 目录结构

```text
app/
  config.py          配置
  db.py              SQLite 元数据
  storage.py         Telegram 上传/下载/删除
  ranges.py          HTTP Range 解析
  main.py            FastAPI 接口
static/              前端
scripts/login_qr.py  扫码登录
scripts/chat_id.py   查看频道 ID
tests/               单元测试
```

## 已知限制

- 当前是单用户 MVP，没有多租户隔离。
- 上传失败时已成功上传的 Telegram 分段可能成为孤儿消息，后续可增加分段级重试和清理任务。
- 没有缩略图和转码；浏览器能否播放取决于视频编码。
- Telegram 不是专业对象存储，没有 SLA，重要文件请另做备份。
