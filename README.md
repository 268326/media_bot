# Media Bot

一个基于 `aiogram + HDHive Open API` 的 Telegram Bot，用于 HDHive 资源检索、链接提取、解锁、每日签到、定时自动签到，以及可选的 `.strm` 文件监控重命名归档。

## 功能

- `/hdt` 搜索剧集资源
- `/hdm` 搜索电影资源
- 直接发送 HDHive 链接自动解析
- 积分解锁（支持阈值自动解锁）
- `/points` 查询积分
- `/checkin` 手动每日签到
- `CHECKIN_CRON` 定时自动签到
- 自动签到失败时通知 `ALLOWED_USER_ID`
- `/danmu` 下载 B 站弹幕 XML
- `/strm_status` 查看 STRM 监控服务状态
- `/strm_scan` 手动触发一次 STRM 存量重扫
- 可选启用 STRM 监控：实时探测、重命名、失败归档、整目录移动到 DONE

## 环境变量

必填：

- `BOT_TOKEN`
- `HDHIVE_API_KEY`

常用可选：

- `ALLOWED_USER_ID`：机器人可用用户，同时也是自动签到失败通知接收人
- `AUTO_UNLOCK_THRESHOLD`
- `CHECKIN_CRON`：5 段 cron，留空禁用自动签到
- `CHECKIN_TIMEZONE`：默认 `Asia/Shanghai`
- `TMDB_API_KEY`：关键词搜索推荐必填
- `SA_URL`
- `SA_PARENT_ID`
- `SA_AUTO_ADD_DELAY`
- `SA_TOKEN`
- `SA_ENABLE_115_PUSH`
- `STRM_WATCH_ENABLED`：是否启用 STRM 监控
- `STRM_WATCH_DIR` / `STRM_DONE_DIR` / `STRM_FAILED_DIR`
- `STRM_FFPROBE_PATH`：默认 `/usr/local/bin/ffprobe`

说明：

- 关键词搜索依赖 TMDB API，因此 `/hdt` 和 `/hdm` 建议同时配置 `TMDB_API_KEY`
- `/points`、`/checkin` 和自动签到依赖 HDHive Premium 权限对应的 Open API
- STRM 监控依赖系统中的 `ffprobe` 和 `inotifywait`，Dockerfile 已自动安装

## STRM 监控说明

启用后，后台会递归监控 `STRM_WATCH_DIR` 下的 `.strm` 文件：

- 监听 `close_write/moved_to`
- 读取 `.strm` 内 URL
- 用 `ffprobe` 探测媒体信息
- 清理旧技术标签并重命名
- 失败文件移动到 `STRM_FAILED_DIR`（保留原目录层级）
- 一级目录空闲且达到最小存活时间后，整体移动到 `STRM_DONE_DIR`

已内置以下稳健性优化：

- 同一路径去重，避免重复提交
- 失败移动保留 `.strm` 扩展名
- `inotifywait` 异常退出自动重启
- 目录完成判定加入最小存活时间保护
- 默认保留 `SDR` 标记

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
