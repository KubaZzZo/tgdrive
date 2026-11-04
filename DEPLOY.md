# 部署到美国 VPS

> 流量模型：`浏览器 → VPS → Telegram` 双向都过 VPS，**1 GB 文件看一次 = 2 GB 流量**。
> 瓶颈是带宽/流量，不是 CPU。

## 1. 选型

| 项 | 建议 | 原因 |
|---|---|---|
| 端口带宽 | ≥1 Gbps | 100 Mbps 看 4K 会卡，第一优先级 |
| 月流量 | ≥2 TB（5 TB 更稳） | 双份流量 |
| 磁盘 | ≥40 GB SSD | 镜像 + SQLite + 上传临时分片 |
| 内存 | 2 GB | 1 GB 能跑但余量少 |
| 位置 | 美东（纽约/阿什本） | 到 Telegram 欧洲 DC RTT 更小 |
| 虚拟化 | KVM | OpenVZ 跑 Docker 易出问题 |

## 2. 一键部署

```bash
sudo bash deploy/bootstrap-ubuntu.sh \
  --domain drive.example.com \
  --email you@example.com \
  --repo https://github.com/KubaZzZo/tgdrive.git
```

然后：

```bash
cd /opt/tgdrive
nano .env                      # 填 TG_API_ID / TG_API_HASH / TG_CHAT_ID
docker compose run --rm app python scripts/login_qr.py   # 扫码登录，生成 session
docker compose up -d --force-recreate app
docker compose ps              # 期望 app 为 healthy
```

## 3. 验收清单

```bash
# 后端存活
docker compose exec app curl -fsS http://127.0.0.1:8787/api/meta; echo

# 公网 HTTPS + 证书
curl -fsS https://drive.example.com/api/meta; echo
echo | openssl s_client -connect drive.example.com:443 -servername drive.example.com 2>/dev/null | openssl x509 -noout -dates

# ★ Range 必须返回 206（视频拖动的前提）
curl -sI -b /tmp/c.txt -H 'Range: bytes=0-1023' https://drive.example.com/api/files/1/download | head -8
# 期望：206 Partial Content + Content-Range: bytes 0-1023/<size>

# 确认没有被 gzip 破坏
curl -sI -b /tmp/c.txt -H 'Range: bytes=0-1023' https://drive.example.com/api/files/1/download | grep -i content-encoding
# 期望：无输出
```

浏览器侧：上传 500 MB 视频能完成 → 拖动进度条任意位置 3 秒内出画面 → 断网再拖同名文件能续传。

## 4. 断点续传行为

| 场景 | 行为 |
|---|---|
| 分片失败 | 自动退避重试 4 次（1/2/4/8 s） |
| 响应丢失（服务端已写入） | `offset < received` 幂等返回当前进度，不重复写 |
| 刷新/关标签页 | uploadId 存 localStorage，重新拖入同名文件从断点续传 |
| 服务端重启 | 临时分片在 `/tmp/upload` 卷，未完成的任务仍在 SQLite 里 |
| 取消 | `DELETE /api/uploads/{id}` 清临时文件与记录 |

## 5. 备份

```bash
# SQLite 必须用 .backup（不能直接 cp 运行中的库）
docker compose exec -T app sqlite3 /data/tgdrive.sqlite3 ".backup '/data/bk.db'"
docker compose cp app:/data/bk.db ./backup-$(date +%F).db
# session 文件 + .env（含 TG_SESSION，等同账号密码）加密后异地保存
docker run --rm -v tgdrive_tgdata:/d -v $PWD:/out alpine tar czf /out/data-$(date +%F).tgz -C /d .
```

## 6. 常见故障

| 现象 | 原因 | 处理 |
|---|---|---|
| 502 | app unhealthy / 绑错地址 | `docker compose logs app`，容器内必须 `HOST=0.0.0.0` |
| 视频拖不动 | 反代缓冲 / 被 gzip | 确认 `flush_interval -1`、`response_buffering off`、无 `Content-Encoding` |
| 上传卡住 | 磁盘满 / Caddy `max_size` 太小 | `df -h`；`request_body max_size 4.5GB` |
| FloodWaitError | Telegram 限流 | 等，别重启绕过；降并发；用自己申请的 api_id |
| 大量 401 | `COOKIE_SECURE=true` 但走的是 http | 必须通过 https 访问 |
| `database is locked` | 起了多份 app 实例 | `--scale app=1`，SQLite 只允许单写者 |

## 7. 安全红线

1. **8787 端口绝不 publish 到公网**，只由 Caddy 内网访问。
2. `TG_SESSION` 文件 + `.env` = 账号所有权，不进 Git、不截图、不传聊天工具。
3. 用自己申请的 `api_id/api_hash`，别用公开演示值（`api_id=6`）。
4. 只做个人私有备份，不做公开分发/多租户，否则账号会被限。
5. 重要数据留第二份独立备份。