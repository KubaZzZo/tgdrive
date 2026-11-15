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

将示例域名、邮箱和仓库地址替换为自己的部署参数。

```bash
sudo bash deploy/bootstrap-ubuntu.sh \
  --domain drive.example.com \
  --email you@example.com \
  --repo https://github.com/OWNER/tgdrive.git
```

Docker 镜像采用多阶段构建，会自动执行前端 `npm ci && npm run build`，无需在宿主机安装 Node。

接入网站 CDN 或多 CDN 地域调度前，请阅读 [CDN 接入指南](docs/cdn.md)，确认私有接口缓存、视频 Range、流式缓冲和证书设置。实际部署记录与测速产物请保存在已忽略的 `docs/local/` 或 `output/`，不要提交凭据、个人域名或服务器信息。

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
# 多频道完整备份：先停止应用，确保各 SQLite 和账号映射处于同一时点
docker compose stop app
docker run --rm -v tgdrive_tgdata:/d -v $PWD:/out alpine \
  tar czf /out/data-$(date +%F).tgz -C /d .
docker compose start app
# 另备份 .env；它不在 tgdata 卷内
```

## 7. 升级

```bash
cd /opt/tgdrive
git pull --ff-only
docker compose build --pull
docker compose up -d
```

SQLite schema 使用 `PRAGMA user_version` 管理；启动时会拒绝打开版本高于程序支持的数据库。

应用会将数据库自动迁移到 schema v14，保留人物视频索引并添加手动标签及视频关联。升级前应按第 6 节停止应用并备份完整数据卷。回滚到只支持旧 schema 的镜像时，需同时恢复与该镜像匹配的整组数据库和账号、频道映射。恢复备份会丢弃备份之后的元数据变更。

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
## AxisNow CDN 套餐监控与保护

在服务器 `.env` 设置 `AXISNOW_API_TOKEN` 并重启应用，即可在顶部“TG Drive 流量”面板查看各订阅月额度、平台已用量、保护估算、余量和更新时间。令牌只用于后端调用 `https://api.axisnow.io/client/v1/tenant_subscriptions/filter` 只读查询接口，不返回给浏览器。请勿将令牌写入前端环境变量或 Git。

默认每 60 秒查询全部有效订阅，使用平台返回的 `quota_period`（北京时间自然月）和额度；平台 MB 按 MiB（1024² 字节）换算。`AXISNOW_SUBSCRIPTION_IDS` 可限定参与保护的订阅 UUID，以英文逗号分隔；留空时任意一个受限订阅余量不足都会触发全站媒体保护，不会自动修改平台订阅或 DNS。

`CDN_WARNING_PERCENT=80` 在面板预警，`CDN_STOP_PERCENT=95` 保留 5% 额度。下载、视频 Range、分享下载、封面均受保护；请求预计超过可用保护额度时返回 429，并在传输过程中按实际分块检查，达到阈值会中断正在进行的传输。管理、登录和流量面板保持可用。平台查询超过 3 分钟未成功、月份不匹配或配置的订阅不存在时暂停媒体传输，数据恢复后自动解除。用量更新时间只作展示：闲置订阅的时间戳可能不变，不以此单独判定查询失效。

本地 SQLite 持久化每订阅每月的保护估算：取平台计数与本地估算的较大值，再把本站每个媒体块加到所有受监控的有限额订阅上，覆盖平台统计延迟及多请求并发，重启和切换频道不清零。无法判断请求由哪个订阅转发，因此该估算有意偏保守，可能提前触发保护。不限制额度的订阅不会被当成零额度。

应用只能限制本站回源媒体流量，CDN 缓存命中、协议开销及同套餐其他站点的流量无法由此硬性封顶。请同时在 AxisNow 配置订阅额度或平台侧限制，并保留余量。TG Drive 应用额度和 CDN 套餐独立统计。部署后请检查 CDN API/分享路径不缓存，沿用本项目 Caddy 的 `private, no-store` 配置；令牌须有订阅读取权限。

## TG Drive 每日流量

