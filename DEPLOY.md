# 部署与维护指南

本项目为 Media Bot（浏览器抓取方式），首次运行会自动登录并保存 Cookie 到 `auth.json`。

## 📦 环境准备

本地运行需要：
- Python 3.11+
- pip
- Playwright（Chromium）

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
HDHIVE_USER=your_username
HDHIVE_PASS=your_password
```

可选项（按需填写）：
```env
HDHIVE_USER_ID=
TMDB_API_KEY=
ALLOWED_USER_ID=0
AUTO_UNLOCK_THRESHOLD=0
CHECKIN_CRON=
CHECKIN_TIMEZONE=Asia/Shanghai
SA_URL=
SA_PARENT_ID=
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
playwright install chromium
python main.py
```

后台运行（日志自动写入 `media_bot.log`）：
```bash
nohup python main.py >/dev/null 2>&1 &
```

## 🐳 Docker 运行

```bash
cd /path/to/media_bot
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

## 🆘 常见问题

### 1) 启动时报配置错误

检查 `.env` 中必需项是否完整：
```bash
cat .env | grep -E "BOT_TOKEN|HDHIVE_USER|HDHIVE_PASS"
```

### 2) 登录失败或 Cookie 失效

删除旧 Cookie，让程序重新登录：
```bash
rm -f auth.json
```

### 3) Playwright 报错

确认已安装浏览器：
```bash
playwright install chromium
```

## 🔄 更新维护

### 更新代码

替换代码后重启进程：
- 本地：停止旧进程，重新运行 `python main.py`
- Docker：`docker compose up -d --build`

### 变更账号或密码

更新 `.env` 后删除旧 `auth.json`，再重启程序。

### 自动解锁策略

`AUTO_UNLOCK_THRESHOLD > 0` 会自动解锁低于阈值的资源，谨慎开启。
