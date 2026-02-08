# Media Bot

一个基于 `aiogram + Playwright` 的 Telegram Bot，用于 HDHive 资源检索、链接提取、解锁、每日签到和定时自动签到。

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

## 环境变量

必填：

- `BOT_TOKEN`
- `HDHIVE_USER`
- `HDHIVE_PASS`

常用可选：

- `ALLOWED_USER_ID`：机器人可用用户，同时也是自动签到失败通知接收人
- `AUTO_UNLOCK_THRESHOLD`
- `CHECKIN_CRON`：5 段 cron，留空禁用自动签到
- `CHECKIN_TIMEZONE`：默认 `Asia/Shanghai`
- `TMDB_API_KEY`
- `SA_URL`
- `SA_PARENT_ID`
- `SA_AUTO_ADD_DELAY`

部署镜像可选：

- `MEDIA_BOT_IMAGE`：Compose 使用的镜像地址（默认 `media_bot:latest`）

## 本地运行（Python）

```bash
cd /path/to/media_bot
cp .env.example .env
pip install -r requirements.txt
playwright install chromium
python main.py
```

## Docker 运行（远程镜像模式）

1. 设置 `.env` 中的 `MEDIA_BOT_IMAGE` 为你的远程镜像（例如 `ghcr.io/<owner>/<repo>:latest`）
2. 启动：

```bash
cd /path/to/media_bot
docker compose pull
docker compose up -d
```

## Docker 本地构建调试模式

使用 `docker-compose.local.yml` 覆盖为本地 build：

```bash
cd /path/to/media_bot
docker compose -f docker-compose.yml -f docker-compose.local.yml up -d --build
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
