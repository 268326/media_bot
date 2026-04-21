"""
Media Bot - 主程序入口
一个用于搜索和提取 HDHive 资源的 Telegram Bot
"""
import asyncio
import logging
import os
import glob
import sys
import nest_asyncio
from dotenv import load_dotenv

DOTENV_PATH = os.getenv("MEDIA_BOT_DOTENV_PATH", "/app/.env")
load_dotenv(dotenv_path=DOTENV_PATH, override=True)

LOG_PATH = os.getenv("MEDIA_BOT_LOG_PATH", os.getenv("HDHIVE_LOG_PATH", "media_bot.log"))
MEDIA_BOT_DEBUG = os.getenv("MEDIA_BOT_DEBUG", "false").strip().lower() in ("1", "true", "yes", "on")
LOG_LEVEL = logging.DEBUG if MEDIA_BOT_DEBUG else logging.INFO

log_dir = os.path.dirname(LOG_PATH)
if log_dir:
    os.makedirs(log_dir, exist_ok=True)

# ==================== 首先配置日志 ====================
# 必须在导入其他模块之前配置，因为其他模块可能会使用 logging
logging.basicConfig(
    level=LOG_LEVEL,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    handlers=[
        logging.FileHandler(LOG_PATH, encoding="utf-8"),
        logging.StreamHandler()
    ]
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


async def main():
    """主程序"""
    logging.info("🚀 Media Bot 启动中...")
    logging.info("🧩 配置来源: dotenv=%s, HDHIVE_API_KEY=%s", DOTENV_PATH, mask_secret(HDHIVE_API_KEY))
    
    # 启动时清除所有调试图片
    debug_files = glob.glob("debug_*.png") + glob.glob("error_*.png")
    if debug_files:
        for file in debug_files:
            try:
                os.remove(file)
                logging.info(f"🗑️ 启动时清除调试图片: {file}")
            except:
                pass
        logging.info(f"✅ 启动时清除了 {len(debug_files)} 个调试图片")
    
    # 创建 Bot 和 Dispatcher 实例
    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher()

    await strm_notifier.start(bot)

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
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n👋 再见！")
    except Exception as e:
        logging.error(f"❌ 启动失败: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
