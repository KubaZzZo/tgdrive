# 客户端与备份

在网页顶部打开「客户端与备份」。客户端凭据在生成时显示一次，可单独撤销。每个凭据绑定签发时的频道，服务器切到其他频道时会拒绝旧频道的客户端请求，切回后可继续使用。客户端通过 HTTPS 传送凭据。

## PicGo

生成「PicGo 图床」令牌。使用 PicGo 的 Web Uploader 插件，按下表填写。

| 字段 | 值 |
| --- | --- |
| 上传地址 | `https://你的网盘域名/api/picgo/upload` |
| 表单文件字段 | `file` |
| 返回直链路径 | `data.downloadLink` |
| 请求头 | `Authorization: Bearer <令牌>` |

每次请求上传一张 JPEG、PNG、GIF 或 WebP，图床上限 20 MiB，同时受站点文件大小限制。写入 Telegram 成功后直接返回可公开访问的图片链接。仍可从图床页面撤销链接。

## 只读 WebDAV

生成「只读 WebDAV」应用密码。在 rclone、AList 或其他 WebDAV 客户端将服务地址设为 `https://你的网盘域名/webdav/`，用户名填 `tgdrive`，密码填生成的凭据。rclone 的供应商选通用 WebDAV。

支持浏览目录、读取文件信息和 Range 下载。已移入回收站的文件不会出现在目录中。若网盘的同一目录有重名项，WebDAV 在重名路径上追加资源 ID。代理应保留 `Authorization`、`Depth` 和 `Range` 请求头，并绕过 `/webdav` 与 `/webdav/*` 的共享缓存和压缩。

## 频道索引备份

点击「下载当前频道索引」。ZIP 内的 `manifest.json` 记录频道 ID、导出时间和数据库版本，`index.sqlite3` 包含目录、文件分段索引、回收站、频道同步索引、分享链接、图床和人物标签。

压缩包含现有分享和图床令牌，请私下保存。它是供查看和迁移的索引快照，直接替换运行库需要完整部署备份，本功能只提供导出。
