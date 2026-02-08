"""
自动签到调度模块
根据环境变量中的 cron 表达式每日定时执行签到
"""
import html
import logging
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from checkin_service import daily_check_in
from config import CHECKIN_CRON, CHECKIN_TIMEZONE, ALLOWED_USER_ID


class CheckinScheduler:
    """自动签到调度器"""

    def __init__(self):
        self.scheduler: AsyncIOScheduler | None = None
        self.enabled = bool(CHECKIN_CRON)
        self.bot = None

    async def _notify_failure(self, result: dict):
        """自动签到失败时通过 Telegram 通知管理员"""
        if not self.bot:
            return
        if ALLOWED_USER_ID == 0:
            logging.warning("⚠️ 自动签到失败但未配置 ALLOWED_USER_ID，跳过通知")
            return

        text = (
            "❌ <b>自动签到失败</b>\n\n"
            f"原因: {html.escape(str(result.get('message', '未知错误')))}\n"
            f"签到前积分: <code>{result.get('before_points') if result.get('before_points') is not None else '未知'}</code>\n"
            f"签到后积分: <code>{result.get('after_points') if result.get('after_points') is not None else '未知'}</code>"
        )
        try:
            await self.bot.send_message(ALLOWED_USER_ID, text, parse_mode="HTML")
            logging.info("📨 自动签到失败通知已发送到 ALLOWED_USER_ID=%s", ALLOWED_USER_ID)
        except Exception as e:
            logging.error("❌ 自动签到失败通知发送失败: %s", e)

    async def _run_checkin_job(self):
        """定时任务：执行签到并记录日志"""
        logging.info("⏰ 触发自动签到任务")
        result = await daily_check_in()
        if result.get("success"):
            status = "已签到" if result.get("already_checked_in") else "签到成功"
            logging.info(
                "✅ 自动签到完成: %s | %s | before=%s after=%s",
                status,
                result.get("message"),
                result.get("before_points"),
                result.get("after_points"),
            )
        else:
            logging.error(
                "❌ 自动签到失败: %s | before=%s after=%s",
                result.get("message"),
                result.get("before_points"),
                result.get("after_points"),
            )
            await self._notify_failure(result)

    async def start(self, bot=None):
        """启动调度器"""
        self.bot = bot

        if not self.enabled:
            logging.info("ℹ️ 自动签到未启用（CHECKIN_CRON 为空）")
            return

        if self.scheduler:
            return

        try:
            timezone = ZoneInfo(CHECKIN_TIMEZONE)
            trigger = CronTrigger.from_crontab(CHECKIN_CRON, timezone=timezone)
        except Exception as e:
            logging.error(
                "❌ 自动签到配置无效，已禁用: cron=%s timezone=%s error=%s",
                CHECKIN_CRON,
                CHECKIN_TIMEZONE,
                e,
            )
            return

        self.scheduler = AsyncIOScheduler(timezone=timezone)
        self.scheduler.add_job(
            self._run_checkin_job,
            trigger=trigger,
            id="daily_checkin",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=300,
        )
        self.scheduler.start()
        logging.info("✅ 自动签到调度已启动: cron=%s timezone=%s", CHECKIN_CRON, CHECKIN_TIMEZONE)

    async def stop(self):
        """停止调度器"""
        if self.scheduler:
            self.scheduler.shutdown(wait=False)
            self.scheduler = None
            logging.info("🛑 自动签到调度已停止")


checkin_scheduler = CheckinScheduler()
