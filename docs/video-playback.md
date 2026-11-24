# 视频播放、服务端缓存与外部直链

网页使用浏览器原生媒体元素播放。浏览器按需发起 HTTP Range 请求，服务端返回原文件对应的 206 字节区间。播放器保留进度恢复、倍速、旋转、定时关闭、画中画和全屏。视频能否解码取决于设备支持的容器和编码。无法在网页播放时，可使用外部播放器或下载。

在「播放设置」中选择「听视频」可隐藏画面并保留原有播放进度和声音，选择「恢复画面」即可切回。听视频继续读取原视频流。支持网页调用画中画的浏览器会在播放器工具栏显示画中画按钮。

下载和在线播放共用 ETag / If-Range 校验。跨 Telegram 分段的读取按文件顺序合并。HEAD 只返回响应头，无效范围返回 416。原生播放器会根据缓冲状态决定何时重新发请求。

## Telegram 小程序

在 BotFather 将小程序或菜单按钮的 URL 设为现有 CDN 上的 HTTPS 网盘地址，例如 `https://drive.example.com/people` 可以直接进入人物页。通过原生 Telegram 客户端的入口打开后，使用 TG Drive 的访问密码登录。页面、列表和封面通过该域名加载，列表和封面按各自的私有缓存策略处理。页面的嵌入策略会拦截 Telegram Web 的跨站小程序框架，可以直接在浏览器打开网盘地址。

