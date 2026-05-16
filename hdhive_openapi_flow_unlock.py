from __future__ import annotations

import logging
from aiogram.types import Message

from formatter import format_error_message, format_unlock_confirmation
from hdhive_openapi_client import get_user_points, unlock_and_fetch, unlock_resource
from hdhive_openapi_unlock_service import UnlockQueueNotice
from hdhive_openapi_state import HDHiveOpenAPIState

logger = logging.getLogger(__name__)


class HDHiveOpenAPIUnlockFlow:
    def __init__(self, state: HDHiveOpenAPIState):
        self.state = state
        self.link_extracted_handler = None
        self.auto_unlock_threshold = 0

    @staticmethod
    def format_duration_compact(seconds: float | int | None) -> str:
        total_seconds = max(0, int(float(seconds or 0)))
        minutes, secs = divmod(total_seconds, 60)
        hours, mins = divmod(minutes, 60)
        if hours > 0:
            return f"{hours}h {mins}m {secs}s"
        if mins > 0:
            return f"{mins}m {secs}s"
        return f"{secs}s"

    async def notify_auto_unlock_failed(self, target_msg: Message, fallback_msg: Message):
        try:
            await target_msg.edit_text("❌ 自动解锁失败，请稍后重试", parse_mode="HTML")
        except Exception:
            await fallback_msg.reply("❌ 自动解锁失败，请稍后重试", parse_mode="HTML")

    async def update_unlock_queue_notice(
        self,
        wait_msg: Message,
        notice: UnlockQueueNotice,
        *,
        auto_unlock: bool,
        title: str | None = None,
        tip: str | None = None,
    ):
        mode_text = title or ("自动解锁排队中" if auto_unlock else "解锁排队中")
        footer_tip = tip or "💡 当前请求已进入官方 OpenAPI 串行处理队列"
        ahead_text = (
            f"前面还有 <code>{notice.ahead_count}</code> 个请求"
            if notice.ahead_count > 0
            else "你已在队列最前面，等待下一个可用速率窗口"
        )
        queued_text = self.format_duration_compact(notice.queued_seconds)
        wait_text = self.format_duration_compact(notice.wait_seconds)
        try:
            await wait_msg.edit_text(
                f"⏳ <b>{mode_text}...</b>\n\n"
                f"🆔 <code>{notice.resource_id}</code>\n"
                f"📍 当前队列位置: <code>{notice.queue_position}</code>\n"
                f"👥 {ahead_text}\n"
                f"⌛ 已累计等待: <code>{queued_text}</code>\n"
                f"⏱️ 当前退避策略: <code>{wait_text}</code>\n"
                f"🚦 限制来源: <code>官方 429/Retry-After</code>\n\n"
                f"{footer_tip}",
                parse_mode="HTML",
            )
        except Exception as exc:
            logger.debug("更新解锁排队提示失败: %s", exc)

    def build_unlock_wait_callback(
        self,
        wait_msg: Message,
        *,
        auto_unlock: bool,
        title: str | None = None,
        tip: str | None = None,
    ):
        async def _wait_callback(notice: UnlockQueueNotice):
            await self.update_unlock_queue_notice(
                wait_msg,
                notice,
                auto_unlock=auto_unlock,
                title=title,
                tip=tip,
            )
        return _wait_callback

    async def perform_unlock_and_handle_result(
        self,
        *,
        wait_msg: Message,
        fallback_msg: Message,
        resource_id: str,
        user_id: int,
        auto_unlock: bool,
        website: str,
    ) -> bool:
        if auto_unlock:
            await wait_msg.edit_text(
                f"🤖 <b>自动解锁中...</b>\n\n"
                f"🆔 <code>{resource_id}</code>\n"
                f"💡 请求已提交到解锁队列",
                parse_mode="HTML",
            )
        else:
            await wait_msg.edit_text(
                f"🔓 <b>正在解锁资源...</b>\n\n"
                f"🆔 <code>{resource_id}</code>\n"
                f"💡 请求已提交到解锁队列",
                parse_mode="HTML",
            )

        wait_callback = self.build_unlock_wait_callback(wait_msg, auto_unlock=auto_unlock)
        try:
            result = await unlock_and_fetch(resource_id, user_id=user_id, wait_callback=wait_callback)
        except Exception as exc:
            logger.error("❌ 解锁出错: %s", exc)
            error_text = f"❌ {'自动' if auto_unlock else ''}解锁失败，请稍后重试"
            try:
                await wait_msg.edit_text(error_text, parse_mode="HTML")
            except Exception:
                await fallback_msg.reply(error_text, parse_mode="HTML")
            return False

        if result and result.get("link"):
            if self.link_extracted_handler is None:
                raise RuntimeError("link_extracted_handler 未配置")
            await self.link_extracted_handler(
                wait_msg,
                result["link"],
                result.get("code", "无"),
                auto_unlock=auto_unlock,
                website=result.get("website") or website,
                requester_user_id=user_id,
            )
            return True

        if auto_unlock:
            await self.notify_auto_unlock_failed(wait_msg, fallback_msg)
        else:
            try:
                await wait_msg.edit_text("❌ 解锁失败，请稍后重试", parse_mode="HTML")
            except Exception:
                await fallback_msg.reply("❌ 解锁失败，请稍后重试", parse_mode="HTML")
        return False

    async def handle_unlock_required(
        self,
        *,
        wait_msg: Message,
        fallback_msg: Message,
        resource_id: str,
        user_id: int,
        result: dict,
        website: str,
    ) -> bool:
        points = result["points"]

        if self.auto_unlock_threshold > 0 and points <= self.auto_unlock_threshold:
            logger.info("🤖 自动解锁: %s 积分 <= %s 积分阈值", points, self.auto_unlock_threshold)
            await self.perform_unlock_and_handle_result(
                wait_msg=wait_msg,
                fallback_msg=fallback_msg,
                resource_id=resource_id,
                user_id=user_id,
                auto_unlock=True,
                website=website,
            )
            return True

        user_points = await get_user_points()
        if user_points is not None and user_points < points:
            await wait_msg.edit_text(
                format_error_message(
                    "insufficient_points",
                    f"需要: <code>{points}</code> 积分\n"
                    f"当前: <code>{user_points}</code> 积分\n"
                    f"缺少: <code>{points - user_points}</code> 积分",
                ),
                parse_mode="HTML",
            )
            return True

        text, kb = format_unlock_confirmation(resource_id, points, user_points)
        await wait_msg.edit_text(text, reply_markup=kb, parse_mode="HTML")
        return True

    async def fetch_download_link_and_handle_result(
        self,
        *,
        wait_msg: Message,
        fallback_msg: Message,
        resource_id: str,
        user_id: int,
        website: str,
    ) -> bool:
        wait_callback = self.build_unlock_wait_callback(
            wait_msg,
            auto_unlock=False,
            title="提取排队中",
            tip="💡 当前提取链路将按官方 OpenAPI 解锁流程串行执行",
        )
        try:
            result = await unlock_resource(resource_id, user_id=user_id, wait_callback=wait_callback)
        except Exception as exc:
            logger.error("❌ 提取链接出错: %s", exc)
            await wait_msg.edit_text(format_error_message("fetch_failed"), parse_mode="HTML")
            return False

        link = str((result or {}).get("full_url") or (result or {}).get("url") or "").strip()
        if not link:
            await wait_msg.edit_text(format_error_message("fetch_failed"), parse_mode="HTML")
            return False

        if self.link_extracted_handler is None:
            raise RuntimeError("link_extracted_handler 未配置")
        await self.link_extracted_handler(
            wait_msg,
            link,
            (result or {}).get("access_code") or "无",
            auto_unlock=False,
            website=website,
            requester_user_id=user_id,
        )
        return True
