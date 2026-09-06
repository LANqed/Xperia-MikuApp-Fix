# Mikuxperia 兼容服务端

为 Xperia A Miku 联动（Xperia SO-04E，Android 4.2）的应用（Find Vocalo-P Player，Miku Weather，Miku Downloader，Find Creative News）提供兼容接口的 Python 服务端，同时附带一个中文WebUI面板。它提供新闻、下载器列表、歌曲播放列表、天气数据和媒体文件接口，并支持 AI 新闻汇总、Bilibili 收藏夹同步、网页上传歌曲和定时任务。

服务端本体只使用 Python 标准库：不需要 `pip install`，不需要 Docker，也不需要数据库。只有「同步 Bilibili 收藏夹」这一个可选功能需要额外安装 `yt-dlp` 与 `ffmpeg`。

本文档同时覆盖 Debian 11（systemd）与 Alpine Linux（OpenRC），以及 Nginx 与 Caddy 两种反向代理。操作系统与反向代理可以自由搭配，但同一台机器上只能启用 Nginx 或 Caddy 之一，不要让两者同时监听 80 与 443 端口。

## 目录

- [1. 项目简介](#1-项目简介)
- [2. 接口一览](#2-接口一览)
- [3. 配置文件 miku.conf](#3-配置文件-mikuconf)
- [4. music.json 数据结构](#4-musicjson-数据结构)
- [5. 快速开始（本机试运行）](#5-快速开始本机试运行)
- [6. 部署到 Debian 11（systemd）](#6-部署到-debian-11systemd)
- [7. 部署到 Alpine Linux（OpenRC）](#7-部署到-alpine-linuxopenrc)
- [8. 反向代理：Nginx](#8-反向代理nginx)
- [9. 反向代理：Caddy](#9-反向代理caddy)
- [10. HTTPS 与旧客户端的 HTTP 兼容](#10-https-与旧客户端的-http-兼容)
- [11. 防火墙与端口](#11-防火墙与端口)
- [12. 修改客户端 APK，指向本服务端](#12-修改客户端-apk指向本服务端)
- [13. Xperia 联动包（TWRP 刷入）](#13-xperia-联动包twrp-刷入)
- [14. 管理面板 WebUI](#14-管理面板-webui)
- [15. 定时任务](#15-定时任务)
- [16. 上传歌曲](#16-上传歌曲)
- [17. 手动维护歌曲与专辑](#17-手动维护歌曲与专辑)
- [18. 同步 Bilibili 收藏夹](#18-同步-bilibili-收藏夹)
- [19. AI 新闻与新歌动态](#19-ai-新闻与新歌动态)
- [20. 天气接口适配细节](#20-天气接口适配细节)
- [21. Docker 部署（可选）](#21-docker-部署可选)
- [22. 测试与自检](#22-测试与自检)
- [23. 故障排查](#23-故障排查)
- [24. 已知限制](#24-已知限制)
- [25. 安全注意事项](#25-安全注意事项)
- [26. 附录：Debian 与 Alpine 命令对照](#26-附录debian-与-alpine-命令对照)

## 1. 项目简介

### 1.1 服务提供的能力

| 能力 | 说明 | 依赖 |
| --- | --- | --- |
| 歌曲播放列表 | 把 `music.json` 与 `media/` 转换成旧客户端能解析的 XML 与下载地址 | 无 |
| 天气数据 | 把和风天气（QWeather）的 8 天预报转换成旧版天气小组件的 XML | QWeather 账号 |
| 新闻列表 | 静态占位内容，或由 Tavily 搜索 + AI 汇总生成的中文条目 | 可选 Tavily、OpenAI 兼容接口 |
| 下载器列表 | 返回应用列表与通知列表占位数据，让下载器 APP 不报错 | 无 |
| 管理面板 | `/admin`，Material Design 2 风格，含健康状态、访问统计、任务控制、上传歌曲 | 无 |
| 定时任务 | 天气刷新、AI 新闻刷新、Bilibili 同步、媒体缓存清理 | 无 |
| Bilibili 收藏夹同步 | 把收藏夹音频下载为 MP3 并生成播放列表 | `yt-dlp`、`ffmpeg` |

### 1.2 运行环境要求

- Python 3.9 或更高版本（代码使用了 `str.removeprefix`）。Debian 11 自带 3.9，Alpine 3.16 及以上自带 3.10 或更新版本，都已满足。
- 服务端本体不需要任何第三方 Python 包。
- 仅在使用 Bilibili 同步时需要：`yt-dlp`（解析与下载）和 `ffmpeg`（转换 MP3、缩放封面）。
- 建议至少 1 GB 内存。媒体文件占用取决于歌曲数量；128 kbps 的 MP3 大约 1 MB/分钟。

### 1.3 文件与模块

| 文件 | 作用 |
| --- | --- |
| `server.py` | 主程序：加载配置、HTTP 路由、XML 生成、天气适配、任务注册、`--check` 自检 |
| `webui.py` | 管理面板：登录会话、页面渲染（内联 CSS）、访问统计、Bilibili 缓存概览 |
| `tasks.py` | 定时任务调度器：注册、间隔与开关、状态持久化、后台执行 |
| `uploads.py` | 解析 `multipart/form-data`、校验上传文件、写入 `music.json` |
| `ainews.py` | Tavily 搜索与 OpenAI 兼容接口摘要、结果缓存 |
| `bilicookies.py` | Bilibili Cookie 解析（Netscape 与请求头两种格式）、生成 `yt-dlp` 用临时文件 |
| `sync_bilibili_favorites.py` | 独立可执行的收藏夹同步脚本，也被面板任务以子进程方式调用 |
| `test_server.py` | 单元测试（`unittest`） |
| `miku.conf.example` | 配置模板。真实的 `miku.conf` 会被 git 忽略（含密钥），部署时复制本模板填写 |
| `music.json` | 播放列表与歌曲目录 |
| `media/` | 媒体根目录：音频、封面、歌词 |
| `Dockerfile`、`compose.example.yml` | 可选的容器部署配置 |
| `apk/` | 设备上预装的四个 Miku 应用 APK，改机对象，见[第 12 节](#12-修改客户端-apk指向本服务端) |
| `apktool_2.9.3.jar`、`uber-apk-signer-1.3.0.jar` | APK 解包/重打包与签名工具 |
| `patch-apk.ps1`、`patch-apk.sh` | 一键改机脚本（见第 12.1 节） |
| `.github/workflows/patch-apk.yml` | 改机用的 GitHub Action（见第 12.2 节） |
| `Xperia_feat_original_FE_V5a.zip` | Xperia 联动包，TWRP 刷入，见[第 13 节](#13-xperia-联动包twrp-刷入) |

`server.py` 会导入 `ainews`、`tasks`、`uploads`、`webui`，而 `webui` 会导入 `bilicookies`。缺少其中任何一个文件，服务都无法启动。上表后五行与服务器运行无关，仅在给设备侧改机、刷机时使用。

### 1.4 运行时生成的文件

| 文件或目录 | 内容 | 由谁创建 |
| --- | --- | --- |
| `webui-stats.json` | 访问统计，保留最近 120 天 | 面板启用后自动写入 |
| `ainews-cache.json` | 最近一次成功的新闻结果 | AI 新闻刷新成功后写入 |
| `tasks-state.json` | 各任务的间隔、开关、上次执行时间与结果 | 任务首次保存状态时写入 |
| `media/uploads/` | 面板上传的音频、封面、歌词 | 首次上传时创建 |
| `media/bilibili/<收藏夹ID>/` | 同步下载的 MP3 与封面 | 同步脚本创建 |
| `bilibili.cookies.txt` | Bilibili 登录 Cookie，需要手动放置 | 用户提供 |

这些文件都在 `miku.conf` 所在目录下解析（除 `media/` 子目录外），路径可以在配置里改成绝对路径。

## 2. 接口一览

### 2.1 GET 接口

| 路径 | 用途 | 返回 |
| --- | --- | --- |
| `/resources/xml/MikuNews/list.xml` | Miku News 新闻列表 | `<mikunews>` XML |
| `/resources/xml/MikuDownloader/applist.xml` | 下载器应用列表 | `<mikudownloaderapps>` XML |
| `/resources/xml/MikuDownloader/noticelist.xml` | 下载器通知列表 | `<mikudownloaderapps>` XML |
| `/resources/xml/FeatureSongsPlayer/playlist.xml` | 歌曲播放列表与文件大小 | `<featuresongsplayer>` XML |
| `/healthz` | 健康检查 | JSON，状态为 `error` 时 HTTP 503 |
| `/admin`、`/admin/` | 管理面板，未登录时显示登录页 | HTML |
| `/admin/api/status` | 面板数据快照，需要登录 | JSON，未登录返回 401 |
| `/media/<相对路径>` | 音频、封面、歌词 | 对应 MIME 类型 |
| `/`、`/pages/*` | 占位页面（客户端里的「详情」链接会指向这里） | HTML |

未匹配的 GET 路径返回纯文本 `Not found`，但状态码是 200（`/admin` 下的未知子路径和不存在的媒体文件才返回 404）。

### 2.2 POST 接口

| 路径 | 表单字段 | 用途 |
| --- | --- | --- |
| `/getdata.php` | `APID`、`AREA` | 旧版天气小组件取数据，`APID` 必须是 `SO-04E_OTENKIMIKU` |
| `/feature_songs_provider/addresses` | `feature_songs_provider[{N}][...]` | 客户端用文件名换真实下载地址 |
| `/admin/login` | `password` | 登录，成功后 303 跳转并下发 `miku_admin` Cookie |
| `/admin/logout` | 无 | 退出登录 |
| `/admin/task/run` | `task` | 立即执行某个任务（后台线程） |
| `/admin/task/interval` | `task`、`interval`、`unit` | 修改任务间隔，`unit` 为 `seconds`/`minutes`/`hours`/`days` |
| `/admin/task/toggle` | `task`、`enabled` | 开关任务的自动执行 |
| `/admin/news/refresh` | 无 | 等价于执行 `news` 任务 |
| `/admin/upload` | `multipart/form-data` | 上传歌曲 |

除 `/admin/login` 外，所有 `/admin` 下的 POST 都需要有效会话，未登录会 303 跳回 `/admin`。表单全部使用 `application/x-www-form-urlencoded`，只有 `/admin/upload` 使用 `multipart/form-data`。

### 2.3 媒体文件解析规则

- `/media/` 开头的请求保留后面的相对路径，因此支持 `uploads/x.mp3`、`bilibili/123456789/BV1xx.mp3` 这类子目录。
- 其他以 `.mp3 .m4a .aac .ogg .wav .webm .png .jpg .jpeg .webp .txt` 结尾的请求，只取路径最后一段当文件名，用于兼容客户端拼接出的奇怪路径。
- 路径会做越界检查，`..` 一律拒绝，只能读取媒体根目录下的文件。
- `_empty.txt` 返回空文本，供没有歌词的歌曲使用。
- `debug.wav`、`debug.png`、`debug.txt` 在媒体目录中不存在时，由程序在内存里生成（3 秒 440 Hz 正弦音、64×64 PNG、两行示例歌词），方便调试。
- 文件是一次性读入内存后整体返回的，不支持 Range 请求，详见[已知限制](#24-已知限制)。

### 2.4 健康检查

```bash
curl -s http://127.0.0.1:8080/healthz
```

```json
{"status": "ok", "playlists": 1, "songs": 6, "news_items": 8, "today_requests": 42}
```

`status` 有三种取值：

- `ok`：全部检查项通过，HTTP 200。
- `warn`：只有提示项，例如天气尚未请求过、AI 新闻未配置，HTTP 200。
- `error`：存在故障，例如 `music.json` 校验失败或媒体目录不存在，HTTP 503，便于监控系统识别。

检查项包括：播放列表配置、媒体目录、天气接口凭据、天气数据缓存、AI 新闻。`/healthz` 与 `/admin` 的请求不计入访问统计。

## 3. 配置文件 miku.conf

`miku.conf` 是唯一的配置文件，UTF-8 编码，标准 INI 格式。注释必须单独一行，以 `#` 或 `;` 开头。

由于它存放密钥与面板密码，已加入 `.gitignore` 不会入库；仓库随附 `miku.conf.example`
模板。首次使用先 `cp miku.conf.example miku.conf`（见第 5 节），再编辑填写。

`[server]`、`[qweather]`、`[weather]`、`[music]` 四个小节必须存在，缺少任何一个都会在启动时报 `missing config sections`。`[webui]`、`[ainews]`、`[tasks]`、`[bilibili]` 是可选的：缺少时管理面板返回 404、新闻使用静态占位内容、定时任务使用内置默认值、Bilibili 同步不可用，服务本身照常运行。

### 3.1 公网部署推荐配置

```ini
[server]
listen_host = 127.0.0.1
listen_port = 8080
public_base_url = http://miku.example.com

[qweather]
api_host = 你的账号专属Host.qweatherapi.com
api_key = 你的和风天气APIKey
bearer_token =
cache_seconds = 1800

[weather]
city = 南海
latitude = 22.82989
longitude = 113.01677

[music]
media_root = media
catalog = music.json
```

要点：

- `listen_host = 127.0.0.1` 表示只允许本机的反向代理访问，8080 端口不直接暴露公网。容器部署必须改成 `0.0.0.0`，否则端口映射不通。
- `public_base_url` 填客户端实际访问的根地址，末尾不要加 `/`。留空时服务端会用请求里的 `Host` 头和 `X-Forwarded-Proto` 拼出地址，适合局域网调试。
- `api_host` 填 QWeather 控制台显示的账号专属 Host，不要填完整 URL；代码会自动去掉开头的 `https://` 和结尾的 `/`。
- `api_key` 与 `bearer_token` 二选一，同时填写时优先使用 `bearer_token`。
- `city`、`latitude`、`longitude` 是服务端固定的天气位置，客户端传来的旧 `AREA` 值会被忽略。
- 相对路径（`media_root`、`catalog` 以及各类状态文件）都相对于 `miku.conf` 所在目录解析。

### 3.2 配置项速查表

`[server]`

| 选项 | 默认值 | 说明 |
| --- | --- | --- |
| `listen_host` | `0.0.0.0` | 监听地址 |
| `listen_port` | `8080` | 监听端口，必须在 1–65535 之间 |
| `public_base_url` | 空 | 对外根地址，留空则使用请求 Host |

`[qweather]`

| 选项 | 默认值 | 说明 |
| --- | --- | --- |
| `api_host` | 空 | 账号专属 API Host，缺失时天气接口报错 |
| `api_key` | 空 | 以 `X-QW-Api-Key` 头发送 |
| `bearer_token` | 空 | 以 `Authorization: Bearer` 头发送，优先级更高 |
| `cache_seconds` | `1800` | 预报缓存时长，同时是天气任务的默认间隔 |

`[weather]`

| 选项 | 默认值 | 说明 |
| --- | --- | --- |
| `city` | `北京` | 显示在天气小组件上的地名 |
| `latitude` | `39.92` | 纬度，范围 −90–90 |
| `longitude` | `116.41` | 经度，范围 −180–180 |

`[music]`

| 选项 | 默认值 | 说明 |
| --- | --- | --- |
| `media_root` | `media` | 媒体根目录 |
| `catalog` | `music.json` | 播放列表文件 |

`[webui]`（可选，详见[第 14 节](#14-管理面板-webui)）

| 选项 | 默认值 | 说明 |
| --- | --- | --- |
| `enabled` | `false` | 必须为 true 且 `password` 非空，面板才可用 |
| `password` | 空 | 明文保存的管理密码 |
| `session_hours` | `12` | 会话有效期，自动限制在 1–720 小时 |
| `stats_file` | `webui-stats.json` | 访问统计文件，保留最近 120 天 |

`[ainews]`（可选，详见[第 19 节](#19-ai-新闻与新歌动态)）

| 选项 | 默认值 | 说明 |
| --- | --- | --- |
| `enabled` | `false` | 与 `tavily_api_key` 同时具备才算已配置 |
| `tavily_api_key` | 空 | Tavily API Key |
| `tavily_endpoint` | `https://api.tavily.com/search` | 搜索接口地址 |
| `tavily_max_results` | `8` | 每个查询的结果数，限制在 1–20 |
| `tavily_topic` | `news` | Tavily 的 topic 参数 |
| `search_queries` | 三条内置查询 | 每行一个查询，结果按链接去重后合并 |
| `openai_base_url` | 空 | 填到 `/v1` 为止，程序自动拼 `/chat/completions` |
| `openai_api_key` | 空 | 摘要模型的 Key |
| `openai_model` | 空 | 模型名，例如 `gpt-4o-mini` |
| `refresh_seconds` | `3600` | 缓存有效期，最小 300，同时是新闻任务的默认间隔 |
| `max_items` | `8` | 最终条目数，限制在 1–30 |
| `timeout_seconds` | `30` | 单次 HTTP 超时，限制在 5–120 |
| `cache_file` | `ainews-cache.json` | 结果缓存文件 |

`openai_base_url`、`openai_api_key`、`openai_model` 三项必须同时填写才会启用 AI 摘要；只填一两项等于不启用。

`[tasks]`（可选，详见[第 15 节](#15-定时任务)）

| 选项 | 默认值 | 说明 |
| --- | --- | --- |
| `state_file` | `tasks-state.json` | 任务状态文件，其中的值优先于本节配置 |
| `weather_enabled` | `true` | 天气任务开关 |
| `weather_interval_seconds` | `[qweather] cache_seconds` | 天气任务间隔，最小 300 |
| `news_enabled` | `true` | 新闻任务开关 |
| `news_interval_seconds` | `[ainews] refresh_seconds` | 新闻任务间隔，最小 300 |
| `bilibili_enabled` | `false` | 同步任务开关 |
| `bilibili_interval_seconds` | `21600` | 同步任务间隔，最小 600 |
| `bilibili_timeout_seconds` | `3600` | 同步子进程超时，最小 60 |
| `cache_cleanup_enabled` | `true` | 缓存清理任务开关，会删除文件，务必先看第 15 节 |
| `cache_cleanup_interval_seconds` | `604800` | 缓存清理间隔，最小 86400 |

`[bilibili]`（可选，详见[第 18 节](#18-同步-bilibili-收藏夹)）

| 选项 | 默认值 | 说明 |
| --- | --- | --- |
| `folder_ids` | 空 | 收藏夹 ID，可用换行、空格或逗号分隔，必须是不重复的正整数 |
| `cookie_file` | `bilibili.cookies.txt` | Cookie 文件路径 |
| `media_root` | `media/bilibili` | 音频与封面缓存目录 |
| `catalog` | `music.json` | 要写入的播放列表文件 |
| `page_size` | `20` | 收藏夹接口分页大小，限制在 1–20 |
| `max_videos_per_folder` | `0` | 每个收藏夹最多处理多少个视频，0 表示不限制 |
| `songs_per_playlist` | `50` | 每个播放列表最多多少首，超出会拆分成多个列表，限制在 1–60 |
| `redownload_existing` | `false` | 为 true 时重新下载已缓存的音频 |
| `combined_enabled` | `false` | 为 true 时在保留各收藏夹播放列表的同时，追加一个包含全部成功下载歌曲的合集（跨收藏夹按 BV 号去重，超出 `songs_per_playlist` 同样拆分） |
| `combined_title` | `Bilibili 收藏夹合集` | 合集播放列表标题，多片时自动追加 `(1/3)` 后缀 |

### 3.3 容易踩的配置陷阱

1. **数值项不能留空。** `latitude`、`longitude`、`listen_port`、`cache_seconds` 这类选项一旦写成 `latitude =` 而不删掉，Python 的 `configparser` 不会使用默认值，而是直接抛错：

   ```text
   ValueError: could not convert string to float: ''
   ```

   仓库自带的 `miku.conf` 模板里 `latitude` 与 `longitude` 就是空的，部署前必须填上真实坐标，否则服务无法启动。要使用默认值，就把整行删掉。

2. **`server.py` 在被导入时就会读取同目录下的 `miku.conf`。** 即使用 `--config` 指定了别的路径，`server.py` 旁边的 `miku.conf` 也必须存在且可解析，否则连 `--check` 都跑不起来。

3. **`[bilibili]` 直接写在 `miku.conf` 里。** 仓库中没有单独的 `bilibili.conf.example`，把 `[bilibili]` 小节追加到 `miku.conf` 末尾即可。

4. **面板密码是明文保存的。** 模板里的 `password = 123456` 只用于本地试跑，公网部署前必须换成强密码，并把配置文件权限设为 `640`。

## 4. music.json 数据结构

顶层只有一个 `playlists` 数组：

```json
{
  "playlists": [
    {
      "id": 1,
      "version": 1,
      "title": "ミクスペリエンス e.p.",
      "description": "完整专辑介绍",
      "brief_description": "简短介绍",
      "image": "Cover.jpg",
      "brief_image": "Cover_thumb.jpg",
      "songs": [
        {
          "id": "01",
          "title": "Opening -Hatsune Calling-",
          "artist": "円盤P",
          "date": "2013.08.28",
          "music": "01.mp3",
          "thumbnail": "Cover.jpg",
          "lyrics": "",
          "link": "http://example.com"
        }
      ]
    }
  ]
}
```

### 4.1 播放列表字段

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `id` | 整数 | 是 | 唯一正整数，且不得超过 `2147483647` |
| `version` | 整数 | 是 | 正整数，内容变化时必须递增 |
| `title` | 字符串 | 否 | 列表标题 |
| `description` | 字符串 | 否 | 详情页介绍 |
| `brief_description` | 字符串 | 否 | 列表页简介 |
| `image` | 字符串 | 否 | 详情页封面，相对媒体根目录 |
| `brief_image` | 字符串 | 否 | 列表页封面，缺省时用 `image` |
| `songs` | 数组 | 否 | 歌曲对象列表 |
| `source` | 字符串 | 否 | 由 Bilibili 同步写入，固定为 `bilibili-favorites` |
| `source_folder_id` | 整数 | 否 | 由 Bilibili 同步写入，来源收藏夹 ID |
| `source_part`、`source_parts` | 整数 | 否 | 由 Bilibili 同步写入，拆分序号与总片数 |

`id` 的上限来自客户端：Android 端用 `Integer.parseInt` 解析播放列表 ID，超过 32 位有符号整数上限会让 `MusicDataService` 崩溃。`server.py --check` 会拦住这种配置。带 `source = bilibili-favorites` 的播放列表由同步脚本全权管理，每次同步都会被整体替换，不要手工编辑，也不能作为上传目标。

### 4.2 歌曲字段

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `id` | 字符串 | 是 | 同一播放列表内唯一且非空 |
| `title` | 字符串 | 否 | 缺省时显示文件名 |
| `artist` | 字符串 | 否 | 缺省显示 `Unknown` |
| `date` | 字符串 | 否 | 建议 `YYYY.MM.DD`，缺省用当天 |
| `music` | 字符串 | 是 | 音频文件，相对媒体根目录 |
| `thumbnail` | 字符串 | 否 | 封面文件，缺省用 `debug.png` |
| `lyrics` | 字符串 | 否 | UTF-8 歌词文件，留空表示无歌词 |
| `link` | 字符串 | 否 | 客户端「详情」按钮的跳转地址 |

### 4.3 校验规则

`python3 server.py --check` 与面板的健康检查会验证：

- 每个播放列表都有整数 `id` 与 `version`，`id` 唯一、为正且不超过 32 位上限，`version` 为正。
- 每首歌都有非空且列表内唯一的 `id`，以及非空的 `music`。
- 所有被引用的音频、封面、歌词文件都真实存在于媒体根目录内；缺失会报 `media file not found: 文件名`。
- 文件名不能通过 `..` 跳出媒体根目录。
- Linux 区分大小写，`Cover.JPG` 与 `Cover.jpg` 是两个文件。

歌词与封面允许为空字符串，此时文件大小按 0 处理，客户端会拿到空文件名；缺歌词的歌曲在地址接口里会指向 `/media/_empty.txt`。

### 4.4 版本号与客户端更新时间

旧客户端靠播放列表的 `pubDate` 判断是否需要重新拉取，服务端会把 `version` 映射成一个随版本递增的日期：

| version 取值 | 映射方式 |
| --- | --- |
| 小于 `100000` | 2020-01-01（UTC+9）加上 `version` 天 |
| `100000` 到 `999999999` | 2020-01-01（UTC+9）加上 `version` 分钟 |
| 不小于 `1000000000` | 视为旧版同步写入的 Unix 时间戳，先折算成「2026-01-01 起的分钟数」再按分钟映射 |

手工维护时保持 `1`、`2`、`3` 这样的小整数即可，每次改动加一。Bilibili 同步脚本写入的是「2026-01-01 起的分钟数」，落在分钟映射区间内，因此每次同步都会得到一个更新的日期。

### 4.5 客户端文件名压平规则

Android 4.2 客户端会把 `musicFileName` 直接交给 `File()`，文件名里带斜杠会抛 `IllegalArgumentException`。因此播放列表 XML 里的文件名会被压平成：

```text
<播放列表ID>_<文件名>
```

例如 `music` 是 `bilibili/3275482587/BV1xx.mp3`、播放列表 ID 是 `1275482587`，XML 中的 `musicFileName` 就是 `1275482587_BV1xx.mp3`。客户端拿着这个名字来请求地址接口时，服务端会反查 `music.json`，还原成真实相对路径再生成 `/media/...` 下载地址。

由此带来两条实践建议：

- `music.json` 里的路径统一使用正斜杠，不要写 Windows 风格的反斜杠。
- 服务端生成下载 URL 时不会做百分号编码，手工添加的文件名请只用 ASCII 字符，避免空格与中文。

## 5. 快速开始（本机试运行）

首次使用（或 clone 仓库后）先复制配置模板；真实配置 `miku.conf` 已被 git 忽略，
不会入库：

```bash
cp miku.conf.example miku.conf
```

然后把配置改好（至少填上 `[weather]` 的经纬度和 `[qweather]` 的凭据），再做一次离线自检：

```bash
cd server
python3 server.py --check
```

成功时输出：

```text
Configuration OK: 1 playlist(s), 6 song(s)
```

命令行只有两个参数：`--config <路径>` 指定配置文件（默认同目录的 `miku.conf`），`--check` 只做校验然后退出。

启动服务：

```bash
python3 server.py
# 或
python3 server.py --config /opt/mikuxperia-server/miku.conf
```

启动日志会打印配置路径、监听地址、面板状态和每个任务的间隔：

```text
Config: /opt/mikuxperia-server/miku.conf
Serving on http://0.0.0.0:8080
Admin WebUI: http://0.0.0.0:8080/admin
Task weather: enabled, every 30 分
Task bilibili: disabled, every 6 小时
Task news: enabled, every 1 小时
Task cache_cleanup: enabled, every 7 天
```

另开一个终端验证各接口：

```bash
# 健康检查
curl -s http://127.0.0.1:8080/healthz

# 播放列表
curl -s http://127.0.0.1:8080/resources/xml/FeatureSongsPlayer/playlist.xml

# 天气（APID 必须完全一致，否则返回 502）
curl -s -X POST http://127.0.0.1:8080/getdata.php \
  -d 'APID=SO-04E_OTENKIMIKU' -d 'AREA=4410'

# 地址接口（模拟客户端用压平后的文件名换下载地址）
curl -s -X POST http://127.0.0.1:8080/feature_songs_provider/addresses \
  --data-urlencode 'feature_songs_provider[{0}][playlist_id]=1' \
  --data-urlencode 'feature_songs_provider[{0}][musicFileName]=1_01.mp3'

# 媒体文件
curl -sI http://127.0.0.1:8080/media/01.mp3
```

按 `Ctrl+C` 停止临时进程。

## 6. 部署到 Debian 11（systemd）

以下步骤把服务安装到 `/opt/mikuxperia-server`，用专用低权限用户运行，8080 只监听本机，公网流量由反向代理转发。

### 6.1 准备工作

需要准备：

- 一台公网服务器，Debian 11
- 具备 `sudo` 权限的账号
- 一个域名，例如 `miku.example.com`
- QWeather 开发者账号、API Host 与 API Key
- 服务器放行 TCP 80 与 443

### 6.2 安装运行环境

```bash
sudo apt update
sudo apt upgrade -y
sudo apt install -y python3 nginx ca-certificates curl rsync
python3 --version
```

版本应为 3.9 或更高。服务端只用标准库，不需要执行 `pip install`。

### 6.3 配置 DNS

在域名服务商处添加 A 记录：

```text
主机记录：miku
记录类型：A
记录值：服务器公网 IPv4
```

生效后检查：

```bash
getent hosts miku.example.com
```

只有 IPv6 的服务器需要额外配置 AAAA 记录，并确认客户端网络也支持 IPv6。

### 6.4 创建用户与目录

```bash
sudo useradd --system --home /opt/mikuxperia-server --shell /usr/sbin/nologin mikuxperia
sudo mkdir -p /opt/mikuxperia-server
sudo chown -R "$USER":"$USER" /opt/mikuxperia-server
```

### 6.5 上传文件

在本地项目目录执行：

```bash
rsync -av --exclude='__pycache__' server/ 用户名@服务器IP:/opt/mikuxperia-server/
```

服务器上至少应有：

```text
/opt/mikuxperia-server/server.py
/opt/mikuxperia-server/webui.py
/opt/mikuxperia-server/tasks.py
/opt/mikuxperia-server/uploads.py
/opt/mikuxperia-server/ainews.py
/opt/mikuxperia-server/bilicookies.py
/opt/mikuxperia-server/sync_bilibili_favorites.py
/opt/mikuxperia-server/miku.conf
/opt/mikuxperia-server/music.json
/opt/mikuxperia-server/media/
```

### 6.6 设置权限

```bash
sudo chown -R mikuxperia:mikuxperia /opt/mikuxperia-server
sudo chmod 750 /opt/mikuxperia-server
sudo chmod 640 /opt/mikuxperia-server/miku.conf
sudo find /opt/mikuxperia-server/media -type f -exec chmod 640 {} \;
```

`miku.conf` 含 QWeather 凭据和面板密码，不要设成 `644`，也不要提交到公开仓库。

要使用上传歌曲、Bilibili 同步、访问统计、任务状态持久化，以下路径必须对 `mikuxperia` 用户可写：

```bash
sudo chmod 660 /opt/mikuxperia-server/music.json
sudo chmod 750 /opt/mikuxperia-server/media
```

状态文件（`webui-stats.json`、`ainews-cache.json`、`tasks-state.json`）由程序自动创建，因此 `/opt/mikuxperia-server` 目录本身也要对该用户可写。

### 6.7 自检并临时启动

```bash
cd /opt/mikuxperia-server
sudo -u mikuxperia python3 server.py --check
sudo -u mikuxperia python3 server.py --config /opt/mikuxperia-server/miku.conf
```

另开一个 SSH 窗口：

```bash
curl -i http://127.0.0.1:8080/healthz
curl -i http://127.0.0.1:8080/resources/xml/FeatureSongsPlayer/playlist.xml
```

确认返回 200 后按 `Ctrl+C` 停止。

### 6.8 创建 systemd 服务

```bash
sudo nano /etc/systemd/system/mikuxperia.service
```

```ini
[Unit]
Description=Mikuxperia compatibility server
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=mikuxperia
Group=mikuxperia
WorkingDirectory=/opt/mikuxperia-server
ExecStart=/usr/bin/python3 /opt/mikuxperia-server/server.py --config /opt/mikuxperia-server/miku.conf
Restart=on-failure
RestartSec=5
NoNewPrivileges=true
PrivateTmp=true
ProtectHome=true
ProtectSystem=full
ReadWritePaths=/opt/mikuxperia-server

[Install]
WantedBy=multi-user.target
```

`ReadWritePaths` 必须覆盖所有需要写入的路径。如果把媒体目录或状态文件放到了 `/opt/mikuxperia-server` 之外，要在这里补上对应路径。

启用并启动：

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now mikuxperia.service
sudo systemctl status mikuxperia.service --no-pager
curl -i http://127.0.0.1:8080/healthz
```

### 6.9 日常运维

```bash
# 实时日志
sudo journalctl -u mikuxperia.service -f

# 最近 100 行
sudo journalctl -u mikuxperia.service -n 100 --no-pager

# 修改 miku.conf、music.json 或媒体文件后重启
sudo systemctl restart mikuxperia.service
```

通过管理面板上传歌曲、执行任务、修改任务间隔都不需要重启；只有改动 `miku.conf` 才需要。

## 7. 部署到 Alpine Linux（OpenRC）

流程与 Debian 相同，区别是包管理器用 `apk`、服务管理用 OpenRC。服务端代码本身不需要任何修改。

### 7.1 安装运行环境

```sh
apk update
apk upgrade
apk add python3 curl ca-certificates rsync tzdata
python3 --version
```

如需 Bilibili 同步：

```sh
apk add py3-pip ffmpeg
python3 -m pip install --break-system-packages --upgrade yt-dlp
yt-dlp --version
```

如果 pip 拒绝安装，改用虚拟环境并做软链接，保证同步脚本能在 PATH 里找到 `yt-dlp`：

```sh
python3 -m venv /opt/mikuxperia-venv
/opt/mikuxperia-venv/bin/pip install --upgrade yt-dlp
ln -sf /opt/mikuxperia-venv/bin/yt-dlp /usr/local/bin/yt-dlp
```

### 7.2 创建用户与目录

Alpine 使用 BusyBox 的 `adduser`，参数与 Debian 不同：

```sh
addgroup -S mikuxperia
adduser -S -D -H -h /opt/mikuxperia-server -s /sbin/nologin -G mikuxperia mikuxperia
mkdir -p /opt/mikuxperia-server
```

上传文件后设置权限：

```sh
chown -R mikuxperia:mikuxperia /opt/mikuxperia-server
chmod 750 /opt/mikuxperia-server
chmod 640 /opt/mikuxperia-server/miku.conf
chmod 660 /opt/mikuxperia-server/music.json
find /opt/mikuxperia-server/media -type f -exec chmod 640 {} \;
```

### 7.3 手动测试

```sh
cd /opt/mikuxperia-server
su -s /bin/sh mikuxperia -c "python3 server.py --check"
su -s /bin/sh mikuxperia -c "python3 server.py --config /opt/mikuxperia-server/miku.conf"
```

另开一个终端：

```sh
curl -i http://127.0.0.1:8080/healthz
```

### 7.4 创建 OpenRC 服务

```sh
nano /etc/init.d/mikuxperia
```

```sh
#!/sbin/openrc-run

name="mikuxperia"
description="Mikuxperia compatibility server"

command="/usr/bin/python3"
command_args="/opt/mikuxperia-server/server.py --config /opt/mikuxperia-server/miku.conf"
command_user="mikuxperia:mikuxperia"
command_background="yes"
directory="/opt/mikuxperia-server"
pidfile="/run/${RC_SVCNAME}.pid"
output_log="/var/log/mikuxperia.log"
error_log="/var/log/mikuxperia.log"

depend() {
	need net
	after firewall
}

start_pre() {
	checkpath --file --owner mikuxperia:mikuxperia --mode 0640 "$output_log"
}
```

启动并设置开机自启：

```sh
chmod +x /etc/init.d/mikuxperia
rc-update add mikuxperia default
rc-service mikuxperia start
rc-service mikuxperia status
curl -i http://127.0.0.1:8080/healthz
```

常用操作：

```sh
rc-service mikuxperia restart
rc-service mikuxperia stop
tail -f /var/log/mikuxperia.log
```

### 7.5 Alpine 上的反向代理

Caddy：

```sh
apk add caddy
nano /etc/caddy/Caddyfile      # 内容与第 9 节相同
caddy validate --config /etc/caddy/Caddyfile
rc-update add caddy default
rc-service caddy start
```

Nginx（Alpine 用 `/etc/nginx/http.d/`，没有 `sites-available` 与 `sites-enabled`）：

```sh
apk add nginx
mkdir -p /etc/nginx/http.d
nano /etc/nginx/http.d/mikuxperia.conf   # 内容与第 8 节相同
nginx -t
rc-update add nginx default
rc-service nginx start
```

### 7.6 Alpine 上的定时同步

Alpine 没有 systemd 定时器，用 crond 代替（面板里的定时任务同样可用，这里适合完全关掉面板的场景）：

```sh
apk add busybox-suid
rc-update add crond default
rc-service crond start
crontab -u mikuxperia -e
```

每 6 小时同步一次：

```cron
0 */6 * * * cd /opt/mikuxperia-server && /usr/bin/python3 sync_bilibili_favorites.py --config /opt/mikuxperia-server/miku.conf >> /var/log/mikuxperia-sync.log 2>&1
```

```sh
touch /var/log/mikuxperia-sync.log
chown mikuxperia:mikuxperia /var/log/mikuxperia-sync.log
chmod 640 /var/log/mikuxperia-sync.log
tail -f /var/log/mikuxperia-sync.log
```

## 8. 反向代理：Nginx

```bash
sudo nano /etc/nginx/sites-available/mikuxperia.conf
```

```nginx
server {
    listen 80;
    listen [::]:80;
    server_name miku.example.com;

    # 面板上传单次上限是 80 MB，这里要留出余量
    client_max_body_size 100m;

    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 60s;
    }
}
```

启用并检查：

```bash
sudo ln -s /etc/nginx/sites-available/mikuxperia.conf /etc/nginx/sites-enabled/mikuxperia.conf
sudo nginx -t
sudo systemctl reload nginx
curl -i http://miku.example.com/healthz
```

如果访问后看到默认 Nginx 欢迎页，检查 `server_name`、DNS，并移除默认站点：

```bash
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t
sudo systemctl reload nginx
```

`X-Real-IP` 会影响面板的独立访客统计；不转发这个头时所有访客都会被算成同一个 IP。

## 9. 反向代理：Caddy

Caddy 的优势是自动申请和续期证书，不需要 Certbot。选择 Caddy 就不要再启用 Nginx。

### 9.1 安装（Debian 11）

```bash
sudo apt install -y debian-keyring debian-archive-keyring apt-transport-https curl gnupg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | sudo tee /etc/apt/sources.list.d/caddy-stable.list
sudo apt update
sudo apt install -y caddy
caddy version
```

如果之前启用过 Nginx，先停掉避免端口冲突：

```bash
sudo systemctl disable --now nginx
```

### 9.2 Caddyfile

```bash
sudo nano /etc/caddy/Caddyfile
```

自动 HTTPS（推荐，前提是客户端能连 HTTPS）：

```caddyfile
miku.example.com {
	encode zstd gzip

	reverse_proxy 127.0.0.1:8080 {
		header_up Host {host}
		header_up X-Real-IP {remote_host}
		header_up X-Forwarded-For {remote_host}
		header_up X-Forwarded-Proto {scheme}
	}
}
```

如果 APK 仍然使用 HTTP，用 `http://` 前缀显式声明，Caddy 就不会强制跳转 HTTPS：

```caddyfile
http://miku.example.com {
	encode zstd gzip

	reverse_proxy 127.0.0.1:8080 {
		header_up Host {host}
		header_up X-Real-IP {remote_host}
		header_up X-Forwarded-For {remote_host}
		header_up X-Forwarded-Proto {scheme}
	}
}
```

也可以同时提供两个入口，让旧设备走 HTTP、新客户端走 HTTPS：

```caddyfile
http://miku.example.com {
	reverse_proxy 127.0.0.1:8080
}

https://miku.example.com {
	reverse_proxy 127.0.0.1:8080
}
```

Caddyfile 的缩进必须统一使用制表符或统一使用空格，不要混用。

### 9.3 校验并启动

```bash
sudo caddy fmt --overwrite /etc/caddy/Caddyfile
sudo caddy validate --config /etc/caddy/Caddyfile
sudo systemctl enable --now caddy
sudo systemctl reload caddy
sudo systemctl status caddy --no-pager
curl -i http://miku.example.com/healthz
sudo journalctl -u caddy -n 100 --no-pager
```

### 9.4 媒体传输超时

媒体文件较大时可以延长上游超时：

```caddyfile
miku.example.com {
	reverse_proxy 127.0.0.1:8080 {
		transport http {
			response_header_timeout 60s
			read_timeout 120s
		}
	}
}
```

Caddy 默认不限制请求体大小，因此上传功能不需要额外配置。使用 Caddy 时跳过第 10 节的 Certbot 步骤，证书由 Caddy 自动管理；自动签发要求 80 与 443 从公网可达且 DNS 已正确解析。

## 10. HTTPS 与旧客户端的 HTTP 兼容

Mikuxperia 的 Android 4.2 客户端默认使用 HTTP。服务端与 QWeather 之间始终使用 HTTPS，这与客户端用什么协议无关。

如果汉化 APK 已经改成 HTTPS，优先使用 HTTPS；如果 APK 仍是 HTTP，必须保留一个可用的 HTTP 入口，并确认云厂商安全组放行 80 端口。

Nginx 上启用 HTTPS：

```bash
sudo apt install -y certbot python3-certbot-nginx
sudo certbot --nginx -d miku.example.com
sudo certbot renew --dry-run
curl -i https://miku.example.com/healthz
```

旧设备的 TLS 栈很老，可能因为协议版本或证书链无法连接 HTTPS。这种情况下不要为了迁就它去降低服务端与 QWeather 之间的安全要求，而应保留一个只暴露必要兼容接口的 HTTP 域名，并把 `/admin` 限制成仅内网或指定 IP 可访问。

## 11. 防火墙与端口

UFW（Debian）：

```bash
sudo apt install -y ufw
sudo ufw allow OpenSSH
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo ufw enable
sudo ufw status verbose
```

iptables（Alpine）：

```sh
apk add iptables ip6tables
iptables -A INPUT -p tcp --dport 22 -j ACCEPT
iptables -A INPUT -p tcp --dport 80 -j ACCEPT
iptables -A INPUT -p tcp --dport 443 -j ACCEPT
rc-update add iptables default
/etc/init.d/iptables save
```

不要开放 `8080/tcp`：Python 服务只监听 `127.0.0.1`，公网访问一律经过反向代理。云服务器还要在控制台安全组放行 TCP 22、80、443。

## 12. 修改客户端 APK，指向本服务端
四个 APP 把服务地址写死在代码里，指向已经停服的 `mikuxperia.com`。要连上你自己的
服务端，只需把地址换成你的域名再签名安装。`apk/` 里的四个文件就是设备预装的原版，
本目录随附「一键脚本」和「GitHub Action」两种方式，改机无需懂 smali。

### 12.1 一键脚本（推荐）

前提：本机装有 JDK 8+（命令行能运行 `java`），`apktool_2.9.3.jar` 与
`uber-apk-signer-1.3.0.jar` 已在本目录。设备上建议已刷 Xperia 联动包（见第 13 节），
否则按下方说明加 `-NoSharedLib`。

Windows（PowerShell）：

```powershell
# 局域网测试（默认就是这个地址）
.\patch-apk.ps1

# 公网域名
.\patch-apk.ps1 -BaseUrl http://miku.example.com

# 未刷联动包时：自动去掉 <uses-library>，避免安装报缺少共享库
.\patch-apk.ps1 -BaseUrl http://miku.example.com -NoSharedLib
```

Linux / macOS：

```bash
bash patch-apk.sh                                # 局域网测试
bash patch-apk.sh http://miku.example.com        # 公网域名
bash patch-apk.sh http://miku.example.com noslib # 未刷联动包时加 noslib
```

脚本会依次：解包四个 APK → 把旧域名整体替换成你填的地址（只动主机名，路径不变）
→ 重打包 → zipalign 并签名。等它跑完，本目录 `patched/` 里就是四个已签名的成品：

```text
patched/com.mikuxperia.featuresongsplayerapp.apk
patched/com.mikuxperia.mikunewsapp.apk
patched/com.mikuxperia.mikuweatherwidget.apk
patched/MikuDownloader.apk
```

直接安装即可；设备上已有同名旧版时先卸载：

```bash
adb uninstall com.mikuxperia.featuresongsplayerapp
adb install  patched/com.mikuxperia.featuresongsplayerapp.apk
adb uninstall com.mikuxperia.mikunewsapp
adb install  patched/com.mikuxperia.mikunewsapp.apk
adb uninstall com.mikuxperia.mikuweatherwidget
adb install  patched/com.mikuxperia.mikuweatherwidget.apk
adb uninstall com.mikuxperia.mikudownloader
adb install  patched/MikuDownloader.apk
```

`-NoSharedLib` / `noslib` 的含义：四个 APP 在 Manifest 里声明了
`com.mikuxperia.mikuxperia_library` 共享库依赖，未刷联动包的设备没有这个库，
安装会报 `INSTALL_FAILED_MISSING_SHARED_LIBRARY`。加这个开关会删掉 Manifest 里那行
声明（smali 检查确认应用运行时并不使用该库，删掉不影响功能）。

### 12.2 GitHub Action 方式（可选）

仓库根目录的 `.github/workflows/patch-apk.yml` 已配好，想不动本地环境也可以：

1. 把仓库推到 GitHub（`apk/` 与 `patch-apk.sh` 必须在库里；两个 jar 缺了也会由工作流自动下载）；
2. 打开仓库的 **Actions** → **改机 APK** → **Run workflow**；
3. 在 **Base URL** 里填服务端地址，勾不勾「去掉共享库依赖」看设备是否刷过联动包；
4. 运行结束后在本次运行页的 **Artifacts** 里下载 `patched-apks`。

### 12.3 被替换的是哪些地址

脚本把三个旧主机名全局替换成你的地址。手动核对时关注下面这些点即可：

| 应用 | 旧主机（替换为你的地址） | 相关接口 |
| --- | --- | --- |
| 歌曲 | `http://auth.mikuxperia.com/feature_songs_provider/addresses` | 播放地址 |
| 歌曲 | `http://distribute.mikuxperia.com/resources/xml/FeatureSongsPlayer/playlist.xml` | 歌单 |
| 新闻 | `http://distribute.mikuxperia.com/resources/xml/MikuNews/list.xml` | 新闻列表 |
| 新闻 | `http://distribute.mikuxperia.com/resources/images/MikuNews/` | 缩略图前缀 |
| 天气 | `http://evawdt.otenki.co.jp/getdata.php` | 天气数据 |
| 下载器 | `http://distribute.mikuxperia.com/resources/xml/MikuDownloader/applist.xml` | 应用列表 |
| 下载器 | `http://distribute.mikuxperia.com/resources/xml/MikuDownloader/noticelist.xml` | 通知列表 |

补充说明：

- 只替换「协议 + 主机[:端口]」，路径保持原样；服务端路由不依赖主机名。
- 新闻 APP 会把 `<thumbnail>debug.png</thumbnail>` 拼到上面的图片前缀后面去取图，
  服务端会自动生成这张占位图，无需再改。
- 下载器里的 `http://dx39.net` 是作者站点的外链，与数据无关，脚本刻意保留。
- 局域网测试填 `http://192.168.31.104:8080`，公网填 `http://miku.example.com`。

### 12.4 手动改（脚本不可用时的替代做法）

1. `java -jar apktool_2.9.3.jar d -f -o 解包目录 apk/对应APK`
2. 在解包目录里把三个旧主机名全局替换成你的地址；
3. 需要去共享库依赖就编辑 `AndroidManifest.xml` 删除
   `<uses-library android:name="com.mikuxperia.mikuxperia_library"/>` 这一行；
4. `java -jar apktool_2.9.3.jar b 解包目录 -o 输出.apk`
5. `java -jar uber-apk-signer-1.3.0.jar --apks 输出.apk --allowResign`。

### 12.5 别改动的东西

- 天气的 `APID` 值 `SO-04E_OTENKIMIKU`：服务端靠它校验请求（见第 20.1 节）。
- 歌曲 APP 的表单字段名 `feature_songs_provider[{N}][playlist_id]`、
  `[musicFileName]` 等：服务端按此格式解析。
- 包名与 Activity / Service 类名：下载器返回的 `packageName` / `className`
  与它们一一对应。

### 12.6 验证与常见问题

验证：安装后打开四个 APP，能出列表、能放歌、天气小组件能刷新；再回服务端管理面板
看「今日访问量」中 APP 接口请求是否增长。始终为 0 说明客户端还在访问旧地址，或
设备解析不了你的域名。

| 现象 | 处理 |
| --- | --- |
| 安装报 `INSTALL_FAILED_MISSING_SHARED_LIBRARY` | 刷第 13 节联动包，或加 `-NoSharedLib` 重新生成 |
| 安装报 `INSTALL_FAILED_UPDATE_INCOMPATIBLE` | 签名不同，先卸载旧包 |
| 安装报 `INSTALL_PARSE_FAILED_NO_CERTIFICATES` | 漏了签名步骤 |
| 列表能加载但点歌没声音 | 看服务端 `music.json` 与 APP 请求统计 |
| 天气一直不更新 | 确认 `APID` 未被改动，`/getdata.php` 可访问 |

## 13. Xperia 联动包（TWRP 刷入）

本目录下的 `Xperia_feat_original_FE_V5a.zip`（约 387 MB）是 Xperia feat. 整合联动包
（V5a[FE]，作者 Tad-Liou；音频来自 YCx，壁纸来自 aurajp，应用为 R3 Crack 版），
目标设备为 Xperia（SO-04E / Android 4.2），通过 TWRP 刷入。它把 Miku 应用、共享库、
角色壁纸、铃声、通知音与输入法皮肤装进设备。联动包与服务端没有网络通信，但它
刷入的共享库是第 12 节四个 APP 的安装前提。

### 13.1 包内结构

| zip 内路径 | 内容 | 体积 |
| --- | --- | --- |
| `system/lib/` 与 `system/etc/permissions/` | 共享库 `com.mikuxperia.mikuxperia_library.jar`（344 字节空壳）与其声明文件、语音引擎 `libpatts_engine_jni_api.so` | 约 2.1 MB |
| `system/app/` | 五个角色输入法皮肤：`01_Miku_Keyboard` … `05_Meiko_Keyboard` | 约 5.2 MB |
| `system/etc/customization/…/wallpapers/` | 角色壁纸与高清壁纸约 18 组 | 约 16 MB |
| `system/media/audio/` | 铃声 22 个、通知 17 个、闹钟 17 个（共 56 个 ogg） | 约 35.4 MB |
| `data/app/` | 11 个 APK（含歌曲、新闻、天气、下载器、MikuHome、LiveWallpaper、Alarm、FindYourMiku content、电池/时钟小组件等） | 约 93 MB |
| `sdcard/Android/obb/com.mikuxperia.findyourmikucontent/` | FindYourMiku 数据包（obb） | 约 217 MB |
| `META-INF/com/google/android/` | TWRP Edify 脚本 `updater-script` 与 `update-binary` | 0.2 MB |

其中 `data/app/` 里的歌曲、新闻、天气、下载器四个文件与本目录 `apk/` 完全一致，
即第 12 节「要改地址的对象」的原始版本。整包解压后约 369 MB，仅 `/system` 部分就
需要约 59 MB 可用空间。

### 13.2 刷入脚本做了什么

`updater-script` 依次执行：挂载 `/system` 与 `/data` → 把 `system/` 目录整树解压
到 `/system`、`data/` 解压到 `/data` → 用 `set_perm` 修正共享库、五个输入法、音频
文件的权限 → 挂载 `/sdcard` 并把 obb 解压到位 → 提示 5 秒后自动重启。

需要注意：

- **脚本里没有设备型号断言**，刷错机型不会拦截，只应在 Xperia SO-04E 这类目标机上刷。
- 会覆盖 `/system/media/audio` 下同名铃声/通知/闹钟，以及
  `wallpaperpicker` 的壁纸文件；`/system` 上已有的其他定制也会被覆盖。
- `/data/app` 里的 11 个 APK 要到首次开机才由 PackageManager 逐个安装，开机明显
  变慢属正常；安装后它们就是普通应用，可在系统设置里卸载。
- 五个输入法位于 `system/app`，随系统存在，卸载需要先解除其为默认输入法。
- 原包带有签名；若自己改动 zip 内容再刷，需在 TWRP 里关闭签名校验或重新签名。

### 13.3 与服务端配合使用的顺序

1. （强烈建议）进 TWRP 先完整备份 System 与 Data 分区。
2. TWRP → Install → 选择 `Xperia_feat_original_FE_V5a.zip` 刷入并重启。
3. 按第 12 节重打包四个应用，`adb` 卸载旧包后安装新包。
4. 配置并启动本服务端，验证四个应用能连上。

### 13.4 回退方法

在 TWRP 里恢复备份即可。手动回退则需要：删除 `/system` 下新增的文件与恢复被覆盖
的音频/壁纸（先把 `/system` 挂载为可写）、删除 `/data/app` 里对应的 APK、删除
`/sdcard/Android/obb/com.mikuxperia.findyourmikucontent/` 目录。

### 13.5 版权提示

联动包内的壁纸、音频与素材版权归原作者（Tad-Liou）、音频作者（YCx）、壁纸作者
（aurajp）及其各自权利方所有。仅供个人在自有设备上使用，请勿重新分发。

## 14. 管理面板 WebUI

面板地址是 `/admin`，Material Design 2 风格，主题色 `#39C5BB`，界面全中文。默认关闭，必须显式启用并设置密码。

### 14.1 启用面板

在 `miku.conf` 中加入：

```ini
[webui]
enabled = true
password = 换成你自己的强密码
session_hours = 12
stats_file = webui-stats.json
```

重启服务后访问 `http://miku.example.com/admin`，输入密码即可进入。

```bash
sudo systemctl restart mikuxperia.service   # Debian
rc-service mikuxperia restart               # Alpine
```

`enabled` 为 false 或 `password` 为空时，`/admin` 全部返回 404 文本提示，不会泄露任何运行信息。

### 14.2 面板区块

| 区块 | 内容 |
| --- | --- |
| 今日访问量 | 今日请求数、独立访客、APP 接口请求、媒体文件请求、累计请求 |
| 健康状态 | 运行时长、总体状态，以及播放列表配置、媒体目录、天气凭据、天气缓存、AI 新闻五项检查 |
| 服务信息 | 监听地址、公开地址、天气位置、播放列表与歌曲数量、媒体目录 |
| 定时任务 | 四个任务的状态、间隔、下次执行时间，可立即执行、改间隔、开关自动执行 |
| 上传歌曲 | 上传音频、封面与歌词，自动写入 `music.json` |
| Miku 新闻与新歌动态 | AI 汇总结果、数据源、抓取时间、最近错误 |
| Bilibili 收藏夹缓存 | Cookie 状态与格式、SESSDATA 检测、各收藏夹的歌曲数、音频数、封面数与磁盘占用 |
| 最近 14 天访问统计 | 按天列出请求数、访客数、APP 与媒体请求数以及趋势条 |

顶栏有四个按钮：更新天气、刷新新闻、同步收藏夹、退出。前三个等价于立即执行对应任务。

### 14.3 会话与密码

- 密码以明文保存在 `miku.conf`，比对时使用 `hmac.compare_digest`，避免时序差异。
- 登录成功后下发 Cookie `miku_admin`，属性为 `Path=/admin; HttpOnly; SameSite=Strict`，有效期等于 `session_hours`。
- 会话只保存在内存里，服务重启后所有人都需要重新登录。
- 面板没有验证码，也没有登录失败次数限制，建议在反向代理层再加一道来源限制。

### 14.4 访问统计

- 数据写入 `stats_file`（默认 `webui-stats.json`），保留最近 120 天，面板展示最近 14 天。
- 独立访客按「当天日期 + 客户端 IP」做 SHA-256 后取前 16 位计数，磁盘上不保存原始 IP。
- `/admin` 与 `/healthz` 的请求完全不计入统计，自己刷新面板不会污染数据。
- 请求分类：`/media/` 前缀计为媒体请求；`/resources/`、`/getdata`、`/feature_songs_provider` 前缀计为 APP 接口请求；其余只计入总数。
- 为减少磁盘写入，统计每 25 次请求或出现新访客时才落盘，进程被强杀可能丢失最后几条记录。

确保统计文件可写：

```bash
sudo -u mikuxperia touch /opt/mikuxperia-server/webui-stats.json
sudo chmod 640 /opt/mikuxperia-server/webui-stats.json
```

### 14.5 JSON 状态接口

`/admin/api/status` 返回面板使用的完整快照，含 `stats`、`health`、`news`、`bilibili`、`tasks`、`playlists` 等字段，适合接入外部监控。需要携带有效的 `miku_admin` Cookie，未登录返回 401 与 `{"error":"unauthorized"}`。

### 14.6 限制面板访问来源

Nginx：

```nginx
location /admin {
    allow 203.0.113.10;
    deny all;
    proxy_pass http://127.0.0.1:8080;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-Proto $scheme;
}
```

Caddy：

```caddyfile
miku.example.com {
	@admin path /admin*
	@notmine not remote_ip 203.0.113.10
	respond @admin 403 {
		close
	}

	reverse_proxy 127.0.0.1:8080
}
```

更严格的做法是让 `/admin` 只在内网或 VPN 内可达，公网只暴露 APP 需要的接口。

## 15. 定时任务

调度器随服务启动，每 5 秒检查一次是否有任务到期，因此实际触发时间可能比设定值晚几秒。任务在独立线程中执行，异常会被捕获并记录，不会影响服务或其他任务。同一个任务不会并发执行，重复点击「立即执行」会提示「正在执行中」。

### 15.1 任务清单

| 任务名 | 面板标签 | 作用 | 默认间隔 | 最小间隔 | 默认开关 |
| --- | --- | --- | --- | --- | --- |
| `weather` | 天气更新 | 清空缓存并重新请求和风天气 | `[qweather] cache_seconds`（默认 30 分钟） | 5 分钟 | 开启 |
| `news` | AI 新闻更新 | Tavily 搜索并用 AI 汇总 Miku 动态 | `[ainews] refresh_seconds`（默认 1 小时） | 5 分钟 | 开启 |
| `bilibili` | Bilibili 收藏夹同步 | 以子进程运行同步脚本，下载音频与封面并更新播放列表 | 6 小时 | 10 分钟 | 关闭 |
| `cache_cleanup` | 缓存清理 | 删除媒体目录下未被 `music.json` 引用的文件 | 7 天 | 24 小时 | 开启 |

间隔上限统一是 30 天。设置低于最小间隔的值会被拒绝，避免频繁调用付费接口。

### 15.2 缓存清理任务的删除范围

这是唯一会删除文件的任务，务必先确认行为再让它自动运行：

- 它会递归遍历 `media_root` 下的**所有文件**，凡是没有出现在 `music.json` 的 `image`、`brief_image`、`music`、`thumbnail`、`lyrics` 字段中的，一律删除。
- 因此媒体目录里的说明文档、备份文件、还没登记进 `music.json` 的音频、Bilibili 同步生成但已被新一轮同步淘汰的旧文件，都会被清掉。
- 同步脚本生成的播放列表封面（`*.list.jpg`、`*.brief.jpg`）会被 `music.json` 引用，因此不会被误删。
- 空目录不会被删除，只删文件。
- 执行结果形如「清理完成：删除 12 个文件，释放 34.56 MiB」。

如果媒体目录里还放了其他用途的文件，请在 `miku.conf` 里关闭它：

```ini
[tasks]
cache_cleanup_enabled = false
```

也可以在面板里点「关闭自动」，效果会写入状态文件并在重启后保持。

### 15.3 默认值与状态文件

`[tasks]` 提供的是初始值：

```ini
[tasks]
state_file = tasks-state.json
weather_enabled = true
weather_interval_seconds = 1800
news_enabled = true
news_interval_seconds = 3600
bilibili_enabled = false
bilibili_interval_seconds = 21600
bilibili_timeout_seconds = 3600
cache_cleanup_enabled = true
cache_cleanup_interval_seconds = 604800
```

在面板里修改间隔或开关后，结果会写入 `state_file`，**并且优先于 `miku.conf` 里的初始值**。状态文件同时保存上次执行时间、成功与否和结果文本，因此重启后不会立刻重跑刚执行过的任务。

想恢复配置文件里的默认值，删除 `tasks-state.json` 后重启服务即可。

`bilibili_timeout_seconds` 是同步子进程的超时上限。收藏夹很大时首次同步可能很久，超时后任务会被中止，并在面板显示「同步超过 N 秒未完成，已中止」。

### 15.4 立即执行与结果查看

顶栏的三个按钮，以及每个任务的「立即执行」，都在后台线程中运行，页面立即返回。刷新页面就能看到上次执行时间、耗时、累计次数、成功或失败，以及具体的返回信息或错误原因。

Bilibili 同步是通过子进程调用 `sync_bilibili_favorites.py` 完成的，因此面板里的执行效果与手动运行脚本完全一致，脚本输出也会进入服务日志。

## 16. 上传歌曲

面板的「上传歌曲」表单会把文件写入媒体目录的 `uploads` 子目录，并追加到 `music.json`。

### 16.1 限制与校验

| 项目 | 规则 |
| --- | --- |
| 音频 | 必填，允许 `.mp3`、`.m4a`、`.aac`、`.ogg`、`.wav` |
| 封面 | 可选，允许 `.jpg`、`.jpeg`、`.png`、`.webp` |
| 歌词 | 可选，允许 `.txt`、`.lrc`，且必须能按 UTF-8 解码 |
| 标题 | 必填，超过 160 字符会被截断 |
| 作者 | 可选，留空记为「未知作者」，超过 80 字符截断 |
| 日期 | 必须是 `YYYY.MM.DD`，留空则用当天 |
| 链接 | 必须以 `http://` 或 `https://` 开头 |
| 总大小 | 单次上传全部文件合计不超过 80 MB |

超过 80 MB 会返回 HTTP 413。经过反向代理时还要注意代理自身的请求体上限：Nginx 需要 `client_max_body_size`（本文档建议 100m），Caddy 默认不限制。

Android 4.2 设备对 MP3 的兼容性最好，其他格式可能无法播放，建议上传前转成 MP3。

### 16.2 目标播放列表

- 下拉框里可以选择任意手工维护的播放列表。
- 留空时会自动创建并使用 ID 为 `900000001` 的「手动上传」列表。
- 由 Bilibili 同步生成的播放列表不会出现在下拉框中，也不允许作为上传目标，因为下一次同步会整体覆盖它们。

### 16.3 落盘行为

- 文件名会被转换成安全的 ASCII 名称（非 ASCII 字符会被丢弃，全部丢失时退化成 `song-<时间戳>`），重名时自动追加 `-2`、`-3` 序号。中文标题不影响文件名，`music.json` 里仍保存原始标题。
- 封面与歌词会跟随音频使用同一个文件名主干，例如 `my-song.mp3`、`my-song.jpg`、`my-song.txt`。
- 歌曲 `id` 取文件名主干，与列表内已有 ID 冲突时追加时间戳。
- 未提供封面时，`thumbnail` 回落到所在播放列表的 `image`。
- 如果目标播放列表原本没有封面，本次上传的封面会同时成为列表的 `image` 与 `brief_image`。
- 上传成功后播放列表的 `version` 自动加一，旧客户端因此会重新拉取列表，不需要手动改版本号或重启服务。
- 写入 `music.json` 失败时，已经落盘的文件会被回滚删除，不会留下孤立文件。

### 16.4 权限要求

上传要求 `music.json` 与 `media/` 对运行用户可写：

```bash
sudo chown mikuxperia:mikuxperia /opt/mikuxperia-server/music.json
sudo chmod 660 /opt/mikuxperia-server/music.json
sudo chmod 750 /opt/mikuxperia-server/media
```

## 17. 手动维护歌曲与专辑

### 17.1 添加歌曲

1. 把音频、封面和 UTF-8 歌词文件放进 `media/`（可以放在子目录里，路径用正斜杠）。
2. 在 `music.json` 对应播放列表的 `songs` 数组中添加歌曲对象。
3. 把该播放列表的 `version` 加一。只改动了哪个列表就只增哪个列表的版本号。
4. 运行自检，然后重启服务。

```json
{
  "id": "song-001",
  "title": "歌曲标题",
  "artist": "歌手",
  "date": "2026.08.31",
  "music": "song-001.mp3",
  "thumbnail": "song-001.jpg",
  "lyrics": "song-001.txt",
  "link": ""
}
```

```bash
cd /opt/mikuxperia-server
sudo -u mikuxperia python3 server.py --check
sudo systemctl restart mikuxperia.service
```

注意两点：

- 只把 MP3 复制进 `media/` 不会自动发布，必须同时在 `songs` 数组里登记；而且未被登记的文件会被[缓存清理任务](#152-缓存清理任务的删除范围)删除。
- 音频文件必须存在，歌词可以是空字符串。

### 17.2 新建专辑

向顶层 `playlists` 数组追加对象：

```json
{
  "id": 2,
  "version": 1,
  "title": "第二张专辑",
  "description": "完整专辑介绍",
  "brief_description": "简短介绍",
  "image": "album-2.jpg",
  "brief_image": "album-2-thumb.jpg",
  "songs": []
}
```

`id` 必须是唯一的正整数且不超过 `2147483647`。已经被客户端用过的 ID 不要改变含义，否则客户端本地数据库里的旧数据会与新内容混在一起。封面建议提前压到较小尺寸，原因见[封面为什么会被强制缩小](#封面为什么会被强制缩小)。

## 18. 同步 Bilibili 收藏夹

`sync_bilibili_favorites.py` 可以把 Bilibili 收藏夹同步成播放列表：一个收藏夹对应一个或多个播放列表，只下载音频流和封面，不下载视频画面。开启 `combined_enabled` 后，还会在保留各收藏夹列表的同时，额外生成一个包含全部成功下载歌曲的「合集」播放列表（跨收藏夹自动去重），详见[第 18.6 节](#186-同步做了什么)。

请只处理你拥有版权、获得授权或明确允许下载和再分发的内容，不要把这个功能做成任何人都能提交链接的公开下载代理。

### 18.1 安装依赖

Debian 11：

```bash
sudo apt install -y python3-pip ffmpeg
sudo python3 -m pip install --upgrade yt-dlp
yt-dlp --version
ffmpeg -version
```

两个依赖都是必需的：`yt-dlp` 负责解析与下载，`ffmpeg` 负责把音频转成 MP3 并缩放封面。缺少 `ffmpeg` 时下载会直接失败。

### 18.2 导出 Bilibili Cookie

收藏夹接口需要登录态。用你自己的账号导出 Cookie，保存为 `/opt/mikuxperia-server/bilibili.cookies.txt`。脚本支持两种格式，任选一种。

格式一，请求头格式（最简单）。在浏览器登录 bilibili.com，按 F12 打开开发者工具，在 Network 里任选一个请求，复制 Request Headers 中 `Cookie` 的值，粘贴成一行：

```text
SESSDATA=xxxxx; bili_jct=yyyyy; DedeUserID=12345; buvid3=zzzzz
```

只有 `SESSDATA` 是必需的，下面这样也能工作：

```text
SESSDATA=xxxxx
```

格式二，Netscape 格式，由「Get cookies.txt」这类扩展导出，每行 7 个制表符分隔字段：

```text
# Netscape HTTP Cookie File
.bilibili.com	TRUE	/	TRUE	1800000000	SESSDATA	xxxxx
```

脚本会自动识别格式。使用格式一时，脚本在调用 `yt-dlp` 前会生成一个权限为 600 的临时 Netscape 文件，用完立即删除，因为 `yt-dlp` 只接受 Netscape 格式。

设置权限，并且不要把这个文件提交到 Git 或发给别人：

```bash
sudo chown mikuxperia:mikuxperia /opt/mikuxperia-server/bilibili.cookies.txt
sudo chmod 600 /opt/mikuxperia-server/bilibili.cookies.txt
```

检查 Cookie：

```bash
cd /opt/mikuxperia-server
sudo -u mikuxperia python3 sync_bilibili_favorites.py --check-cookie
```

```text
cookie: 4 个字段，格式 header，字段 DedeUserID, SESSDATA, bili_jct, buvid3
登录状态正常: uname=你的昵称 mid=12345678
```

缺少 `bili_jct`、`DedeUserID`、`buvid3` 只会给出提示，不会中止；缺少 `SESSDATA` 会直接报错。

### 18.3 配置收藏夹

收藏夹页面地址形如：

```text
https://space.bilibili.com/你的UID/favlist?fid=123456789
```

`fid` 后面的数字就是收藏夹 ID。把 `[bilibili]` 小节追加到 `miku.conf`：

```ini
[bilibili]
folder_ids =
    123456789
    987654321
cookie_file = bilibili.cookies.txt
media_root = media/bilibili
catalog = music.json
page_size = 20
max_videos_per_folder = 0
songs_per_playlist = 50
redownload_existing = false
# 需要「全部收藏夹合成一个合集」时再打开下面两行：
# combined_enabled = true
# combined_title = Bilibili 收藏夹合集
```

首次测试建议把 `max_videos_per_folder` 改成 `3`，确认流程无误后再改回 `0`。各选项含义见[第 3.2 节](#32-配置项速查表)。

### 18.4 检查写入权限

同步脚本和服务都以 `mikuxperia` 用户运行，缓存目录必须由该用户拥有，否则 `yt-dlp` 会报 `Permission denied`：

```bash
sudo mkdir -p /opt/mikuxperia-server/media/bilibili
sudo chown -R mikuxperia:mikuxperia /opt/mikuxperia-server/media
sudo chmod -R u+rwX /opt/mikuxperia-server/media
sudo chown mikuxperia:mikuxperia /opt/mikuxperia-server/music.json
sudo chmod 660 /opt/mikuxperia-server/music.json

cd /opt/mikuxperia-server
sudo -u mikuxperia python3 sync_bilibili_favorites.py --check-paths
```

```text
运行用户: mikuxperia:mikuxperia
播放列表可写: /opt/mikuxperia-server/music.json
媒体目录可写: /opt/mikuxperia-server/media/bilibili（mikuxperia:mikuxperia mode=0o755）
收藏夹目录可写: /opt/mikuxperia-server/media/bilibili/123456789（mikuxperia:mikuxperia mode=0o755）
```

不可写时，输出里会直接给出需要执行的 `chown` 与 `chmod` 命令。

### 18.5 试运行与正式同步

先只读取收藏夹，不下载也不改动 `music.json`：

```bash
cd /opt/mikuxperia-server
sudo -u mikuxperia python3 sync_bilibili_favorites.py --dry-run
```

确认无误后正式同步：

```bash
sudo -u mikuxperia python3 sync_bilibili_favorites.py
sudo -u mikuxperia python3 server.py --check
sudo systemctl restart mikuxperia.service
```

典型输出：

```text
cookie: 4 个字段，格式 header，字段 DedeUserID, SESSDATA, bili_jct, buvid3
folder 3275482587: 110 video(s)，跳过 2 个失效条目
  [37/110] 跳过 BV1xxxxxxxxx：视频已失效或被删除（ERROR: ...）
folder 3275482587: 生成 3 个播放列表，成功 108 首，下载失败 1 个
updated /opt/mikuxperia-server/music.json: 4 playlist(s), 108 song(s)
skipped 1 video(s):
  - BV1xxxxxxxxx 某个标题：视频已失效或被删除
```

其中「跳过 N 个失效条目」是读取收藏夹时按接口字段过滤掉的条目（已失效、已删除、非视频资源），「下载失败」是下载阶段被跳过的视频。被跳过的视频最多列出 20 条，其余只报数量。

开启 `combined_enabled` 后，输出里会多一行合集汇总，例如：

```text
combined: 生成 3 个合集播放列表，共 132 首（去重 1 首重复视频）
```

### 18.6 同步做了什么

1. 调用收藏夹信息接口取收藏夹名称，作为播放列表标题；取不到时退回「Bilibili 收藏夹 <ID>」。
2. 分页读取收藏夹条目，过滤掉失效稿件、私密稿件和非视频资源。
3. 用 `yt-dlp` 取最佳音频流并转成 128 kbps MP3（`-x --audio-format mp3`），非 MP3 的结果再用 `ffmpeg`（libmp3lame、128 kbps、44.1 kHz、立体声）转一遍。已存在音频文件时默认跳过下载，`redownload_existing = true` 才会重下。
4. 下载封面存为 `<BV号>.jpg`，并缩放到宽 170 像素；已有封面会尝试再压小。
5. 按 `songs_per_playlist` 把歌曲切成多个播放列表，多片时标题追加 `(1/3)` 这样的后缀。
6. 为每个播放列表派生两张更小的封面：`<BV号>.list.jpg`（宽 240）用于详情页，`<BV号>.brief.jpg`（宽 80）用于列表页。
7. 若 `combined_enabled = true`：把所有收藏夹成功下载的歌曲收集起来，跨文件夹相同的视频（按 BV 号）只保留首次出现的条目，再按 `songs_per_playlist` 拆成「合集」播放列表，追加在本轮列表末尾。合集只引用各收藏夹已下载的缓存音频与封面，不会重复下载。
8. 写回 `music.json`：保留所有不带 `source = bilibili-favorites` 的播放列表，然后追加本轮生成的全部列表（含合集，如有）。

同步后的目录结构：

```text
/opt/mikuxperia-server/media/bilibili/123456789/
├── BV1xxxxxxxx.mp3
├── BV1xxxxxxxx.jpg
├── BV1xxxxxxxx.list.jpg
└── BV1xxxxxxxx.brief.jpg
```

生成的播放列表带这些标记字段：

```text
source = bilibili-favorites
source_folder_id = 123456789
source_part = 0
source_parts = 3
```

播放列表 ID 由收藏夹 ID 折算而来：`1000000000 + 收藏夹ID % 1000000000 + 分片序号`。例如收藏夹 `3275482587` 的第一个分片是 `1275482587`。这样做是因为客户端用 `Integer.parseInt` 解析播放列表 ID，而收藏夹 ID 本身就超过了 32 位上限。同一个收藏夹每次同步都会得到相同的 ID，客户端不会把它当成新列表。

合集播放列表使用独立的标记与 ID：

```text
source = bilibili-favorites
source_combined = true
source_parts = 3
```

合集 ID 取 `2000000000 + 分片序号`（第一片是 `2000000000`）。它落在收藏夹折算区间 `[1000000000, 2000000000)` 之外，既不会与任何收藏夹分片冲突，也不会与手工列表或网页上传的默认列表冲突，且同样不超过 32 位上限。合集标题多片时形如 `Bilibili 收藏夹合集 (1/3)`。与分文件夹列表一样，合集每次同步整体重建并保留手工列表，因此收藏夹数量或内容变化后不会残留旧的合集分片。

每个视频用 BV 号作为歌曲 `id`，重复同步不会在列表里出现重复条目。播放列表 `version` 取「2026-01-01 起的分钟数」，因此每次同步都会推进，客户端会重新拉取。

#### 封面为什么会被强制缩小

客户端把播放列表封面以 BLOB 形式存进 `tbl_play_music_info` 的一行，并通过 2 MiB 的 `CursorWindow` 读取。两张 1920×1080 的原图就会溢出，导致 `MusicListActivity` 抛出 `Couldn't read row 0, col 0 from CursorWindow` 崩溃。所以脚本把歌曲封面压到宽 170、详情页封面压到宽 240、列表页封面压到宽 80。手工维护专辑时也请遵守同样的量级。

### 18.7 定期同步

面板里的 `bilibili` 任务已经可以定时执行，把它打开即可。若希望完全脱离面板，也可以用 systemd 定时器。

```bash
sudo nano /etc/systemd/system/mikuxperia-bilibili-sync.service
```

```ini
[Unit]
Description=Sync authorized Bilibili favorite folders
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User=mikuxperia
Group=mikuxperia
WorkingDirectory=/opt/mikuxperia-server
ExecStart=/usr/bin/python3 /opt/mikuxperia-server/sync_bilibili_favorites.py --config /opt/mikuxperia-server/miku.conf
```

```bash
sudo nano /etc/systemd/system/mikuxperia-bilibili-sync.timer
```

```ini
[Unit]
Description=Run Bilibili favorites sync every six hours

[Timer]
OnBootSec=10min
OnUnitActiveSec=6h
Persistent=true

[Install]
WantedBy=timers.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now mikuxperia-bilibili-sync.timer
sudo systemctl list-timers mikuxperia-bilibili-sync.timer
sudo journalctl -u mikuxperia-bilibili-sync.service -n 100 --no-pager
```

不要同时开启面板任务和系统定时器，否则可能在同一时间跑两份同步。Alpine 上的 crond 写法见[第 7.6 节](#76-alpine-上的定时同步)。

### 18.8 控制磁盘占用

同步会保存 MP3 和封面，128 kbps 大约 1 MB/分钟，收藏夹条目多时占用相当可观。

```bash
du -sh /opt/mikuxperia-server/media/bilibili
df -h /opt/mikuxperia-server
```

建议：

- 首次使用先设 `max_videos_per_folder = 3` 到 `10`，分几次跑完。
- 把 `max_videos_per_folder` 设成合理上限，避免一次同步填满磁盘。
- 至少保留 20% 磁盘空间给系统、日志和临时文件。
- 同步脚本本身不会删除旧媒体文件；从收藏夹里移除的歌曲会离开 `music.json`，之后由[缓存清理任务](#152-缓存清理任务的删除范围)回收，或者自己手工删除。

### 18.9 单个视频诊断

```bash
cd /opt/mikuxperia-server
sudo -u mikuxperia python3 sync_bilibili_favorites.py --probe BV1eT41137WH
```

这会列出该视频的所有可用格式。按结果判断：

- 能列出 `audio only` 格式：偶发网络或风控问题，重跑即可。
- 只有 FLV 之类的合并流：视频太老，B 站没有提供 DASH 音频流，需要靠 `ffmpeg` 从合并流提取音频。
- 报 `HTTP Error 412`、`-352` 或提示风控：请求太密集被限流，等几分钟再试并降低同步频率。
- 报 `HTTP Error 403`：Cookie 失效，重新导出。
- 报 `KeyError('bvid')` 或稿件不可见：视频已失效，脚本会自动跳过。

关于风控，脚本已经内置 `--sleep-requests 1`、`--retries 3`、`--extractor-retries 2` 和 30 秒 socket 超时，但一次同步上百个视频仍容易触发限流，建议定时同步间隔不短于 6 小时。

## 19. AI 新闻与新歌动态

新闻由两部分组成：Tavily 负责实时搜索，OpenAI 兼容接口负责整理成中文条目。两者都是可选的：

| 配置情况 | 结果 |
| --- | --- |
| 只配置 Tavily | 直接使用搜索结果的标题与摘要 |
| 同时配置两者 | 由模型汇总、分类并按重要性排序 |
| 都不配置 | `/resources/xml/MikuNews/list.xml` 返回静态占位内容 |

结果同时提供给管理面板和 Miku News APP，APP 中每条新闻的链接指向原始来源。

### 19.1 配置

- Tavily：在 Tavily 控制台创建 API Key。
- 摘要模型：任何 OpenAI 兼容的 `/chat/completions` 接口都可以，包括官方、兼容代理或自建服务。

```ini
[ainews]
enabled = true
tavily_api_key = tvly-你的Key
tavily_endpoint = https://api.tavily.com/search
tavily_max_results = 8
tavily_topic = news
search_queries =
    初音ミク 新曲 最新情報
    初音未来 最新新闻 动态
    Hatsune Miku news new song
openai_base_url = https://api.openai.com/v1
openai_api_key = sk-你的Key
openai_model = gpt-4o-mini
refresh_seconds = 3600
max_items = 8
timeout_seconds = 30
cache_file = ainews-cache.json
```

要点：

- `search_queries` 每行一个，多个查询的结果按链接去重后合并。
- `refresh_seconds` 最小 300 秒，避免频繁调用付费接口。
- `max_items` 上限 30，`tavily_max_results` 上限 20。
- `openai_base_url` 填到 `/v1` 为止，程序自动拼 `/chat/completions`。
- 只想使用搜索结果时，把 `openai_base_url`、`openai_api_key`、`openai_model` 三项留空。
- `cache_file` 保存最近一次成功的结果，服务重启后立即可用，不必等待重新抓取。

### 19.2 验证

```bash
sudo systemctl restart mikuxperia.service
curl -s http://127.0.0.1:8080/resources/xml/MikuNews/list.xml
```

成功时 XML 里的 `notice` 会变成 `Miku 新闻由 AI 汇总。`，每个 `item` 的 `link` 指向原始来源，`category` 取 `music`、`event` 或 `news`，`thumbnail` 固定为 `debug.png`。也可以在面板点「刷新新闻」，然后查看「Miku 新闻与新歌动态」区块里的数据源与抓取时间。

查看日志：

```bash
sudo journalctl -u mikuxperia.service -n 50 --no-pager   # Debian
tail -n 50 /var/log/mikuxperia.log                        # Alpine
```

### 19.3 刷新时机与容错

- 服务启动时不会主动抓取，而是先从 `cache_file` 恢复上次结果。
- 之后有两个刷新入口：`news` 定时任务（默认开启，若距上次执行已超过间隔，启动后几秒内就会触发），以及新闻接口被请求且缓存已过期时的同步刷新。
- 后者会让那一次客户端请求一直等到网络返回，旧设备容易超时，所以建议保持 `news` 任务开启，让刷新总是发生在后台。
- 同一时间只允许一个刷新任务，重复触发会直接返回当前条目数。
- 模型返回内容无法解析时，自动退回原始搜索结果，面板的数据源会显示 `tavily (总结失败)`。
- 模型被要求只能引用搜索结果里的链接，程序还会二次校验：出现不在结果集合里的 URL 时，会替换成第一条搜索结果的链接。
- 搜索全部失败时保留上一次的缓存内容，并在面板显示最近错误信息。

AI 汇总的内容仍可能出现事实错误或过时信息，重要信息请通过面板里的来源链接自行核实。

## 20. 天气接口适配细节

旧版天气小组件用 `POST /getdata.php` 取数据，服务端把和风天气的响应翻译成它能解析的 XML。

### 20.1 请求与缓存

- 客户端表单里的 `APID` 必须是 `SO-04E_OTENKIMIKU`，否则返回 502 与 `invalid legacy APID`。
- 客户端的 `AREA` 只会被原样回显到 `<id>` 里，实际位置由 `[weather]` 的经纬度决定。
- 服务端请求 `https://<api_host>/weather/v1/daily/<纬度>/<经度>?days=8&localTime=true&lang=zh`，超时 10 秒，自动处理 gzip 与 deflate 响应。
- 返回的 `days` 少于 2 天视为异常，直接报错。
- 预报按「保留两位小数的经纬度」缓存 `cache_seconds` 秒；面板的「更新天气」会清空缓存并立即重新请求。

### 20.2 字段映射

响应包含 8 组 `weatherdata`：`today`、`tomorrow`、`weekly1` 到 `weekly6`。前两组带完整字段，后六组只有日期、天气、气温和降水概率。

| 旧字段 | 来源 |
| --- | --- |
| `day` | 预报日期，格式 `YYYYMMDD` |
| `weather` | 由和风天气代码归并成 `100` 晴、`101` 多云、`200` 阴或雾霾、`300` 雨、`400` 雪 |
| `temphi`、`templo` | 当日最高、最低气温，四舍五入取整 |
| `proba01` 到 `proba04` | 白天降水概率，自动识别 0–1 与 0–100 两种量纲 |
| `wind` | 风向角度除以 23 得到的旧版风向编号 |
| `velocity` | 风速 |
| `uvr` | 紫外线指数除以 3 后向上取整，限制在 1–5 |
| `wash`、`star`、`dry` | 洗车、观星、干燥指数，由降水概率与天气代码推导 |
| `point` | `城市（和风天气）`，城市名来自 `[weather] city` |
| `update` | 由首日 `forecastStartTime` 生成的 `YYYY年M月D日H時発表` |

天气代码归并规则：`100`/`150` 归 `100`；`101`–`103`、`151`–`153` 归 `101`；`104`/`154` 归 `200`；`300`–`399` 归 `300`；`400`–`499` 归 `400`；`500`–`599` 归 `200`；其余归 `100`。

## 21. Docker 部署（可选）

已有 Docker 环境时可以直接使用仓库里的配置：

```bash
cp compose.example.yml compose.yml
# 先编辑 miku.conf
docker compose up -d --build
curl http://127.0.0.1:8080/healthz
```

镜像基于 `python:3.12-alpine`，以 `nobody` 用户运行，包含全部模块与同步脚本。使用前请注意以下几点。

**必须把 `listen_host` 改成 `0.0.0.0`。** 容器里监听 `127.0.0.1` 时端口映射不通。`compose.example.yml` 已经把端口绑定在 `127.0.0.1:8080`，公网仍然只经过宿主机上的反向代理。

**状态文件要先创建成空文件。** `compose.example.yml` 直接挂载了三个 JSON 文件，若宿主机上不存在，Docker 会创建同名目录，导致服务写入失败：

```bash
cd server
python3 -c "import pathlib;[pathlib.Path(name).write_text('{}\n') for name in ('webui-stats.json','ainews-cache.json','tasks-state.json') if not pathlib.Path(name).exists()]"
```

**挂载的文件必须对容器内的 `nobody` 可写。** `music.json`、`media/` 以及三个状态文件都需要写权限，否则上传歌曲、访问统计和任务状态持久化都会失败。`miku.conf` 挂载为只读没有问题，服务不会写配置。

**镜像里没有 `yt-dlp` 与 `ffmpeg`。** 因此容器内的 Bilibili 同步任务会失败并提示 `yt-dlp is not installed or not in PATH`。需要这个功能就自行扩展镜像，在 `Dockerfile` 的 `COPY` 之前加一行：

```dockerfile
RUN apk add --no-cache ffmpeg yt-dlp
```

Debian 11 上没有 Docker 时，直接按[第 6 节](#6-部署到-debian-11systemd)用 systemd 部署即可，不需要安装 Docker。

## 22. 测试与自检

### 22.1 单元测试

```bash
cd /opt/mikuxperia-server
python3 -m unittest -v test_server.py
```

当前共 84 个用例，覆盖天气响应解码与 XML 转换、配置解析与相对路径、Cookie 两种格式解析、任务调度与状态持久化、上传校验与落盘、媒体子目录与路径越界、Bilibili 条目过滤与失败分类、播放列表版本与 ID 边界、面板会话与统计、AI 新闻摘要与回退、健康检查快照。

前置条件：

- `server.py` 同目录下的 `miku.conf` 必须能解析（数值项不能留空）。
- `music.json` 与 `media/` 必须完整，其中一个用例断言仓库自带的「1 个播放列表、6 首歌」。加入自己的歌曲后这个断言会失败，属于预期现象。
- 测试会读写 `tasks-state.json`（用例结束时会把改动的间隔调回去），不会触碰媒体文件。

### 22.2 配置自检

```bash
python3 server.py --check
```

它会校验配置文件、`music.json` 的结构与所有被引用的媒体文件，成功时输出 `Configuration OK: N playlist(s), M song(s)`。每次手工改动 `music.json` 或媒体文件后都建议先跑一次。

## 23. 故障排查

### 23.1 启动就报错

| 报错 | 原因与处理 |
| --- | --- |
| `ValueError: could not convert string to float: ''` | `latitude`、`longitude` 之类的数值项留空了。填上真实值，或整行删除以使用默认值 |
| `missing config sections: ...` | 缺少 `[server]`、`[qweather]`、`[weather]`、`[music]` 中的某个必需小节 |
| `cannot read config ...` | 路径不对或权限不足。注意 `server.py` 同目录的 `miku.conf` 必须存在，即使用 `--config` 指定了别的文件 |
| `server.listen_port must be between 1 and 65535` | 端口值非法 |
| `weather latitude or longitude is out of range` | 经纬度超出范围，或经纬度写反了 |
| `every playlist requires integer id and version` | `music.json` 里有播放列表缺少 `id` 或 `version`，或者不是整数 |
| `playlist id ... exceeds the 32-bit limit` | 播放列表 ID 超过 `2147483647`，客户端会崩溃 |
| `media file not found: xxx` | `music.json` 引用了不存在的文件，注意大小写 |

### 23.2 `curl 127.0.0.1:8080/healthz` 失败

Debian：

```bash
sudo systemctl status mikuxperia.service --no-pager
sudo journalctl -u mikuxperia.service -n 100 --no-pager
sudo ss -ltnp | grep 8080
```

Alpine：

```sh
rc-service mikuxperia status
tail -n 100 /var/log/mikuxperia.log
ss -ltnp | grep 8080
```

重点检查配置文件路径、端口占用、文件权限和 Python 报错信息。

### 23.3 天气返回 502 或 QWeather 报错

- `invalid legacy APID`：请求里的 `APID` 不是 `SO-04E_OTENKIMIKU`。用第 5 节的 `curl` 命令复现时要带上完整参数。
- `QWeather HTTP 401` 或 `403`：确认 `api_host` 与 `api_key` 来自同一个 QWeather 账号，套餐和接口权限有效。改完重启服务。
- `QWEATHER_API_HOST is required`：`api_host` 为空。
- `QWEATHER_API_KEY or QWEATHER_BEARER_TOKEN is required`：两个凭据都为空。
- `QWeather returned insufficient daily forecast data`：套餐返回的天数不足，或经纬度落在无数据区域。

### 23.4 媒体文件 404

- 核对 `music.json` 里的文件名与 `media/` 中的实际文件是否完全一致，Linux 区分大小写。
- 确认路径没有以 `/` 开头，也没有 `..`。
- 确认文件对运行用户可读。
- 如果文件名含空格或中文，改成 ASCII 名称，服务端生成 URL 时不做百分号编码。

### 23.5 面板相关

- 打开 `/admin` 显示「WebUI 未启用」：`[webui] enabled` 不是 true，或 `password` 为空。
- 登录后很快掉线：`session_hours` 太短，或服务重启过（会话只存在内存里）。
- 统计一直是 0：确认反向代理转发了真实请求，且 `stats_file` 可写；`/admin` 与 `/healthz` 本身不计入统计。
- 独立访客数异常偏低：反向代理没有转发 `X-Real-IP`。

### 23.6 上传失败

| 提示 | 处理 |
| --- | --- |
| `请选择音频文件` | 表单没有选中音频，或浏览器没提交文件字段 |
| `音频格式不支持` | 扩展名不在允许列表内，先转成 MP3 |
| `歌词文件必须是 UTF-8 编码的纯文本` | 用文本编辑器另存为 UTF-8 |
| `日期格式必须是 YYYY.MM.DD` | 按格式填写，或留空使用当天 |
| `链接必须以 http:// 或 https:// 开头` | 补全协议前缀 |
| `不能上传到由 Bilibili 同步生成的播放列表` | 选择手工维护的列表，或留空使用「手动上传」 |
| `无法写入 music.json`、`无法创建上传目录` | 权限不足，按第 16.4 节设置属主与权限 |
| `上传大小超过 80 MB 限制` | 拆分上传，或先压低音频码率 |
| 反向代理返回 413 | 调大代理的请求体上限，Nginx 是 `client_max_body_size` |

### 23.7 定时任务不执行

在面板确认该任务是否显示「自动关闭」，以及「下次执行」的倒计时；任务失败原因会直接显示在任务区块里。也可以查看服务日志：

```bash
sudo journalctl -u mikuxperia.service -n 100 --no-pager
```

注意任务的上次执行时间是持久化的：如果状态文件里记录的时间还很近，重启后不会立刻重跑。想立刻跑一次就点「立即执行」。

### 23.8 Bilibili Cookie 无法使用

先运行诊断：

```bash
cd /opt/mikuxperia-server
sudo -u mikuxperia python3 sync_bilibili_favorites.py --check-cookie
```

| 输出 | 原因 |
| --- | --- |
| `cookie file not found` | 路径不对。`[bilibili] cookie_file` 的相对路径以 `miku.conf` 所在目录为基准 |
| `cookie file has no usable cookies` | 文件为空或只有注释行 |
| `cookie 缺少 SESSDATA` | 导出时漏了这个字段，它是唯一必需字段 |
| `Cookie 未通过登录校验` | `SESSDATA` 已过期，重新登录浏览器后再导出 |
| `Bilibili API error -101` | 同样是登录态失效 |
| `Bilibili API error -403` 或 `11010` | Cookie 有效但无权访问该收藏夹，私密收藏夹必须用本人账号的 Cookie |

常见格式错误：

```text
# 错误：带引号
"SESSDATA=xxx"

# 错误：带 Cookie: 前缀
Cookie: SESSDATA=xxx

# 正确
SESSDATA=xxx; bili_jct=yyy
```

从 Windows 编辑器上传时注意不要把制表符替换成空格，否则 Netscape 格式会失效；这种情况下改用请求头格式更稳妥。权限也会导致读取失败：

```bash
sudo chown mikuxperia:mikuxperia /opt/mikuxperia-server/bilibili.cookies.txt
sudo chmod 600 /opt/mikuxperia-server/bilibili.cookies.txt
sudo -u mikuxperia head -c 20 /opt/mikuxperia-server/bilibili.cookies.txt
```

面板的「Bilibili 收藏夹缓存」区块也会显示 Cookie 格式、字段数量和缺失的可选字段。

### 23.9 Bilibili 同步报 `Permission denied`

```text
audio download failed for BV1xxxxxxx: ERROR: unable to open for writing:
[Errno 13] Permission denied: '/opt/mikuxperia-server/media/bilibili/123456789/BV1xxxxxxx.mp3'
```

这不是 Cookie 问题，而是缓存目录对运行用户不可写。常见原因是目录被 root 创建过、属主仍是 root：

```bash
ls -ld /opt/mikuxperia-server/media /opt/mikuxperia-server/media/bilibili
id mikuxperia
sudo mkdir -p /opt/mikuxperia-server/media/bilibili
sudo chown -R mikuxperia:mikuxperia /opt/mikuxperia-server/media
sudo chmod -R u+rwX /opt/mikuxperia-server/media
sudo -u mikuxperia python3 sync_bilibili_favorites.py --check-paths
```

不要用 root 直接跑同步脚本：虽然能成功，但新文件属主会变成 root，之后以 `mikuxperia` 运行的服务和任务又会失败。如果已经这样跑过，重新执行一次上面的 `chown`。

systemd 服务里有 `ProtectSystem=full`，写入路径必须包含在 `ReadWritePaths` 中。把媒体目录移到 `/opt/mikuxperia-server` 之外时，要在服务文件里补上路径再 `sudo systemctl daemon-reload`。

### 23.10 Bilibili 单个视频下载失败

脚本会把可识别的失败归类后跳过，并在结束时汇总，不会中断整轮同步；只有目录权限问题才会中止。可识别的原因包括：视频已失效或被删除、视频不可用、私密视频、稿件不可见、需要登录或权限不足、需要大会员、付费内容、不支持的链接类型、没有可用的纯音频流、触发风控、访问被拒绝、网络超时、DNS 解析失败、解析失败等。

```text
folder 3275482587: 生成 3 个播放列表，成功 105 首，下载失败 5 个
skipped 5 video(s):
  - BV1eT41137WH 某个标题：没有可用的纯音频流，可能是仅有 FLV 合并流的老视频（ERROR: Requested format is not available）
```

若跳过数量异常多，先升级 `yt-dlp`，B 站页面结构变化后旧版本会大量解析失败：

```bash
sudo python3 -m pip install --upgrade yt-dlp
yt-dlp --version
```

`Downloading webpage` 只是 `yt-dlp` 的进度输出，不是失败原因；脚本会优先提取含 `ERROR:` 的行，所以看到的应当是真实原因。单个视频的进一步诊断见[第 18.9 节](#189-单个视频诊断)。

### 23.11 反向代理返回 502

通常是 Python 服务没运行或没有监听 `127.0.0.1:8080`。

```bash
sudo systemctl restart mikuxperia.service   # Debian
rc-service mikuxperia restart               # Alpine
curl -i http://127.0.0.1:8080/healthz
```

### 23.12 Caddy 无法申请证书

```bash
sudo journalctl -u caddy -n 100 --no-pager
sudo ss -ltnp | grep -E ':80|:443'
```

常见原因：DNS 未解析到本机、80 或 443 被防火墙拦截、端口被另一个 Web 服务占用，或同一域名同时在别的服务器上申请。

### 23.13 端口冲突

同一台服务器只能有一个服务监听 80 与 443：

```bash
sudo systemctl disable --now nginx    # Debian
```

```sh
rc-update del nginx default           # Alpine
rc-service nginx stop
```

### 23.14 客户端异常

| 现象 | 原因 |
| --- | --- |
| APP 仍访问旧局域网地址 | APK 里的地址是写死的，必须重新反编译替换、打包、签名、安装 |
| `MusicListActivity` 崩溃并提示 `Couldn't read row 0, col 0 from CursorWindow` | 播放列表封面太大，超过客户端 2 MiB 的 `CursorWindow`。按[封面为什么会被强制缩小](#封面为什么会被强制缩小)里的尺寸压小 |
| `MusicDataService` 抛 `NumberFormatException` | 播放列表 ID 超过 `2147483647` |
| 播放时抛 `IllegalArgumentException` | 客户端拿到了带斜杠的文件名。服务端已经做压平处理，出现这种情况说明 XML 被手工改过 |
| 列表不刷新 | 播放列表 `version` 没有递增 |

## 24. 已知限制

- 媒体文件是整体读入内存后返回的，不支持 Range 请求、断点续传和流式传输。大文件被并发请求时内存占用会明显上升。
- 所有响应都带 `Cache-Control: no-store`，客户端和代理不会缓存。
- 管理面板没有 CSRF token，安全性依赖 `SameSite=Strict` 的 Cookie；密码明文保存在配置文件里；会话只在内存中，重启即失效。
- 面板没有验证码和登录频率限制，公网暴露时应在反向代理层限制来源。
- 未匹配的 GET 路径返回状态码 200 的纯文本 `Not found`，不是标准 404。
- 服务是单进程多线程（`ThreadingHTTPServer`），没有 worker 概念，也没有内置访问日志轮转，日志走标准输出，由 systemd 或 OpenRC 收集。
- 访问统计每 25 次请求才落盘一次，进程被强杀会丢失最后几条记录。
- 修改 `miku.conf` 必须重启服务；修改 `music.json` 与媒体文件会在下一次请求时自动生效，但仍建议先跑 `--check`。

## 25. 安全注意事项

- 不要把真实的 QWeather API Key、Tavily API Key、OpenAI 兼容接口 Key 或面板密码提交到公开仓库。
- 不要把 `bilibili.cookies.txt` 提交到仓库或发给他人，它等同于你的账号登录态。
- 已经暴露过的 Key 要立刻在对应控制台撤销并重新生成。
- `miku.conf` 权限设为 `640`，Cookie 文件设为 `600`。
- Python 服务用 `mikuxperia` 低权限用户运行，不要用 root 启动，也不要用 root 跑同步脚本。
- 8080 只监听本机，公网只通过 Nginx 或 Caddy 暴露。
- 面板务必设置强密码，优先通过 HTTPS 访问，并限制可访问的来源 IP。
- 定期备份 `miku.conf`、`music.json` 与 `media/`。
- 只上传可信的音频、图片和歌词文件，并为磁盘和日志设置容量上限。
- 启用缓存清理任务前先确认媒体目录里没有其他用途的文件，它会删除所有未被 `music.json` 引用的文件。

## 26. 附录：Debian 与 Alpine 命令对照

| 项目 | Debian 11 | Alpine |
| --- | --- | --- |
| 包管理 | `apt install` | `apk add` |
| 服务管理 | `systemctl` | `rc-service`、`rc-update` |
| 服务定义 | `/etc/systemd/system/*.service` | `/etc/init.d/*` |
| 日志查看 | `journalctl -u 服务名` | `tail -f /var/log/mikuxperia.log` |
| 定时任务 | systemd timer | crond |
| 创建系统用户 | `useradd --system` | `adduser -S -D -H` |
| Nginx 配置目录 | `sites-available` 与 `sites-enabled` | `/etc/nginx/http.d/` |
| 防火墙 | UFW | iptables 或 awall |
| C 库 | glibc | musl libc |

服务端代码在两种系统上完全相同，不需要修改。musl 与 glibc 的差异不影响纯标准库实现，但如果将来引入需要编译的第三方包，Alpine 可能需要额外安装 `build-base` 等构建依赖。



