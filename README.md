# Media Bot

一个基于 `aiogram + HDHive Open API` 的 Telegram Bot，用于 HDHive 资源检索、链接提取、解锁、每日签到、定时自动签到、可选的 `.strm` 文件监控重命名归档，以及手动触发的 ASS 字幕处理（子集化字体内封 / MKV 字幕内封）。

## 功能

- `/hdt` 搜索剧集资源
- `/hdm` 搜索电影资源
- 直接发送 HDHive 链接自动解析（可通过 `HDHIVE_PARSE_INCOMING_LINKS` 开关控制）
- 积分解锁（支持阈值自动解锁）
- HDHive 解锁请求统一走队列排队，并支持每分限速
- `/points` 查询积分
- `/checkin` 手动每日签到
- `CHECKIN_CRON` 定时自动签到
- 自动签到失败时通知 `bot_chat_id`（若为空则回退 `bot_user_id`）
- `/danmu` 下载 B 站弹幕 XML
- `/ass` 打开 ASS 菜单：
  - 子集化字体：基于 [assfonts](https://github.com/wyzdwdz/assfonts) 的 ASS 字幕纯 TTF 字体子集化并生成 `*.assfonts.ass`
  - 内封字幕：把同目录匹配到的 `.ass/.sup` 内封到 `.mkv`
- `/strm_status` 查看 STRM 监控服务状态
- `/strm_scan` 手动触发一次 STRM 存量重扫
- `/strm_restart` 手动重启 STRM watcher
- `/emby_tasks` 打开 Emby 计划任务面板（查看 / 启动 / 停止 / 轮询通知开关）
- `/rm_strm` 预览 STRM 空目录清理，并在消息按钮中确认/取消实际删除
- 可选启用 STRM 监控：实时探测、重命名、失败归档、整目录移动到 DONE
- 可选启用 STRM Telegram 通知：按目录批次/根目录文件聚合推送归档结果，统计项区分“重命名 / 原本已就绪 / 失败转移”

## 环境变量

必填：

- `BOT_TOKEN`
- `HDHIVE_API_KEY`

常用可选：

- `bot_user_id`：可使用 Telegram 机器人的用户 ID 清单，多个用户用 `,` 分隔；留空则所有用户都能使用
- `AUTO_UNLOCK_THRESHOLD`
- `HDHIVE_UNLOCK_RATE_LIMIT_PER_MINUTE`：HDHive 解锁队列限速，默认每分 `3` 次
- `HDHIVE_PARSE_INCOMING_LINKS`：是否自动解析聊天中直接收到的 HDHive 链接（默认开启）
- `CHECKIN_CRON`：5 段 cron，留空禁用自动签到
- `CHECKIN_TIMEZONE`：默认 `Asia/Shanghai`
- `TMDB_API_KEY`：关键词搜索推荐必填
- `SA_URL`
- `SA_PARENT_ID`
- `SA_AUTO_ADD_DELAY`
- `SA_TOKEN`
- `SA_ENABLE_115_PUSH`
- `MEDIA_BOT_DEBUG`：是否输出调试日志（true/false）
- `MEDIA_BOT_LOG_TO_FILE`：是否同时写本地日志文件；默认 `0`，仅输出到 Docker 日志
- `bot_chat_id`：接受消息通知的用户、群组或频道 Chat ID，多个用 `,` 分隔；用于 STRM / ASS 汇总和自动签到失败通知

### /ass 子集化字体相关

- `ASS_TARGET_HOST_DIR`：宿主机字幕目录，供 Docker 挂载
- `ASS_TARGET_DIR`：容器内 ASS 处理目录，`/ass -> 子集化字体` 从这里读取字幕/字体/压缩包
- `ASS_NOTIFY_CHAT_ID`：`/ass` 子集化字体汇总通知目标；留空时回退 `bot_chat_id`，再回退 `bot_user_id` 第一个用户
- `ASS_CLEANUP_WORK_DIR_ON_SUCCESS`：是否在子集化全部成功后自动清理工作目录，默认 `1`
- `ASS_CLEANUP_WORK_DIR_ON_FAILURE`：是否在失败后也清理工作目录，默认 `0`
- `ASS_DELETE_SOURCE_ASS_ON_SUCCESS`：是否在全部成功后删除原始 ASS，默认 `0`
- `ASS_RECURSIVE`：是否递归扫描子目录
- `ASS_INCLUDE_SYSTEM_FONTS`：是否把系统字体纳入纯 TTF 字体池
- `ASS_WORK_DIR`：临时工作目录（默认 `<ASS_TARGET_DIR>/.assfonts_pipeline_work`）

### /ass 字幕内封相关

- `ASS_MUX_TARGET_HOST_DIR`：宿主机视频/字幕目录，供 Docker 挂载
- `ASS_MUX_TARGET_DIR`：容器内字幕内封目录，`/ass -> 内封字幕` 从这里扫描 `.mkv` 与同目录 `.ass/.sup`
- `ASS_MUX_NOTIFY_CHAT_ID`：`/ass` 字幕内封汇总通知目标；留空时回退 `bot_chat_id`，再回退 `bot_user_id` 第一个用户
- `ASS_MUX_RECURSIVE`：是否递归扫描子目录
- `ASS_MUX_DEFAULT_LANG`：默认字幕语言（如 `chs` / `cht` / `eng` / `chs_eng`），仅在文件名无法识别语言时回退使用
- `ASS_MUX_DEFAULT_GROUP`：默认字幕组
- `ASS_MUX_JOBS`：并发执行 `mkvmerge` 的线程数
- `ASS_MUX_DELETE_EXTERNAL_SUBS`：是否默认在内封成功后删除外挂字幕
- `ASS_MUX_SET_DEFAULT_SUBTITLE`：是否自动设置默认字幕轨，默认 `1`；规则为“简体双语优先，其次简体”，并把 MKV 现有字幕一起纳入判断
- `ASS_MUX_ALLOW_CROSS_FS`：是否允许临时目录与 MKV 跨文件系统
- `ASS_MUX_TMP_DIR`：临时目录（默认 `<ASS_MUX_TARGET_DIR>/.ass_mux_tmp`）
- `ASS_MUX_IDLE_TIMEOUT_SECONDS`：标准模式空闲超时秒数，默认 `1800`（30 分钟）
- `ASS_MUX_SOFT_WARN_AFTER_SECONDS`：标准模式总耗时软告警阈值，默认 `7200`（2 小时）
- `ASS_MUX_HARD_CAP_SECONDS`：标准模式极限保险阈值，默认 `43200`（12 小时）
- `ASS_MUX_PROGRESS_POLL_INTERVAL_SECONDS`：进度轮询间隔，默认 `5`
- `ASS_MUX_TERMINATE_GRACE_SECONDS`：先 `terminate` 后等待再 `kill` 的宽限秒数，默认 `15`
- `ASS_MUX_PLAN_PATH`：计划文件保存位置（默认 `<ASS_MUX_TARGET_DIR>/.ass_mux_plan.json`）
- `ASS_MKVMERGE_BIN`：`mkvmerge` 可执行文件路径，默认 `mkvmerge`

### 其他常用配置

- `STRM_PRUNE_ENABLED`：是否启用 `/rm_strm` 手动空目录清理
- `STRM_PRUNE_ROOTS`：手动清理扫描根目录列表，使用 `|` 分隔
- `STRM_PRUNE_ALLOW_DELETE_FIRST_LEVEL` / `STRM_PRUNE_INCLUDE_ROOTS`
- `STRM_PRUNE_NOTIFY_EMBY`：删除后是否通知 Emby 局部刷新并补做递归刷新
- `STRM_PRUNE_EMBY_URL` / `STRM_PRUNE_EMBY_API_KEY` / `STRM_PRUNE_EMBY_UPDATE_TYPE`
- `STRM_PRUNE_HTTP_TIMEOUT` / `STRM_PRUNE_HTTP_RETRIES` / `STRM_PRUNE_HTTP_BACKOFF`
- `EMBY_TASKS_ENABLED`：是否启用 `/emby_tasks` Emby 计划任务面板
- `EMBY_TASKS_URL` / `EMBY_TASKS_API_KEY` / `EMBY_TASKS_SERVER_TYPE`
- `EMBY_TASKS_NOTIFY_ENABLED`：默认是否开启后台轮询通知（任务完成 / 失败）
- `EMBY_TASKS_POLL_INTERVAL` / `EMBY_TASKS_REQUEST_TIMEOUT` / `EMBY_TASKS_HTTP_RETRIES` / `EMBY_TASKS_HTTP_BACKOFF`
- `EMBY_TASKS_STATE_PATH`：保存通知开关的本地状态文件

说明：

- 关键词搜索依赖 TMDB API，因此 `/hdt` 和 `/hdm` 建议同时配置 `TMDB_API_KEY`
- `/points`、`/checkin` 和自动签到依赖 HDHive Premium 权限对应的 Open API
- `MEDIA_BOT_DEBUG=true` 时会输出 DEBUG 日志，便于排查问题
- `MEDIA_BOT_LOG_TO_FILE=0` 时，日志仅输出到 stdout/stderr，可直接用 `docker compose logs -f media_bot` 查看
- `bot_chat_id` 配置后，STRM、`/ass`、`/emby_tasks` 轮询结果和自动签到失败可发送 Telegram 通知
- `/ass` 不写独立本地日志文件，运行详情直接进入 Docker 日志
- `/ass -> 子集化字体` 在真正内嵌前会先把字体池中的 OTF 转成 TTF，并复制原字体 name table，后续只用纯 TTF/TTC 做匹配与内嵌
- `/ass -> 子集化字体` 所使用的 ASS 字幕字体子集化与内嵌脚本/方法完全来自开源项目 [`wyzdwdz/assfonts`](https://github.com/wyzdwdz/assfonts)；本项目主要补充 Telegram 交互、批处理编排与工程化封装
- `/ass -> 内封字幕` 使用 `mkvmerge` 写回原 MKV；支持在 Telegram 中逐项修改默认字幕组、语言、字幕文件名，并可切换 DRY-RUN / 删除外挂字幕
- `/ass -> 内封字幕` 现已内置标准模式超时保护：空闲超时 30 分钟、总时长 2 小时仅告警、12 小时极限保险，不提供 TG 选择按钮
- `/ass -> 内封字幕` 已支持 Telegram 中按页翻页预览计划细节，并在执行确认前显示磁盘空间、源视频总大小、平均/最大单集大小、预计临时占用、是否同分区等信息
- `/rm_strm` 默认只预览；需在 Bot 返回消息下点击“确认删除”按钮才会实际删除
- STRM 监控依赖系统中的 `ffprobe` 和 `inotifywait`，Dockerfile 已自动安装

## Docker 运行

```bash
cd /path/to/media_bot
docker compose up -d --build
```

查看日志：

```bash
docker compose logs -f media_bot
```

### /ass 使用前准备

1. 在 `.env` 中至少设置：

```env
MEDIA_BOT_LOG_TO_FILE=0

# 子集化字体
ASS_TARGET_HOST_DIR=/你的字幕目录
ASS_TARGET_DIR=/ass_target

# 字幕内封
ASS_MUX_TARGET_HOST_DIR=/你的视频与字幕目录
ASS_MUX_TARGET_DIR=/ass_mux_target
ASS_MUX_DEFAULT_LANG=chs
ASS_MUX_DEFAULT_GROUP=
ASS_MUX_JOBS=2
```

2. `docker-compose.yml` 已默认挂载：

```yaml
- ${ASS_TARGET_HOST_DIR:-./data/ass_target}:${ASS_TARGET_DIR:-/ass_target}
- ${ASS_MUX_TARGET_HOST_DIR:-./data/ass_mux_target}:${ASS_MUX_TARGET_DIR:-/ass_mux_target}
```

3. 在 Telegram 中发送：

```text
/ass
```

4. Bot 会弹出菜单：

- `🔤 子集化字体`
  - 扫描 `ASS_TARGET_DIR`
  - 自动解压 `7z/zip` 字体包
  - 把 OTF 前置转成 TTF
  - 跳过已存在的 `*.assfonts.ass`
  - 在 Docker 日志输出详细过程
  - 最后在 Telegram 返回汇总信息
- `🎞️ 内封字幕`
  - 扫描 `ASS_MUX_TARGET_DIR` 下的 `.mkv` 与同目录 `.ass/.sup`
  - 自动生成计划并在 Telegram 中显示预览
  - 需要时可逐项修改字幕文件、字幕组、语言
  - 可切换 `DRY-RUN` 与“删除外挂字幕”
  - 确认后调用 `mkvmerge` 并在 Docker 日志输出逐集过程
  - 完成后向 Telegram 返回汇总通知

## 第三方项目声明与致谢

### assfonts 来源声明

- 本项目中 `/ass -> 子集化字体` 所使用的字幕内嵌子集化字体核心脚本、处理方法与命令行能力，完全来自开源项目 [`wyzdwdz/assfonts`](https://github.com/wyzdwdz/assfonts)。
- 当前 `media_bot` 对该能力的工作主要是面向 Telegram Bot 使用场景做工程化封装与集成，包括目录扫描、批处理编排、OTF 预转 TTF、name table 复制、执行日志输出、汇总通知，以及 `/ass` 菜单交互。
- 本项目 Dockerfile 在构建镜像时会下载并安装 `assfonts` 官方发布的 CLI；`/ass -> 子集化字体` 流水线实际通过调用该上游工具完成核心处理。
- 除上述工程化封装与交互集成外，本项目不对 `assfonts` 的字体子集化 / 字体内嵌算法、实现或方法主张原创性。

### 致谢

- 感谢 [`wyzdwdz`](https://github.com/wyzdwdz) 开源并持续维护 `assfonts` 项目，使 ASS 字幕字体子集化与内嵌流程可以稳定复用到本项目中。
- `assfonts` 项目主页：<https://github.com/wyzdwdz/assfonts>

### 许可证说明

- 根据 `assfonts` 上游仓库公开信息，其许可证为 **GNU General Public License v3.0 (GPL-3.0)**。
- 使用、分发或二次集成本项目中 `/ass -> 子集化字体` 相关能力时，请同时关注并遵守 `assfonts` 上游项目的许可证、版权声明及其附带要求。
- 上游文档与许可证请以官方仓库为准：
  - README：<https://github.com/wyzdwdz/assfonts/blob/main/README.md>
  - LICENSE：<https://github.com/wyzdwdz/assfonts/blob/main/LICENSE>
