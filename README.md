# 影视资源自动追踪控制台

影视资源自动追踪与下载系统（fnOS / NAS）。给它一个片名——美剧、电影都行——它负责找到 4K 资源、
离线到网盘、下载到 NAS、配好字幕，并且持续盯着更新，新集一上线就自动下载。

链路：**多源搜索（6 个 Jackett 索引器 + bt4g RSS）** → **光鸭云盘「云下载」**（云端离线，不占本地带宽）
→ **光鸭「下载到NAS」**（aria2 16 线程直链）→ **自动中文字幕**（内嵌提取 / SubHD）

## 能做什么

| 能力 | 说明 |
|---|---|
| 追剧 | 登记一部剧 + 已看到第几集，之后每集自动跟。新集上线即下载 |
| 追电影 | 报片名，自动找 4K 版本下载。未上映/未出资源的会保持等待 |
| 画质优先 | 默认 2160p；没有 4K 时列出 1080p 候选并询问，不擅自降级 |
| 中文字幕 | 优先资源内嵌中文字幕，其次 SubHD；**优先 .ass、双语字幕优先**，自动重命名到同目录 |
| 中文片名 | 直接输入「黑帮领地 第二季」这类中文名，自动经豆瓣识别为英文原名 |
| 下载节奏 | 一次下几集、攒批间隔、搜索周期都可调，避免占满带宽 |
| Web 控制台 | 浏览器里登记、搜索、看进度、改设置，无需敲命令 |

## 数据源

7 个搜索源并行（其中 6 个走 Jackett 索引器，bt4g 走它自己的 RSS 接口）：

| 源 | 类型 | 备注 |
|---|---|---|
| thepiratebay | 磁链 | |
| knaben | 磁链 | 结果最丰富，单次可达 90+ 条 |
| therarbg | 磁链 | |
| limetorrents | 种子 | |
| torrentgalaxyclone | 种子 | |
| uindex | 磁链 | 需 FlareSolverr（见下） |
| bt4g | 磁链 | 走 CF 豁免的 RSS 接口，无需代理 |

六个 Jackett 索引器并行搜索约 1.4 秒返回 260+ 条结果，其中 2160p 通常有 30+ 条。

## 日常使用

```bash
cd /vol1/@appdata/autodl

# 搜索看看有什么资源（不下载）
python3 gy_autodl.py search "MobLand S02E07"
python3 gy_autodl.py search "Oppenheimer 2023"

# 一次性下载（单集或电影），无 4K 时会列出 1080p 候选并退出（exit 2），确认后：
python3 gy_autodl.py add "MobLand S02E07"
python3 gy_autodl.py add "Oppenheimer 2023"

# 登记追剧：从看完的最后一集之后开始自动追踪
python3 gy_autodl.py addshow "MobLand" --last S02E06
python3 gy_autodl.py shows          # 列出追剧
python3 gy_autodl.py rmshow "MobLand"

# 状态 / 手动推进
python3 gy_autodl.py status
python3 gy_autodl.py run            # cron 同款：推进任务 + 搜新集
python3 gy_autodl.py poll           # 只推进已有任务，不搜索

# 手动补字幕（优先内嵌 zh → SubHD 中文 → 内嵌 en 兜底）
python3 gy_autodl.py sub "/vol3/1000/媒体库2/xxx.mkv"
```

## 自动化

cron（root）：`13 * * * *` 每小时第 13 分执行 `run`，日志 `logs/cron.log`。
追剧登记在 `shows.json`，每次 run 搜索各剧的下一集，有 2160p 新资源就自动走完整链路。

## 流程细节

1. **搜索**：并行查 6 个 Jackett 索引器（thepiratebay/knaben/therarbg/limetorrents/torrentgalaxyclone/uindex）+ bt4g 原生 RSS 源。
2. **选优**：优先 2160p；排序 = 非意大利语 > Repack > REMUX > BluRay > WEB > 种子数 > 体积。磁链直用；纯种子先本地取字节转 infohash 磁链。
3. **云下载**：磁链提交光鸭离线（`自动下载/{剧名}/` 或 `自动下载/电影/{片名}/`），选中视频文件（排除样片/小文件）。
4. **NAS 落盘**：云任务 100% 后经 signedURL 用 aria2 下到 `/vol3/1000/媒体库2/{剧名}/` 或 `媒体库2/电影/{片名}/`，文件保留 release 原名。
5. **字幕**：同目录已有 → 内嵌中文字幕 ffmpeg 提取为 `xxx.zh.srt` → **SubHD 下载中文字幕**（支持 ZIP/RAR5 包，CHS/CHT 优先，ass 自动转 srt）→ 内嵌英文提取为 `xxx.en.srt` 兜底。