应用自动统计 HTTP 和 Telegram 两段的入站、出站有效载荷，每 5 秒批量写入 SQLite，正常停止时写入剩余计数。Docker Compose 启动后即可使用。面板显示近 7 天用量，默认每日额度为 100 GB，可在网页调整，北京时间 00:00 换日。项目额度只提示，CDN 套餐保护独立执行。

升级前已安装旧采集服务的机器，可以用 `sudo systemctl disable --now tgdrive-traffic.service` 停用它，再重新创建应用容器。应用沿用 SQLite 中的传输明细和额度设置。

## Telegram 账号和频道切换的部署与备份

更新后重建/重启应用，登录网页，通过顶部“Telegram 账号”完成扫码、两步验证与频道选择。
无需手工覆盖原 `.env` 或原 session。网页保存的活动连接优先于初始 `.env` 连接；初始连接保留在列表中可切回。
新账号需先加入目标频道并取得发送、删除消息的管理员权限。

数据库 schema v6 自动保存账号/频道组合和频道数据库映射。备份不能只包含 `tgdrive.sqlite3`：
必须同时保存整个 `/data` 卷，包括 `accounts/*.session`、`channels/*.sqlite3` 和初始 session，另备份 `.env`。
为得到一致的完整备份，应先停止应用，再归档整个数据卷；恢复时也应整组恢复，不能混用不同备份中的映射和频道数据库。
会话文件等同登录凭证，备份需加密保存，不能提交 Git。切换功能使用单应用进程部署。

## 可选的 Bot 备用同步

将专用于 TG Drive 的 Bot 加入存储频道，在 VPS 的 `.env` 设置 `TG_BOT_FALLBACK_TOKEN`，然后重建应用容器。首次以 Bot 启动时使用频道数字 ID。备用通道通过 HTTP Bot API 长轮询收录新帖子，个人会话恢复后继续完整历史同步。Bot Token 应留在服务器配置中。

Bot 备用通道处理新帖和已收录帖子的更新。能够下载已收录且取得 Bot 文件标识的文件，大小上限为 20 MB。历史回扫和网页上传需要个人会话。不要让另一个服务同时轮询这个 Bot 或为它设置 webhook。

SQLite schema v8 保存 Bot 文件标识，v9 增加 Bot 视频封面。升级前按第 6 节备份数据；旧程序拒绝打开更高版本的数据库。

## 可选的 qBittorrent 磁力导入

在 `.env` 设置 `QB_IMPORT_DIR`（宿主机绝对路径，由 UID 10001 写入）和 `VIDEO_IMPORT_QB_PASSWORD`。用下面的命令启动 qBittorrent：

```bash
docker compose -f docker-compose.yml -f docker-compose.qbittorrent.yml up -d qbittorrent
```

通过下方的 SSH 隧道访问 Web UI。首次启动的临时管理员密码在 qBittorrent 容器日志中。用它登录，将 Web UI 密码改成 `.env` 里的值，再用相同的两个 Compose 文件启动应用。

TG Drive 提交的磁力任务使用 `tgdrive-import` 分类及共享目录中的独立任务目录。支持包含一个视频文件的磁力任务，通过 BitTorrent 网络发现对端。Telegram 入库成功后，应用删除对应的种子任务和 VPS 临时文件。手动任务使用 qBittorrent 自己的 `/downloads` 目录，可通过 Web UI 添加磁力或种子文件。

如需在 TG Drive 的「视频链接导入」页面管理从 qBittorrent Web UI 或浏览器脚本提交的任务，在共享目录中创建 `/video-imports/inbox`，并在 qBittorrent 中创建保存路径为该目录的 `tgdrive-inbox` 分类。提交时填写该分类和保存目录。下载完成后可以上传到当前 Telegram 频道并入库，或删除任务及其下载文件。

对于容器内 Web UI 使用 `8080`、VPS 绑定 `127.0.0.1:18885` 的部署，使用 `ssh -N -L 127.0.0.1:8080:127.0.0.1:18885 user@vps` 后打开 `http://127.0.0.1:8080`，使客户端的 Host 端口符合 qBittorrent 的检查。也可使用 HTTPS 反向代理，保留登录和 CSRF 防护，向 qBittorrent 转发访问域名及协议，并仅信任反向代理的上游来源地址。
