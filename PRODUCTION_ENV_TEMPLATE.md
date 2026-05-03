# Media Bot 生产环境建议模板

> 这是基于当前仓库代码、已通过的启动烟测和生产审计整理的**建议版**模板。
> 使用前请按你的实际 Telegram / HDHive / 路径 / Emby / Symedia 环境修改。

## 一、最小生产必填

```env
# Telegram
BOT_TOKEN=替换成你的 Telegram Bot Token

# HDHive Open API
HDHIVE_API_KEY=替换成你的 HDHive Open API Key
HDHIVE_BASE_URL=https://hdhive.com
HDHIVE_OPEN_API_BASE_URL=https://hdhive.com/api/open

# 生产强烈建议配置：限制谁能使用机器人
# 多个用户用英文逗号分隔
bot_user_id=123456789

# 生产强烈建议配置：通知目标
# 可填私聊 / 群组 / 频道 Chat ID，多个用英文逗号分隔
bot_chat_id=123456789
```

## 二、推荐基础生产配置

```env
# 关键词搜索
TMDB_API_KEY=

# 链接直发自动解析
HDHIVE_PARSE_INCOMING_LINKS=1

# 自动解锁阈值
# 0 = 关闭自动解锁（生产更稳妥）
AUTO_UNLOCK_THRESHOLD=0

# HDHive 解锁限速
HDHIVE_UNLOCK_RATE_LIMIT_PER_MINUTE=3

# 自动签到
CHECKIN_CRON=
CHECKIN_TIMEZONE=Asia/Shanghai
CHECKIN_GAMBLE=0

# 日志
MEDIA_BOT_LOG_TO_FILE=0
MEDIA_BOT_LOG_PATH=media_bot.log
MEDIA_BOT_DEBUG=false

# 健康检查心跳文件
MEDIA_BOT_HEALTHCHECK_STATE_PATH=/tmp/media_bot_health.json
```

## 三、如果要接 Symedia / 115 自动推送

```env
SA_URL=
SA_PARENT_ID=
SA_TOKEN=symedia
SA_ENABLE_115_PUSH=1
SA_AUTO_ADD_DELAY=60
```

> 若不需要 Symedia，可保持空值；当前代码会给出警告，但不会阻止启动。

## 四、如果要启用 /ass -> 子集化字体

```env
ASS_TARGET_HOST_DIR=./data/ass_target
ASS_TARGET_DIR=/ass_target
ASS_RECURSIVE=0
ASS_INCLUDE_SYSTEM_FONTS=1
ASS_NOTIFY_CHAT_ID=
ASS_CLEANUP_WORK_DIR_ON_SUCCESS=1
ASS_CLEANUP_WORK_DIR_ON_FAILURE=0
ASS_DELETE_SOURCE_ASS_ON_SUCCESS=0
ASS_WORK_DIR=
ASSFONTS_BIN=/usr/local/bin/assfonts
ASS_FONTFORGE_BIN=fontforge
ASS_7Z_BIN=7z
ASS_UNZIP_BIN=unzip
```

## 五、如果要启用 /ass -> 字幕内封

```env
ASS_MUX_TARGET_HOST_DIR=./data/ass_mux_target
ASS_MUX_TARGET_DIR=/ass_mux_target
ASS_MUX_RECURSIVE=0
ASS_MUX_NOTIFY_CHAT_ID=
ASS_MUX_DEFAULT_LANG=chs
ASS_MUX_DEFAULT_GROUP=
ASS_MUX_JOBS=2
ASS_MUX_DELETE_EXTERNAL_SUBS=0
ASS_MUX_SET_DEFAULT_SUBTITLE=1
ASS_MUX_ALLOW_CROSS_FS=0
ASS_MUX_IDLE_TIMEOUT_SECONDS=1800
ASS_MUX_SOFT_WARN_AFTER_SECONDS=7200
ASS_MUX_HARD_CAP_SECONDS=43200
ASS_MUX_PROGRESS_POLL_INTERVAL_SECONDS=5
ASS_MUX_TERMINATE_GRACE_SECONDS=15
ASS_MUX_TMP_DIR=
ASS_MUX_PLAN_PATH=
ASS_MKVMERGE_BIN=mkvmerge
```

