"""
Media Bot - 主程序入口
一个用于搜索和提取 HDHive 资源的 Telegram Bot
"""
import asyncio
import logging
import os
import glob
import sys
import json
import time
import nest_asyncio
from dotenv import load_dotenv


def _resolve_dotenv_path() -> str:
    explicit = os.getenv("MEDIA_BOT_DOTENV_PATH", "").strip()
    if explicit:
        return explicit
    docker_path = "/app/.env"
    if os.path.exists(docker_path):
        return docker_path
    local_path = os.path.abspath(".env")
    if os.path.exists(local_path):
        return local_path
    return docker_path


DOTENV_PATH = _resolve_dotenv_path()
load_dotenv(dotenv_path=DOTENV_PATH, override=True)

LOG_PATH = os.getenv("MEDIA_BOT_LOG_PATH", os.getenv("HDHIVE_LOG_PATH", "media_bot.log"))
if str(LOG_PATH).strip() in ("", "0", "false", "False", "none", "None"):
    LOG_PATH = "media_bot.log"
LOG_TO_FILE = os.getenv("MEDIA_BOT_LOG_TO_FILE", "0").strip().lower() in ("1", "true", "yes", "on")
MEDIA_BOT_DEBUG = os.getenv("MEDIA_BOT_DEBUG", "false").strip().lower() in ("1", "true", "yes", "on")
LOG_LEVEL = logging.DEBUG if MEDIA_BOT_DEBUG else logging.INFO
HEALTHCHECK_STATE_PATH = os.getenv("MEDIA_BOT_HEALTHCHECK_STATE_PATH", "/tmp/media_bot_health.json")

if LOG_TO_FILE:
    log_dir = os.path.dirname(LOG_PATH)
    if log_dir:
        os.makedirs(log_dir, exist_ok=True)

handlers = [logging.StreamHandler()]
if LOG_TO_FILE and LOG_PATH:
    handlers.insert(0, logging.FileHandler(LOG_PATH, encoding="utf-8"))

# ==================== 首先配置日志 ====================
# 必须在导入其他模块之前配置，因为其他模块可能会使用 logging
logging.basicConfig(
    level=LOG_LEVEL,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    handlers=handlers,
)

# 应用 nest_asyncio（支持嵌套事件循环）
nest_asyncio.apply()

from aiogram import Bot, Dispatcher

# 导入配置（这会自动验证配置）
from config import BOT_TOKEN, DOTENV_PATH, HDHIVE_API_KEY, mask_secret

# 导入处理器路由器
from handlers import router

# 导入会话管理器
from session_manager import session_manager
from checkin_scheduler import checkin_scheduler
from strm_service import strm_service
from strm_notifier import strm_notifier
from hdhive_unlock_service import hdhive_unlock_service


def health_snapshot() -> dict[str, object]:
    return {
        "ok": True,
        "strm_enabled": bool(strm_service.settings.enabled),
        "strm_started": bool(strm_service.started),
        "strm_running": bool(strm_service.status().get("running")),
        "unlock_queue_started": bool(hdhive_unlock_service.started),
        "unlock_queue_workers": len(hdhive_unlock_service.worker_tasks),
        "checkin_enabled": bool(checkin_scheduler.enabled),
        "checkin_scheduler_started": bool(checkin_scheduler.scheduler is not None),
    }


def write_health_snapshot(snapshot: dict[str, object]) -> None:
    try:
        parent = os.path.dirname(HEALTHCHECK_STATE_PATH)
        if parent:
            os.makedirs(parent, exist_ok=True)
        payload = dict(snapshot)
        payload["pid"] = os.getpid()
        payload["ts"] = int(time.time())
        with open(HEALTHCHECK_STATE_PATH, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, separators=(",", ":"))
    except Exception as exc:
        logging.debug("写入健康快照失败: %s", exc)


async def health_heartbeat(stop_event: asyncio.Event) -> None:
    while not stop_event.is_set():
        write_health_snapshot(health_snapshot())
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=30)
        except asyncio.TimeoutError:
            continue


async def main():
    """主程序"""
    logging.info("🚀 Media Bot 启动中...")
    logging.info("🧩 配置来源: dotenv=%s, HDHIVE_API_KEY=%s, log_to_file=%s", DOTENV_PATH, mask_secret(HDHIVE_API_KEY), LOG_TO_FILE)
    
    # 启动时清除所有调试图片
    debug_files = glob.glob("debug_*.png") + glob.glob("error_*.png")
    if debug_files:
        for file in debug_files:
            try:
                os.remove(file)
                logging.info(f"🗑️ 启动时清除调试图片: {file}")
            except Exception as exc:
                logging.debug("启动时清理调试图片失败: file=%s error=%s", file, exc)
        logging.info(f"✅ 启动时清除了 {len(debug_files)} 个调试图片")
    
    # 创建 Bot 和 Dispatcher 实例
    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher()

    await strm_notifier.start(bot)

    heartbeat_stop = asyncio.Event()
    heartbeat_task = asyncio.create_task(health_heartbeat(heartbeat_stop))
    write_health_snapshot(health_snapshot())

    # 启动 HDHive 解锁队列服务
    await hdhive_unlock_service.start()

    # 启动会话管理器
    await session_manager.start()
    logging.info("✅ 会话管理器已启动")
    await checkin_scheduler.start(bot)
    await strm_service.start()
    
    # 注册路由器
    dp.include_router(router)
    
    try:
        await dp.start_polling(bot)
    except Exception as e:
        logging.error(f"❌ Bot 运行出错: {e}")
        raise
    finally:
        heartbeat_stop.set()
        heartbeat_task.cancel()
        try:
            await heartbeat_task
        except asyncio.CancelledError:
            pass

        # 停止自动签到调度
        await checkin_scheduler.stop()

        # 停止 STRM 监控服务
        await strm_service.stop()

        # 停止 STRM 通知器
        await strm_notifier.stop()

        # 停止 HDHive 解锁队列服务
        await hdhive_unlock_service.stop()

        # 停止会话管理器
        await session_manager.stop()
        logging.info("✅ 会话管理器已停止")


if __name__ == "__main__":
    try:
        if "--healthcheck" in sys.argv:
            snapshot = health_snapshot()
            if os.path.exists(HEALTHCHECK_STATE_PATH):
                try:
                    with open(HEALTHCHECK_STATE_PATH, "r", encoding="utf-8") as fh:
                        persisted = json.load(fh)
                    ts = int(persisted.get("ts") or 0)
                    age = max(0, int(time.time()) - ts)
                    snapshot["heartbeat_age_seconds"] = age
                    snapshot["heartbeat_file_present"] = True
                    if age > 120:
                        snapshot["ok"] = False
                        snapshot["reason"] = f"heartbeat stale: {age}s"
                except Exception as exc:
                    snapshot["ok"] = False
                    snapshot["reason"] = f"heartbeat read failed: {exc}"
            else:
                snapshot["heartbeat_file_present"] = False
            if not snapshot.get("ok"):
                logging.error("❌ 健康检查失败: %s", snapshot)
                sys.exit(1)
            logging.info("✅ 健康检查通过: %s", snapshot)
            sys.exit(0)
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n👋 再见！")
    except Exception as e:
        logging.error(f"❌ 启动失败: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
