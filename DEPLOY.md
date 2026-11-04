# 部署到 VPS

> 流量模型：`浏览器 -> VPS -> Telegram` 双向都过 VPS，**1 GB 文件下载一次约消耗 2 GB VPS 流量**。瓶颈通常是带宽和月流量，不是 CPU。

## 1. 推荐配置

| 项 | 建议 | 原因 |
|---|---|---|
| 带宽 | ≥ 1 Gbps | 100 Mbps 看 4K 容易卡 |
| 月流量 | ≥ 2 TB，5 TB 更稳 | 下载/上传双份流量 |
| 磁盘 | ≥ 40 GB SSD | 镜像、SQLite、上传临时分片 |
| 内存 | 2 GB | 1 GB 可运行但余量少 |
| 虚拟化 | KVM | OpenVZ 跑 Docker 容易出问题 |

## 2. 一键部署

```bash
sudo bash deploy/bootstrap-ubuntu.sh \
  --domain drive.example.com \
  --email you@example.com \
  --repo https://github.com/KubaZzZo/tgdrive.git
```

Docker 镜像采用多阶段构建，会自动执行前端 `npm ci && npm run build`，无需在宿主机安装 Node。

首次启动后：

```bash
cd /opt/tgdrive
nano .env
# 必填：TG_API_ID / TG_API_HASH / TG_CHAT_ID
# 生产环境 APP_PASSWORD 不能是默认值，APP_SECRET 至少 32 位

docker compose run --rm app python scripts/login_qr.py
docker compose up -d --force-recreate app
docker compose ps
```

## 3. 配置检查

```bash
# 进程存活
curl -fsS https://drive.example.com/api/health/live

# 应用就绪：数据库 + Telegram 都必须正常
curl -fsS https://drive.example.com/api/health/ready

# 首页和前端资源
curl -fsS https://drive.example.com/ | head
```

`/api/health/ready` 返回 503 时，响应里的 `checks` 和 `storage_error` 会指出是数据库还是 Telegram 配置问题。

## 4. 验收清单

```bash
# 登录并保存 Cookie
curl -c /tmp/tg.cookie -H 'Content-Type: application/json' \
  -d '{"password":"你的密码"}' https://drive.example.com/api/login

# 文件列表
curl -fsS -b /tmp/tg.cookie https://drive.example.com/api/items

# Range 必须返回 206
curl -sI -b /tmp/tg.cookie -H 'Range: bytes=0-1023' \
  https://drive.example.com/api/files/1/download | head -8

# 确认媒体流没有被 gzip 破坏
curl -sI -b /tmp/tg.cookie -H 'Range: bytes=0-1023' \
  https://drive.example.com/api/files/1/download | grep -i content-encoding
```

浏览器侧验收：

1. 上传至少 500 MB 视频，确认完成。
2. 刷新页面后重新拖入同名文件，确认从断点继续。
3. 播放视频并拖动进度条，确认 3 秒内出画面。
4. 创建分享链接并在无登录窗口打开。
5. 删除文件，确认 Telegram 频道里的消息也被清理。

## 5. 日志

容器默认开启 `LOG_JSON=true`，每行一个 JSON 对象：

```bash
docker compose logs -f --tail=200 app
```

每个响应带 `X-Request-ID`，日志字段 `request_id` 可用于关联一次请求。

## 6. 备份

```bash
# SQLite 必须使用 .backup，不能直接拷贝运行中的库
docker compose exec -T app \
  python -c "import sqlite3; src=sqlite3.connect(\"/data/tgdrive.sqlite3\"); dst=sqlite3.connect(\"/data/backup.db\"); src.backup(dst); dst.close(); src.close()"
docker compose cp app:/data/backup.db ./backup-$(date +%F).db
docker compose exec -T app rm /data/backup.db

# session + .env 等同账号所有权，必须加密后异地保存
docker run --rm -v tgdrive_tgdata:/d -v $PWD:/out alpine \
  tar czf /out/data-$(date +%F).tgz -C /d .
```

## 7. 升级

```bash
cd /opt/tgdrive
git pull --ff-only
docker compose build --pull
docker compose up -d
```

SQLite schema 使用 `PRAGMA user_version` 管理；启动时会拒绝打开版本高于程序支持的数据库。

## 8. 常见故障

| 现象 | 原因 | 处理 |
|---|---|---|
| 502 | app 未启动/健康检查失败 | `docker compose logs app`、`docker compose ps` |
| 启动即退出 | 生产环境密码或 APP_SECRET 未通过校验 | 检查 `.env` |
| 首页显示 frontend not built | 未执行前端构建且不是 Docker 部署 | `cd frontend && npm ci && npm run build` |
| 视频拖不动 | 反代缓冲或被 gzip | 检查 Caddy `flush_interval -1`、`response_buffering off` |
| 上传卡住 | 磁盘满或临时卷异常 | `docker compose exec app df -h` |
| FloodWaitError | Telegram 限流 | 等待，降低并发，勿反复重启 |
| 401 | `COOKIE_SECURE=true` 但使用 http 访问 | 必须走 HTTPS |
| database is locked | 启动了多份 app | 保持 `app` 单实例 |

## 9. 安全红线

1. 8787 端口绝不 publish 到公网，只由 Caddy 访问。
2. `session` 文件加 `.env` 等于账号所有权，不提交 Git、不截图、不传聊天工具。
3. 使用自己申请的 `api_id/api_hash`，不要使用公开演示值。
4. 只做个人私有备份，不做公开分发或多租户。
5. 重要数据保留第二份独立备份；Telegram 不是有 SLA 的对象存储。
