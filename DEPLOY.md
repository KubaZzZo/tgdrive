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
# 另备份宿主机 data/traffic/ 和 .env；它们不在 tgdata 卷内
```

## 7. 升级

```bash
cd /opt/tgdrive
git pull --ff-only
docker compose build --pull
docker compose up -d
```

SQLite schema 使用 `PRAGMA user_version` 管理；启动时会拒绝打开版本高于程序支持的数据库。

本次视频简介命名升级到 schema v7，记录自动名称与手动重命名标记；主数据库和使用中的频道数据库会自动迁移。升级前应按第 6 节停止应用并备份完整数据卷。若回滚到只支持 v6 的旧镜像，必须同时恢复升级前的一整组数据库与账号/频道映射，不能只替换镜像；恢复备份会丢弃备份之后的元数据变更。

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
## VPS 每日 100 GB 流量监测

更新代码后，在 Linux VPS **宿主机**的项目目录执行（不要在容器内执行）：

```bash
sudo bash deploy/install-traffic.sh
docker compose up -d --build --force-recreate app
systemctl status tgdrive-traffic
```

裸机部署执行第一条命令后，重启 `tgdrive` 服务即可。新安装的 bootstrap 脚本会自动安装采集服务。
采集器只需要系统 Python 3.10+，无需应用依赖；Ubuntu 22.04/24.04 的系统 Python 可用。
网页按钮中可修改每日额度，默认 100 GB，入站和出站合计，北京时间 00:00 换日。

采集服务每 5 秒读取 `/proc/net/dev` 的网卡字节计数，原子写入 `data/traffic/host.json`，
保留 35 天数据。Docker 只读挂载此目录；容器重建不会重置宿主机计数。
默认选择 IPv4 默认路由的网卡，避免把 Docker 网桥、veth 和回环的同一份流量重复累加。
请在面板核对显示的网卡确实是服务商计费的公网网卡。仅 IPv6、公私网分离、VPN、bond 等网络需手动指定。

自定义网卡：`sudo systemctl edit tgdrive-traffic`，填入下列内容（替换路径和网卡名）：

```ini
[Service]
ExecStart=
ExecStart=/usr/bin/python3 /opt/tgdrive/scripts/traffic_collector.py --state /opt/tgdrive/data/traffic/host.json --interfaces eth0
```

再执行 `sudo systemctl daemon-reload && sudo systemctl restart tgdrive-traffic`。
选择多个独立公网网卡可写 `--interfaces eth0,eth1`；不要同时选择桥或 bond 与其下层网卡。
安装脚本默认使用 `tgdrive` 系统用户，项目须位于该用户可访问的目录（建议 `/opt/tgdrive`）。

准确性与故障提示：

- 从首次启用时建立基线，不把开机以来流量误当成当天流量；因此首日记录不完整。
- 服务重启从上次采样继续；系统重启、网卡计数重置、更换网卡及跨日长时间中断会标记不完整。
- 正常跨日最多有一个采样间隔的归属偏差。跨日长时间中断无法回溯，恢复时差额保守计入恢复当天并标记缺口。
- 文件超过 30 秒未更新显示过期；采集文件损坏时服务报错，不会悄悄清零。
- 网卡计数包含网络层开销及重传，但宿主机与服务商计费层、取整和重置时间仍可能有差异，账单以服务商为准。
- 应用明细仅统计实际读取/发送的 HTTP 和 Telegram 有效载荷，不包含全部底层重试，不能据此推断 VPS 账单。
- 监测只告警，不自动限速或断流。若采集暂停，不能把显示的剩余额度当作实时值。

登录密码使用 VPS `.env` 的 `APP_PASSWORD`。修改后需重建/重启应用以加载；本地 `.env` 不会随 Git 推送到 VPS。
## Telegram 账号和频道切换的部署与备份

更新后重建/重启应用，登录网页，通过顶部“Telegram 账号”完成扫码、两步验证与频道选择。
无需手工覆盖原 `.env` 或原 session。网页保存的活动连接优先于初始 `.env` 连接；初始连接保留在列表中可切回。
新账号需先加入目标频道并取得发送、删除消息的管理员权限。

数据库 schema v6 自动保存账号/频道组合和频道数据库映射。备份不能只包含 `tgdrive.sqlite3`：
必须同时保存整个 `/data` 卷，包括 `accounts/*.session`、`channels/*.sqlite3` 和初始 session，另备份宿主机 `data/traffic/` 和 `.env`。
为得到一致的完整备份，应先停止应用，再归档整个数据卷；恢复时也应整组恢复，不能混用不同备份中的映射和频道数据库。
会话文件等同登录凭证，备份需加密保存，不能提交 Git。切换功能使用单应用进程部署。