### `/ass` 生产建议

- `ASS_MUX_ALLOW_CROSS_FS=0`：保持默认，避免跨文件系统替换风险
- `ASS_MUX_JOBS=2`：NAS / 小机先别调太高
- `ASS_MUX_DELETE_EXTERNAL_SUBS=0`：先保守，确认稳定后再考虑删源字幕

## 六、如果要启用 STRM 监控

```env
STRM_WATCH_ENABLED=1
STRM_FFPROBE_PATH=/usr/local/bin/ffprobe
STRM_WATCH_DIR=/data/strm/watch
STRM_DONE_DIR=/data/strm/done
STRM_FAILED_DIR=/data/strm/failed
STRM_STATE_DIR=/app/data/strm_state
STRM_PROCESSING_LEASE_SECONDS=1800
STRM_STATE_RETENTION_HOURS=168
STRM_MAX_WORKERS=3
STRM_TIMEOUT_S=60
STRM_MAX_RETRIES=2
STRM_RW_TIMEOUT_US=15000000
STRM_PROBESIZE=12M
STRM_ANALYZEDURATION=3000000
STRM_RECENT_EVENT_TTL=10
STRM_IDLE_SECONDS=120
STRM_MIN_FOLDER_AGE_SECONDS=300
STRM_ONLY_FIRST_LEVEL_DIR=1
```

### STRM 生产建议

- `STRM_IDLE_SECONDS=120`
- `STRM_MIN_FOLDER_AGE_SECONDS=300`

这两个值更适合“整季 / 多子目录 / 慢写入”场景，能降低过早 finalize 风险。

## 七、如果要启用 /rm_strm + Emby 刷新

```env
STRM_PRUNE_ENABLED=1
STRM_PRUNE_ROOTS=/volume2/strm/share/电影|/volume2/strm/share/电视剧|/volume2/strm/share/动漫
STRM_PRUNE_ALLOW_DELETE_FIRST_LEVEL=0
STRM_PRUNE_INCLUDE_ROOTS=0
STRM_PRUNE_NOTIFY_EMBY=1
STRM_PRUNE_EMBY_URL=http://172.17.0.1:8096
STRM_PRUNE_EMBY_API_KEY=
STRM_PRUNE_EMBY_UPDATE_TYPE=Deleted
STRM_PRUNE_HTTP_TIMEOUT=15
STRM_PRUNE_HTTP_RETRIES=3
STRM_PRUNE_HTTP_BACKOFF=2.0
```

## 八、一份更接近“可直接上线”的组合示例

```env
BOT_TOKEN=替换
HDHIVE_API_KEY=替换
bot_user_id=123456789
bot_chat_id=123456789
TMDB_API_KEY=
HDHIVE_PARSE_INCOMING_LINKS=1
AUTO_UNLOCK_THRESHOLD=0
HDHIVE_UNLOCK_RATE_LIMIT_PER_MINUTE=3
CHECKIN_CRON=
CHECKIN_TIMEZONE=Asia/Shanghai
CHECKIN_GAMBLE=0
MEDIA_BOT_LOG_TO_FILE=0
MEDIA_BOT_LOG_PATH=media_bot.log
MEDIA_BOT_DEBUG=false
MEDIA_BOT_HEALTHCHECK_STATE_PATH=/tmp/media_bot_health.json

STRM_WATCH_ENABLED=0
STRM_PRUNE_ENABLED=0
```

## 九、上线前至少确认这几项

- `bot_user_id` 已配置
- `bot_chat_id` 已配置
- `.env` 已通过 `python main.py --healthcheck`
- 如果启用 STRM：挂载路径真实存在
- 如果启用 `/ass`：宿主机目录挂载到容器内路径无误
- 如果启用 Emby：API Key 与 URL 有效
