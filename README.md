# Media Bot

一个基于 `aiogram + HDHive Open API` 的 Telegram Bot，用于 HDHive 资源检索、链接提取、解锁、每日签到、定时自动签到、可选的 `.strm` 文件监控重命名归档，以及手动触发的 ASS 字幕纯 TTF 子集化字体内封。

## 功能

- `/hdt` 搜索剧集资源
- `/hdm` 搜索电影资源
- 直接发送 HDHive 链接自动解析（可通过 `HDHIVE_PARSE_INCOMING_LINKS` 开关控制）
- 积分解锁（支持阈值自动解锁）
- HDHive 解锁请求统一走队列排队，并支持每秒限速
- `/points` 查询积分
- `/checkin` 手动每日签到
- `CHECKIN_CRON` 定时自动签到
- 自动签到失败时通知 `ALLOWED_USER_ID`
- `/danmu` 下载 B 站弹幕 XML
- `/ass` 手动执行 ASS 字幕纯 TTF 子集化字体内封
- `/strm_status` 查看 STRM 监控服务状态
- `/strm_scan` 手动触发一次 STRM 存量重扫
- `/strm_restart` 手动重启 STRM watcher
- `/rm_strm` 预览 STRM 空目录清理，并在消息按钮中确认/取消实际删除
- 可选启用 STRM 监控：实时探测、重命名、失败归档、整目录移动到 DONE
- 可选启用 STRM Telegram 通知：按目录批次/根目录文件聚合推送归档结果，统计项区分“重命名 / 原本已就绪 / 失败转移”

## 环境变量

必填：

- `BOT_TOKEN`
- `HDHIVE_API_KEY`

常用可选：

- `ALLOWED_USER_ID`：机器人可用用户，同时也是自动签到失败通知接收人
- `AUTO_UNLOCK_THRESHOLD`
- `HDHIVE_UNLOCK_RATE_LIMIT_PER_SECOND`：HDHive 解锁队列限速，默认每秒 `3` 次
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
- `TGBOT_NOTIFY_CHAT_ID`：STRM 归档通知接收目标（用户/群组/频道 Chat ID）
- `ASS_TARGET_HOST_DIR`：宿主机字幕目录，供 Docker 挂载
- `ASS_TARGET_DIR`：容器内 ASS 处理目录，`/ass` 命令从这里读取字幕/字体/压缩包
- `ASS_NOTIFY_CHAT_ID`：`/ass` 汇总通知目标；留空时回退 `TGBOT_NOTIFY_CHAT_ID`，再回退 `ALLOWED_USER_ID`
- `ASS_RECURSIVE`：`/ass` 是否递归扫描子目录
- `ASS_INCLUDE_SYSTEM_FONTS`：`/ass` 是否把系统字体纳入纯 TTF 字体池
- `ASS_WORK_DIR`：`/ass` 临时工作目录（默认 `<ASS_TARGET_DIR>/.assfonts_pipeline_work`）
- `STRM_WATCH_ENABLED`：是否启用 STRM 监控
- `STRM_WATCH_DIR` / `STRM_DONE_DIR` / `STRM_FAILED_DIR`
- `STRM_FFPROBE_PATH`：默认 `/usr/local/bin/ffprobe`
- `STRM_STATE_DIR`：manifest 批次状态目录，默认 `/app/data/strm_state`
- `STRM_PROCESSING_LEASE_SECONDS`：processing 租约，默认 `1800`
- `STRM_STATE_RETENTION_HOURS`：已完成/失败 manifest 自动清理保留期，默认 `168`
- `STRM_PRUNE_ENABLED`：是否启用 `/rm_strm` 手动空目录清理
- `STRM_PRUNE_ROOTS`：手动清理扫描根目录列表，使用 `|` 分隔
- `STRM_PRUNE_ALLOW_DELETE_FIRST_LEVEL` / `STRM_PRUNE_INCLUDE_ROOTS`
- `STRM_PRUNE_NOTIFY_EMBY`：删除后是否通知 Emby 局部刷新并补做递归刷新
- `STRM_PRUNE_EMBY_URL` / `STRM_PRUNE_EMBY_API_KEY` / `STRM_PRUNE_EMBY_UPDATE_TYPE`
- `STRM_PRUNE_HTTP_TIMEOUT` / `STRM_PRUNE_HTTP_RETRIES` / `STRM_PRUNE_HTTP_BACKOFF`

说明：

- 关键词搜索依赖 TMDB API，因此 `/hdt` 和 `/hdm` 建议同时配置 `TMDB_API_KEY`
- `/points`、`/checkin` 和自动签到依赖 HDHive Premium 权限对应的 Open API
- `MEDIA_BOT_DEBUG=true` 时会输出 DEBUG 日志，便于排查问题
- `MEDIA_BOT_LOG_TO_FILE=0` 时，日志仅输出到 stdout/stderr，可直接用 `docker compose logs -f media_bot` 查看
- `TGBOT_NOTIFY_CHAT_ID` 配置后，STRM 在归档根目录文件或完成目录批次归档时会发送 Telegram 汇总通知
- `/ass` 不写独立本地日志文件，运行详情直接进入 Docker 日志，并通过 Telegram 返回汇总消息
- `/ass` 在真正内嵌前会先把字体池中的 OTF 转成 TTF，并复制原字体 name table，后续只用纯 TTF/TTC 做匹配与内嵌
- `/rm_strm` 默认只预览；需在 Bot 返回消息下点击“确认删除”按钮才会实际删除
- STRM 监控依赖系统中的 `ffprobe` 和 `inotifywait`，Dockerfile 已自动安装

## Docker 运行（默认远程镜像）

```bash
cd /path/to/media_bot
docker compose pull
docker compose up -d
```

查看日志：

```bash
docker compose logs -f media_bot
```

### /ass 使用前准备

1. 在 `.env` 中设置：

```env
MEDIA_BOT_LOG_TO_FILE=0
ASS_TARGET_HOST_DIR=/你的宿主机字幕目录
ASS_TARGET_DIR=/ass_target
ASS_NOTIFY_CHAT_ID=
```

2. `docker-compose.yml` 已默认挂载：

```yaml
- ${ASS_TARGET_HOST_DIR:-./data/ass_target}:${ASS_TARGET_DIR:-/ass_target}
```

3. 在 Telegram 中手动发送：

```text
/ass
```

Bot 会：

- 扫描 `ASS_TARGET_DIR`
- 自动解压 `7z/zip` 字体包
- 把 OTF 前置转成 TTF
- 跳过已存在的 `*.assfonts.ass`
- 在 Docker 日志输出详细过程
- 最后在 Telegram 返回汇总信息