单条频道消息存有完整视频时，点击「Telegram 直连播放」会在小程序里的 TG Drive 播放器打开。设备通过 Telegram WebSocket 按区间读取视频，播放设置里可以设定时关闭。到点关闭播放器后，浏览器结束视频读取。分段视频由 TG Drive 服务器按原文件顺序拼接并播放。 [Telegram 浏览器传输协议](https://core.telegram.org/mtproto/transports) · [Telegram 文件读取接口](https://core.telegram.org/method/upload.getFile)

首次直连时，在页面输入从 `my.telegram.org` 获取的 API ID 和 API Hash，再用有权查看存储频道的个人 Telegram 账号完成验证码和两步验证。浏览器把授权请求直接发给 Telegram，并可在同一个小程序标签页内复用会话。退出 TG Drive 或更换直连账号时会向 Telegram 撤销直连会话。关闭标签页后可以在 Telegram 的「设备」中结束对应的 TG Drive Browser 会话。 [Telegram 用户授权](https://core.telegram.org/api/auth)

直连播放需要 HTTPS、WebAssembly，以及设备能访问 Telegram WebSocket 数据中心。支持 Service Worker 的 WebView 使用浏览器的 Range 播放。无法启用 Service Worker 时，支持 Media Source 的 WebView 可在浏览器内实时封装带索引的普通 MP4，具体编码由设备解码能力决定。让 `/telegram-media-sw.js` 遵循源站的重新验证策略。 [WebKit Media Source 支持](https://webkit.org/blog/14735/webkit-features-in-safari-17-1/)

锁屏或切到后台时，定时器受设备系统的网页调度限制。直连失败时可选「经服务器播放」并使用 TG Drive 的定时关闭，也可选择「在 Telegram 中播放」打开原消息。 [Telegram 小程序接口](https://core.telegram.org/bots/webapps#initializing-mini-apps)

## 外部播放器直链

从文件菜单创建分享后，复制“外部播放器直链”到支持 HTTP URL 的播放器，例如 Kodi 的 `.strm` 文件可写入一行直链。已创建的分享可在“我的分享”中复制播放直链。接口在分享记录中返回 `stream_url`，格式为 `/s/{token}/download`，支持 GET、HEAD 和单段 Range，访问无需登录。任何持有链接的人都可读取文件，请按需要设置有效期；撤销、过期和移入回收站都会使链接停止提供内容。链接只对应单个文件，频道切换后需切回原频道才可访问。

封面优先使用 Telegram 静态缩略图。缺少封面时，服务端用 FFmpeg 从最多 8 MiB 的文件开头提取静态帧。超出范围才可读取索引或关键帧的文件显示视频图标。图床列表使用服务端生成的最长边 320 像素的 WebP 缩略图。Docker 镜像包含 FFmpeg，原生部署需安装 `ffmpeg`。浏览器私有缓存使用 ETag 重新校验，登录仍有效且内容相同时服务端返回 304。

频道视频优先以视频自带说明的首个有效段落命名，多行合并为一行，跳过独立链接和标签行，保留原始视频后缀，最长 255 个字符。视频自身没有有效说明时，会读取同一个 Telegram 相册里唯一的共用说明，例如与视频一起发送的封面图片上的文字；存在多个不同说明时不会猜测。相册关联以 Telegram 的 grouped_id 为准，不借用相邻独立消息的文字。没有有效简介时保留原文件名。

编辑或移除频道说明时，自动名称会跟随更新；在网盘手动重命名后则保留自定义名称。已有文件会在完整频道扫描时补齐；升级时对无法确认来源的旧名称按自定义名称保留。相册完整到达后会再次同步，支持文字与视频分批到达。

回归检查从仓库根目录运行 `python -m pytest -q`，并在 `frontend/` 运行 `npm test` 和 `npm run build`。构建前端并准备 Python Playwright、Chromium 与支持 libx264 的 FFmpeg 后，可运行 `python frontend/tests/video_seek_browser.py --media-cache-mib 32` 验证原生播放、拖动和刷新续播。脚本使用合成视频和模拟 Telegram 回源。

同样可运行 `python frontend/tests/telegram_direct_browser.py`，用测试会话和合成视频验证小程序定时断流、授权过期和播放回退。

## 服务端切片缓存

默认 `MEDIA_CACHE_MAX_MIB=512` 启用 512 MiB 缓存，设为 `0` 关闭。`MEDIA_CACHE_MIN_FREE_MIB=4096` 保留 4 GiB 磁盘可用空间，达到阈值时停止补齐并继续按需播放。落盘切片总量受容量限制，按最近访问时间淘汰。`MEDIA_CACHE_DIR` 默认位于 `DATA_DIR/media-slices`，建议使用本地 SSD 可写目录，并从数据备份中排除。内存盘也可使用，但容量与保留空间需要匹配；默认 4 GiB 保留空间不适合小容量 `/dev/shm`。

缓存服务已登录视频和分享视频的在线播放，包括起播、未对齐的跳转和完整播放请求。每次请求仍检查登录或分享权限及流量预算，缓存不会延长分享有效期。缓存键包含工作区、文件 ETag 和切片位置。`MEDIA_CACHE_SLICE_MIB=2` 按 2 MiB 对齐预读，可选 1、2 或 4。客户端需要的字节到达就发送，无需等待整块下载；同一块的并发请求共享填充，短探测结束后继续补齐。完整切片原子发布后才可跨请求从磁盘读取。

未对齐的冷跳转优先按当前切片内的实际范围回源，正常传输完成后才启动后台补齐，避免尾部索引请求先等待整个切片前缀。已有后台填充到达所需位置时继续共享；尚未到达时直接读取请求范围。中途断开的精确读取会关闭回源，不再为它启动新的补齐任务。

起播后会在后台读取 MP4 顶层 box 的头，跳过媒体载荷并预热最多 8 MiB 的 moov 索引。播放跨切片时在有空闲容量的情况下并发准备下一块。预热与预读共用填充队列，冷跳转仍可直接读取所需范围。

每个进程最多同时填充 8 块，填充超时为 60 秒，队列满或空间不足时按实际 Range 回源。填充保留原频道连接，频道切换后仍使用原工作区；服务关闭会取消填充并释放连接。`MEDIA_CACHE_TTL_SECONDS=600` 表示切片闲置 10 分钟后失效，运行期间定期清理。禁用缓存后不启动维护任务，可手动清理原缓存目录。

视频浏览器回归可加 `--media-cache-mib 16 --mobile --delay-ms 350`，使用合成视频和延迟回源验证缓存开启后的起播、拖动与权限流程，不连接 Telegram。真实 MP4 前置索引测试位于 `tests/test_video_prepare.py`，需要 FFmpeg；CI 会安装后运行。

登录后可从 `GET /api/traffic` 的 `media_cache` 字段查看切片命中次数、未命中次数、缓存发送字节、正在填充的块数，以及合并请求数 `coalesced` 和共享填充发送字节 `coalesced_bytes`。`overfetch_bytes` 记录相对首个请求多读的 Telegram 字节；后续请求可能复用这些数据。统计按进程启动累积，可结合服务端起播日志比较冷、热请求的首字节时间。发送客户端的字节继续计入 CDN 流量。

冷跳转正常完成后另行补齐的整块读取全部计入 `overfetch_bytes`；如果已有填充正在执行，则复用该任务，不重复启动补齐。

## 上传时前置 MP4 索引

默认 `VIDEO_FASTSTART=true`。浏览器上传完成后，只对索引位于媒体数据之后的普通 `.mp4` 执行 FFmpeg `-c copy -movflags +faststart`；已前置索引、分片 MP4、其他格式及无法识别的文件保持原样。不会重编码音视频，但需要完整文件 I/O，耗时随文件大小和磁盘速度变化。`VIDEO_FASTSTART_TIMEOUT=300` 限制单次处理时长。

临时目录需要同时容纳原文件及优化副本，副本预留原文件大小加 8 MiB，并纳入上传容量与磁盘剩余空间检查。缺少 FFmpeg、处理失败、超时或空间不足时仍上传原文件。取消或关闭服务会终止 FFmpeg，删除优化副本并释放预留空间。上传失败时保留原文件供重试。

进程启动时、上传任务开始前，清理临时目录内遗留的 `upload_*.faststart.part`，包括没有上传记录的孤立副本。工作区恢复和重试也会清理对应副本并保留原文件；取消或过期清理同时删除原文件及副本。删除副本失败时保留任务记录和原文件，供后续清理重试。

成功重封装后，存储文件的大小和 SHA256 按优化副本记录，下载得到优化后的 MP4；浏览器上传偏移和任务进度仍以原文件大小计算。需要逐字节保留原始 MP4 时设 `VIDEO_FASTSTART=false`。现有 Telegram 文件及频道同步文件不做回写，需要重新通过浏览器上传才能应用该优化。
