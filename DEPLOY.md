# 部署与维护指南

本项目为 Media Bot（HDHive Open API 方式），通过 `HDHIVE_API_KEY` 访问官方接口。

## 📦 环境准备

本地运行需要：
- Python 3.11+
- pip

Docker 运行可直接使用 `docker-compose.yml`。

## 🔧 配置环境变量

复制示例配置：
```bash
cd /path/to/media_bot
cp .env.example .env
```

编辑 `.env`，填写必需项：
```env
BOT_TOKEN=your_bot_token_here
HDHIVE_API_KEY=your_open_api_key_here
```

可选项（按需填写）：
```env
TMDB_API_KEY=
ALLOWED_USER_ID=0
AUTO_UNLOCK_THRESHOLD=0
HDHIVE_PARSE_INCOMING_LINKS=1
CHECKIN_CRON=
CHECKIN_TIMEZONE=Asia/Shanghai
SA_URL=
SA_PARENT_ID=
SA_TOKEN=symedia
SA_ENABLE_115_PUSH=1
STRM_PRUNE_ENABLED=0
STRM_PRUNE_ROOTS=/volume2/strm/share/电影|/volume2/strm/share/电视剧|/volume2/strm/share/动漫
STRM_PRUNE_NOTIFY_EMBY=1
STRM_PRUNE_EMBY_URL=http://172.17.0.1:8096
STRM_PRUNE_EMBY_API_KEY=
```

自动签到示例：
```env
CHECKIN_CRON=30 8 * * *
CHECKIN_TIMEZONE=Asia/Shanghai
```

## ▶️ 本地运行

```bash
cd /path/to/media_bot
pip install -r requirements.txt
python main.py
```

后台运行（日志自动写入 `media_bot.log`）：
```bash
nohup python main.py >/dev/null 2>&1 &
```

## 🐳 Docker 运行

默认 `docker-compose.yml` 使用远程镜像 `ghcr.io/268326/media_bot:latest`。

```bash
cd /path/to/media_bot
docker compose pull
docker compose up -d
```

本地调试构建时：编辑 `docker-compose.yml`，注释 `image` 并启用 `build: .`，再执行：

```bash
docker compose up -d --build
```

查看日志：
```bash
docker logs -f media_bot
```

## ✅ 功能验证

1. 向 Bot 发送资源链接（`https://hdhive.com/resource/<id>`）。
2. 如果需要解锁，按提示操作。
3. 成功会返回 115 链接和提取码。
4. 如需清理空 STRM 目录，发送 `/rm_strm`，先查看预览结果，再点击消息下方“确认删除”按钮执行实际删除。

## 🆘 常见问题

### 1) 启动时报配置错误

检查 `.env` 中必需项是否完整：
```bash
cat .env | grep -E "BOT_TOKEN|HDHIVE_API_KEY"
```

### 2) API Key 无效或权限不足

检查 `HDHIVE_API_KEY` 是否正确，且绑定了对应的 HDHive 用户。

如果 `/points`、`/checkin` 或自动签到不可用，请确认该 API Key 关联的是 Premium 用户。

## 🔄 更新维护

### 更新代码

替换代码后重启进程：
- 本地：停止旧进程，重新运行 `python main.py`
- Docker：`docker compose up -d --build`

### 变更 API Key

更新 `.env` 后重启程序。

### 自动解锁策略

`AUTO_UNLOCK_THRESHOLD > 0` 会自动解锁低于阈值的资源，谨慎开启。