## 配置（config.json，可选）

```json
{
  "wanted_quality": "2160p",
  "max_submit_per_run": 3,
  "min_video_mb": 300,
  "batch_size": 2,
  "batch_interval_hours": 6,
  "backfill_enabled": true,
  "auto_download_new": true,
  "subhd_email": "xx@xx.com",
  "subhd_password": "..."
}
```

### 下载节奏（针对已上线 / 批量补集）

| 配置 | 默认 | 含义 |
|---|---|---|
| `batch_size` | 2 | 一批最多连续追下几集；本批还没下完不会开下一批 |
| `batch_interval_hours` | 6 | 这批全部完成后再等多久才开始下一批（0 = 不等） |
| `backfill_enabled` | true | true = 连续补集；false = 每次只追最新已上线的那一集 |
| `auto_download_new` | true | 只在**未上线→刚上线**时有用：true = 搜到 2160p 立即自动下载；false = 只登记到「排队中的集数」，等你在 Web 上手动确认 |
| `max_submit_per_run` | 3 | 单次 cron 循环最多提交多少个任务（跨剧总上限） |
| `search_interval_hours` | 24 | **追剧搜索周期**：多少小时搜一次新集。24 = 每天一次，12 = 每天两次，0 = 每次 cron 循环（每小时）都搜 |

这些都能在 Web 控制台「⚙️ 下载节奏设置」里直接改，保存即写回 `config.json`。

### 搜索周期 vs 下载节奏的区别

两件事分开控制，互不干扰：

- **`search_interval_hours`（搜索周期）**：多久去 Jackett **查一次**有没有新集。查得勤才能及时发现新资源，但会产生搜索请求。
- **`batch_size` / `batch_interval_hours`（下载节奏）**：查到新集后**实际下载**的频率。控制的是 NAS 带宽和磁盘占用。

举例：搜索周期 24 小时 + 每批 2 集 + 攒批间隔 6 小时 —— 每天搜一次，发现的新集一次最多下 2 集，下完等 6 小时再来下一批。

云下载和 NAS 下载的**推进**（`phase_cloud` / `phase_nas`）不受搜索周期限制，cron 每小时都会检查，
所以已经在下载的任务不会因为搜索周期而变慢。

当前搜索状态可以在 `python3 gy_autodl.py status` 或 Web 控制台的设置卡片里看到（🔍 现在会搜 / ⏳ 等待中 + 下次时间）。
被节奏拦下的集数会显示在「⏳ 排队中的集数」卡片里，带原因和预计开始时间。

## 字幕规则

优先级：**双语（中英）> 中文单语 > 其他**，同级 **.ass > .srt**。

1. 已有字幕文件 → 跳过
2. 视频内嵌中文字幕 → 用 ffmpeg 直接导出 `.zh.ass`（保留原样式），失败退回 `.srt`
3. SubHD 搜索下载 → 优先保留 `.ass` 原格式，多候选按上面优先级挑
4. 兜底：内嵌英文字幕导出 `.en.ass`

字幕文件名与视频同目录同名，只改扩展名（如 `Slow Horses S01E05 ....mkv` → `Slow Horses S01E05 ....zh.ass`），影视软件可直接识别。

## 中文片名支持

输入中文剧名/电影名自动识别为英文名（豆瓣数据源，含季号转换）：

- `search 奥本海默` → 自动用 `Oppenheimer` 搜索
- `add 黑帮领地 第二季` → 自动转为 `MobLand S02` 下载
- `addshow 最后生还者 第二季` → 登记 `The Last of Us S02`，从该季第 1 集开始追踪
- 识别失败（查不到/网络问题）时原词直搜，不影响使用
- Web 控制台同样支持，搜索结果顶部显示识别结果

## Web 控制台（:8770）

浏览器打开 http://192.168.1.3:8770 —— 登记心愿/追剧、搜索资源、立即下载、看任务进度与事件。
后端 `web/app.py`（复用 gy_autodl 链路），cron 每 5 分钟保活（幂等，重复启动自动退出）。
心愿清单存 `wishlist.json`；登记心愿后点「转追剧」或「立即下载」落地为真实任务。

## 状态与日志

- `state.json`：任务状态机（cloud → nas → done/failed），已处理 infohash 防重复。
- `logs/run.log`：运行日志；`logs/events.log`：关键事件（提交/完成/失败）。
- 失败自动重试 2 次；云任务 72h 超时判失败；30 天后清理历史记录。

