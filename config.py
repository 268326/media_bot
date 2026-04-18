"""
配置管理模块
管理所有环境变量、常量配置和应用初始化
"""
import os
import sys
from dotenv import load_dotenv

from strm_config import StrmSettings
from strm_prune import load_settings_from_env

# 加载环境变量
DOTENV_PATH = os.getenv("MEDIA_BOT_DOTENV_PATH", "/app/.env")
load_dotenv(dotenv_path=DOTENV_PATH, override=True)

# ==================== 环境变量配置 ====================
# Telegram Bot
BOT_TOKEN = os.getenv("BOT_TOKEN")

# HDHive Open API
HDHIVE_API_KEY = os.getenv("HDHIVE_API_KEY", "").strip()
HDHIVE_BASE_URL = os.getenv("HDHIVE_BASE_URL", "https://hdhive.com").strip() or "https://hdhive.com"
HDHIVE_OPEN_API_BASE_URL = (
    os.getenv("HDHIVE_OPEN_API_BASE_URL", f"{HDHIVE_BASE_URL}/api/open").strip()
    or f"{HDHIVE_BASE_URL}/api/open"
)

# 是否解析收到的 HDHive 链接（直接发送链接时自动处理；/hdt /hdm 等命令不受影响）
HDHIVE_PARSE_INCOMING_LINKS = os.getenv("HDHIVE_PARSE_INCOMING_LINKS", "1").strip().lower() in (
    "1",
    "true",
    "yes",
    "on",
)

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
CHECKIN_GAMBLE = os.getenv("CHECKIN_GAMBLE", "0").strip().lower() in ("1", "true", "yes", "on")

# Symedia 集成 (可选)
SA_URL = os.getenv("SA_URL", "")
SA_PARENT_ID = os.getenv("SA_PARENT_ID", "")
SA_AUTO_ADD_DELAY = int(os.getenv("SA_AUTO_ADD_DELAY", "60"))
SA_ENABLE_115_PUSH = os.getenv("SA_ENABLE_115_PUSH", "1").strip().lower() in ("1", "true", "yes", "on")
SA_TOKEN = os.getenv("SA_TOKEN", "symedia").strip() or "symedia"

# 日志文件路径（兼容旧变量 HDHIVE_LOG_PATH）
LOG_PATH = os.getenv("MEDIA_BOT_LOG_PATH", os.getenv("HDHIVE_LOG_PATH", "media_bot.log"))

# STRM Telegram 通知目标（可选）
# 支持用户、群组、频道 Chat ID；留空则不发送 STRM 通知
TGBOT_NOTIFY_CHAT_ID = os.getenv("TGBOT_NOTIFY_CHAT_ID", "").strip()

# STRM 监控配置
STRM_WATCH_ENABLED = os.getenv("STRM_WATCH_ENABLED", "0").strip().lower() in ("1", "true", "yes", "on")
STRM_FFPROBE_PATH = os.getenv("STRM_FFPROBE_PATH", "/usr/local/bin/ffprobe").strip() or "/usr/local/bin/ffprobe"
STRM_WATCH_DIR = os.getenv("STRM_WATCH_DIR", "").strip()
STRM_DONE_DIR = os.getenv("STRM_DONE_DIR", "").strip()
STRM_FAILED_DIR = os.getenv("STRM_FAILED_DIR", "").strip()
STRM_STATE_DIR = os.getenv("STRM_STATE_DIR", "/app/data/strm_state").strip() or "/app/data/strm_state"
STRM_PROCESSING_LEASE_SECONDS = int(os.getenv("STRM_PROCESSING_LEASE_SECONDS", "1800"))
STRM_STATE_RETENTION_HOURS = int(os.getenv("STRM_STATE_RETENTION_HOURS", "168"))
STRM_MAX_WORKERS = int(os.getenv("STRM_MAX_WORKERS", "3"))
STRM_TIMEOUT_S = int(os.getenv("STRM_TIMEOUT_S", "60"))
STRM_MAX_RETRIES = int(os.getenv("STRM_MAX_RETRIES", "2"))
STRM_RW_TIMEOUT_US = int(os.getenv("STRM_RW_TIMEOUT_US", "15000000"))
STRM_PROBESIZE = os.getenv("STRM_PROBESIZE", "12M").strip() or "12M"
STRM_ANALYZEDURATION = os.getenv("STRM_ANALYZEDURATION", "3000000").strip() or "3000000"
STRM_RECENT_EVENT_TTL = int(os.getenv("STRM_RECENT_EVENT_TTL", "10"))
STRM_IDLE_SECONDS = int(os.getenv("STRM_IDLE_SECONDS", "30"))
STRM_MIN_FOLDER_AGE_SECONDS = int(os.getenv("STRM_MIN_FOLDER_AGE_SECONDS", "60"))
STRM_ONLY_FIRST_LEVEL_DIR = os.getenv("STRM_ONLY_FIRST_LEVEL_DIR", "1").strip().lower() in ("1", "true", "yes", "on")

