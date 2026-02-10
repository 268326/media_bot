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

LOG_PATH = os.getenv("MEDIA_BOT_LOG_PATH", os.getenv("HDHIVE_LOG_PATH", "media_bot.log"))

# ==================== 首先配置日志 ====================
# 必须在导入其他模块之前配置，因为其他模块可能会使用 logging
logging.basicConfig(
    level=logging.DEBUG,
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
from config import BOT_TOKEN

# 导入处理器路由器
from handlers import router

# 导入会话管理器
from session_manager import session_manager
from checkin_scheduler import checkin_scheduler


async def main():
    """主程序"""
    logging.info("🚀 Media Bot 启动中...")
    
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

    # 启动会话管理器
    await session_manager.start()
    logging.info("✅ 会话管理器已启动")
    await checkin_scheduler.start(bot)
    
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