## 资源筛选偏好

决定「什么样的资源才值得下」。全部在 Web 控制台「🎯 资源筛选偏好」卡片里设置，也可直接改 `config.json`。

| 配置项 | 默认 | 说明 |
|---|---|---|
| `wanted_quality` | `2160p` | 首选清晰度：`2160p` / `1080p` / `any`（不限，4K 优先） |
| `quality_fallback` | `[]` | 首选档没有时的降级允许列表，如 `["1080p"]`。**留空 = 不降级**，找不到 4K 就停下来询问你 |
| `wanted_lang` | `en_prefer` | 语种偏好：`en_prefer`（英语优先，外语版仅兜底）/ `en_only`（排除含外语标签的资源）/ `any`（不限）。未标语种的资源视为英语 |
| `allow_format` | `[]` | 允许的容器格式白名单，如 `["mkv","mp4"]`。**留空 = 不限格式** |
| `prefer_format` | `["mkv"]` | 格式优先级，靠前的优先。同名资源 mkv 优先于 mp4 |
| `prefer_codec` | `[]` | 编码优先级，如 `["hevc","av1","h264"]` |
| `prefer_source` | `[]` | 片源优先级，如 `["remux","bluray","web"]`。留空用默认权重 REMUX > BluRay > WEB |
| `hdr_prefer` | `true` | 同条件下优先 HDR / Dolby Vision 版本 |
| `size_min_gb` | `0` | 单集/单片体积下限（GB），0 = 不限 |
| `size_max_gb` | `0` | 单集/单片体积上限（GB），0 = 不限 |

### 筛选发生在哪两步

1. **搜索阶段**（`best_for_episode` / `best_for_movie`）：按标题解析出的清晰度、格式、编码做初筛，
   体积用索引器报告的资源总大小判断。
2. **提交阶段**（`submit`）：磁链 resolve 之后拿到真实文件列表，再用**真实扩展名**复核格式白名单、
   用**真实文件大小**复核体积上限。这样能挡住标题没标格式的资源。

### 格式判断的注意点

很多 release 名里并不写容器格式（如 `MobLand.S02E06.2160p.WEB-DL.HDR.H.265-G66` 实际是 mkv 但标题无 mkv 字样）。
所以：

- 白名单开启时，`fmt` 为 `None`（标题未标注）的资源**先放行**，到提交阶段看真实扩展名再决定。
- 优先级排序里，未标注格式的资源 `fmt_rank` 最低，会排在明确标了首选格式的资源后面。

实测 Oppenheimer 2023：337 条结果 / 111 条 4K，开启 `allow_format=["mkv","mp4"]` + `size 2~30GB`
筛选后剩 36 条，推荐 `Oppenheimer 2023 4K UHD IMAX BluRay 2160p HDR DTS-HD MA`（26.6GB，65 做种，HEVC）。

## FlareSolverr（uindex.org 必需）

uindex.org 全站（含搜索页）启用了 Cloudflare non-interactive 挑战，普通 HTTP 请求和 headless
浏览器（含持久化会话、cloudscraper）都返回 403。解决方案是本地部署 FlareSolverr 容器：

```bash
cd /vol1/@appdata/autodl/flaresolverr
docker compose up -d          # 监听 :8191，restart: unless-stopped
```

Jackett 侧配置（`/vol1/@appdata/jackett/data/ServerConfig.json`）：

```json
"FlareSolverrUrl": "http://127.0.0.1:8191",
"FlareSolverrMaxTimeout": 60000
```

改完需重启 Jackett：`trim-cli app restart jackett --yes`。

验证：`curl -s "http://127.0.0.1:9117/api/v2.0/indexers/uindex/results?apikey=KEY&Query=slow+horses"`
应返回 100 条结果且无 Error。

FlareSolverr 只在索引器遇到 CF 挑战时被调用，正常搜索不产生额外开销。实测六个索引器并行
搜索 268 条结果耗时 1.4 秒。

## 已知限制

- 无 4K 资源时 `add` 不自动降级（退出码 2 列出 1080p 候选，人工确认）；`add --allow1080` 可自动降级。
- 字幕：中文优先 SubHD（config.json 已配置账号）；SubHD 偶发限流/图形字幕包（.sup 无文本）时自动重试多候选或回退内嵌英文字幕。
- 剧集按「单集资源」追踪；整季包暂不支持自动下载（可手动 add 单集）。
- 需要光鸭网盘(:8998)和 Jackett(:9117) 两个应用处于运行状态。
