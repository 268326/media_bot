# Media Bot

一个基于 `aiogram + HDHive Open API` 的 Telegram Bot，用于 HDHive 资源检索、链接提取、解锁、每日签到、定时自动签到，以及可选的 `.strm` 文件监控重命名归档。

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
- `TGBOT_NOTIFY_CHAT_ID`：STRM 归档通知接收目标（用户/群组/频道 Chat ID）
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
- `TGBOT_NOTIFY_CHAT_ID` 配置后，STRM 在归档根目录文件或完成目录批次归档时会发送 Telegram 汇总通知
- `/rm_strm` 默认只预览；需在 Bot 返回消息下点击“确认删除”按钮才会实际删除
- STRM 监控依赖系统中的 `ffprobe` 和 `inotifywait`，Dockerfile 已自动安装

## STRM 监控说明

启用后，后台会递归监控 `STRM_WATCH_DIR` 下的 `.strm` 文件：

- 监听 `close_write/moved_to`
- 首次扫描或增量事件到达时，为每个一级目录生成并更新一份 manifest 批次计划
- 读取 `.strm` 内 URL
- 用 `ffprobe` 探测媒体信息
- 清理旧技术标签并重命名
- 每完成一个 `.strm`，立即把对应 manifest 条目标记为 `done / already_ok / failed`
- 失败文件移动到 `STRM_FAILED_DIR`（保留原目录层级）
- 一级目录空闲且达到最小存活时间后，会先重新扫描目录并与 manifest 对账：
  - 若发现新增 `.strm`，会加入计划并继续处理
  - 若计划里仍有 `pending / processing / missing`，不会归档
  - 仅当计划闭环后，才整体移动到 `STRM_DONE_DIR`
- 当 `STRM_ONLY_FIRST_LEVEL_DIR=0` 时，根目录下直放的 `.strm` 成功后也会直接移动到 `STRM_DONE_DIR`

已内置以下稳健性优化：

- `/rm_strm` 采用 Telegram 按钮二次确认，避免误删
- 同一路径去重，避免重复提交
- manifest 持久化到 `STRM_STATE_DIR`，支持容器重启后恢复批次状态
- `processing` 条目带租约时间，异常退出后会自动回退到 `pending`
- 已完成/失败的 manifest 会按 `STRM_STATE_RETENTION_HOURS` 自动清理，避免状态目录长期膨胀
- 批量导入推荐参数：`STRM_IDLE_SECONDS=120`、`STRM_MIN_FOLDER_AGE_SECONDS=300`、`STRM_ONLY_FIRST_LEVEL_DIR=1`
- 失败移动保留 `.strm` 扩展名
- `inotifywait` 异常退出自动重启
- 目录完成判定不再依赖最终全量 `ffprobe` 复查，而是依赖 manifest 闭环 + 目录重扫对账
- 默认以一级子目录为批次移动到 DONE；当 `STRM_ONLY_FIRST_LEVEL_DIR=0` 时，根目录下直放的 `.strm` 也会被处理，但不会参与整目录移动

## 本地运行（Python）

```bash
cd /path/to/media_bot
cp .env.example .env
pip install -r requirements.txt
python main.py
```

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

## GitHub Actions 多架构镜像构建

仓库包含工作流：`.github/workflows/docker-image.yml`

- 触发：push 到 `main/master`、tag `v*`、手动触发
- 构建平台：`linux/amd64` + `linux/arm64`
- 推送仓库：`ghcr.io/${{ github.repository }}`

### 使用步骤

1. 将本项目推到 GitHub
2. 在仓库 `Settings -> Actions -> General` 确保 workflow 权限允许写入 packages
3. push 到默认分支后，Actions 会自动构建并推送：
   - `latest`（默认分支）
   - 分支/tag/sha 标签

## 定时签到示例

每天 08:30 自动签到：

```env
CHECKIN_CRON=30 8 * * *
CHECKIN_TIMEZONE=Asia/Shanghai
```

签到失败会自动通知 `ALLOWED_USER_ID`。

## Docker 本地构建调试（可选）

如需本地联调，编辑 `docker-compose.yml`：
- 注释 `image: ghcr.io/268326/media_bot:latest`
- 取消注释 `build: .`

```bash
docker compose up -d --build
```