STRM_SETTINGS = StrmSettings(
    enabled=STRM_WATCH_ENABLED,
    ffprobe_path=STRM_FFPROBE_PATH,
    watch_dir=STRM_WATCH_DIR,
    done_dir=STRM_DONE_DIR,
    failed_dir=STRM_FAILED_DIR,
    state_dir=STRM_STATE_DIR,
    processing_lease_seconds=STRM_PROCESSING_LEASE_SECONDS,
    state_retention_hours=STRM_STATE_RETENTION_HOURS,
    max_workers=STRM_MAX_WORKERS,
    timeout_s=STRM_TIMEOUT_S,
    max_retries=STRM_MAX_RETRIES,
    rw_timeout_us=STRM_RW_TIMEOUT_US,
    probesize=STRM_PROBESIZE,
    analyzeduration=STRM_ANALYZEDURATION,
    recent_event_ttl=STRM_RECENT_EVENT_TTL,
    idle_seconds=STRM_IDLE_SECONDS,
    min_folder_age_seconds=STRM_MIN_FOLDER_AGE_SECONDS,
    only_first_level_dir=STRM_ONLY_FIRST_LEVEL_DIR,
)

STRM_PRUNE_SETTINGS = load_settings_from_env()


def mask_secret(value: str, *, prefix: int = 6) -> str:
    """返回脱敏后的敏感信息，仅展示前几位用于排查配置。"""
    text = str(value or "").strip()
    if not text:
        return "(empty)"
    if len(text) <= prefix:
        return text
    return f"{text[:prefix]}..."

# ==================== 配置验证 ====================
def validate_config():
    """验证必需的配置是否存在"""
    errors = []
    warnings = []
    
    # 必需配置
    if not BOT_TOKEN:
        errors.append("未配置 BOT_TOKEN")
    if not HDHIVE_API_KEY:
        errors.append("未配置 HDHIVE_API_KEY")
    
    # 可选配置警告
    if ALLOWED_USER_ID == 0:
        warnings.append("未配置 ALLOWED_USER_ID，任何人都可以使用机器人！")
    
    if not SA_URL or not SA_PARENT_ID:
        warnings.append("未配置 SA_URL 或 SA_PARENT_ID，无法自动添加到Symedia。")
    elif not SA_ENABLE_115_PUSH:
        warnings.append("已禁用 SA_ENABLE_115_PUSH，115链接不会推送到Symedia。")
    
    if not TMDB_API_KEY:
        warnings.append("未配置 TMDB_API_KEY，/hdt 和 /hdm 关键词搜索不可用，但直链解析仍可使用。")
    
    if AUTO_UNLOCK_THRESHOLD > 0:
        warnings.append(f"已启用自动解锁: {AUTO_UNLOCK_THRESHOLD} 积分及以下的资源将自动解锁。")
    
    if CHECKIN_CRON:
        warnings.append(
            f"已启用自动签到: cron={CHECKIN_CRON}, timezone={CHECKIN_TIMEZONE}, notify_chat_id={ALLOWED_USER_ID or '未配置'}"
        )
    if STRM_WATCH_ENABLED:
        missing = []
        if not STRM_WATCH_DIR:
            missing.append("STRM_WATCH_DIR")
        if not STRM_DONE_DIR:
            missing.append("STRM_DONE_DIR")
        if not STRM_FAILED_DIR:
            missing.append("STRM_FAILED_DIR")
        if not STRM_STATE_DIR:
            missing.append("STRM_STATE_DIR")
        if missing:
            errors.append(f"已启用 STRM_WATCH_ENABLED，但缺少配置: {', '.join(missing)}")
        else:
            warnings.append(
                f"已启用 STRM 监控: watch={STRM_WATCH_DIR}, done={STRM_DONE_DIR}, failed={STRM_FAILED_DIR}, state={STRM_STATE_DIR}"
            )
            if TGBOT_NOTIFY_CHAT_ID:
                warnings.append(f"已启用 STRM Telegram 通知: chat_id={TGBOT_NOTIFY_CHAT_ID}")

    if STRM_PRUNE_SETTINGS.enabled:
        warnings.append(
            "已启用 STRM 空目录清理: "
            f"roots={list(STRM_PRUNE_SETTINGS.roots)}, notify_emby={STRM_PRUNE_SETTINGS.notify_emby}, "
            f"allow_delete_first_level={STRM_PRUNE_SETTINGS.allow_delete_first_level}"
        )
        if STRM_PRUNE_SETTINGS.notify_emby and not STRM_PRUNE_SETTINGS.emby_api_key:
            warnings.append("STRM 空目录清理已开启 Emby 通知，但未配置 STRM_PRUNE_EMBY_API_KEY/EMBY_API_KEY，运行时将跳过通知。")
    
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
