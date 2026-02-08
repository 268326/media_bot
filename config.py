"""
配置管理模块
管理所有环境变量、常量配置和应用初始化
"""
import os
import sys
from dotenv import load_dotenv

# 加载环境变量
load_dotenv()

# ==================== 超时配置常量 ====================
BROWSER_TIMEOUT = 30000
LOGIN_TIMEOUT = 20000
SEARCH_TIMEOUT = 10000
SHORT_WAIT = 1000
MEDIUM_WAIT = 1500
LONG_WAIT = 3000

# ==================== 环境变量配置 ====================
# Telegram Bot
BOT_TOKEN = os.getenv("BOT_TOKEN")

# HDHive 账号
HDHIVE_USER = os.getenv("HDHIVE_USER")
HDHIVE_PASS = os.getenv("HDHIVE_PASS")
HDHIVE_USER_ID = os.getenv("HDHIVE_USER_ID", "")

# TMDB API (可选)
TMDB_API_KEY = os.getenv("TMDB_API_KEY", "")

# 权限控制
ALLOWED_USER_ID = int(os.getenv("ALLOWED_USER_ID", "0"))
TARGET_GROUP_ID = int(os.getenv("TARGET_GROUP_ID", "0"))  # 已废弃，保留兼容

# 自动解锁配置
AUTO_UNLOCK_THRESHOLD = int(os.getenv("AUTO_UNLOCK_THRESHOLD", "0"))  # 0=禁用自动解锁，>0=自动解锁的积分阈值

# 自动签到配置
CHECKIN_CRON = os.getenv("CHECKIN_CRON", "").strip()  # 5段cron表达式，留空则禁用
CHECKIN_TIMEZONE = os.getenv("CHECKIN_TIMEZONE", "Asia/Shanghai").strip() or "Asia/Shanghai"

# Symedia 集成 (可选)
SA_URL = os.getenv("SA_URL", "")
SA_PARENT_ID = os.getenv("SA_PARENT_ID", "")
SA_AUTO_ADD_DELAY = int(os.getenv("SA_AUTO_ADD_DELAY", "60"))

# Cookie 文件路径
COOKIE_FILE = "auth.json"

# 日志文件路径（兼容旧变量 HDHIVE_LOG_PATH）
LOG_PATH = os.getenv("MEDIA_BOT_LOG_PATH", os.getenv("HDHIVE_LOG_PATH", "media_bot.log"))

# ==================== 浏览器配置 ====================
BROWSER_ARGS = [
    "--no-sandbox",
    "--disable-setuid-sandbox",
    "--disable-dev-shm-usage",
    "--disable-blink-features=AutomationControlled"
]

VIEWPORT_SIZE = {'width': 1920, 'height': 1080}

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

# ==================== 配置验证 ====================
def validate_config():
    """验证必需的配置是否存在"""
    errors = []
    warnings = []
    
    # 必需配置
    if not BOT_TOKEN:
        errors.append("未配置 BOT_TOKEN")
    if not HDHIVE_USER:
        errors.append("未配置 HDHIVE_USER")
    if not HDHIVE_PASS:
        errors.append("未配置 HDHIVE_PASS")
    
    # 可选配置警告
    if ALLOWED_USER_ID == 0:
        warnings.append("未配置 ALLOWED_USER_ID，任何人都可以使用机器人！")
    
    if not SA_URL or not SA_PARENT_ID:
        warnings.append("未配置 SA_URL 或 SA_PARENT_ID，无法自动添加到Symedia。")
    
    if not TMDB_API_KEY:
        warnings.append("未配置 TMDB_API_KEY，将无法使用TMDB API功能。")
    
    if AUTO_UNLOCK_THRESHOLD > 0:
        warnings.append(f"已启用自动解锁: {AUTO_UNLOCK_THRESHOLD} 积分及以下的资源将自动解锁。")
    
    if CHECKIN_CRON:
        warnings.append(
            f"已启用自动签到: cron={CHECKIN_CRON}, timezone={CHECKIN_TIMEZONE}, notify_chat_id={ALLOWED_USER_ID or '未配置'}"
        )
    
    # 输出错误和警告
    if errors:
        print("❌ 配置错误:")
        for error in errors:
            print(f"   - {error}")
        print("\n请检查 .env 文件。")
        sys.exit(1)
    
    if warnings:
        print("⚠️ 配置警告:")
        for warning in warnings:
            print(f"   - {warning}")
        print()
    
    print("✅ 配置加载完成\n")

# 自动验证配置
validate_config()
