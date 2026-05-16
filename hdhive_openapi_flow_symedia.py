from __future__ import annotations

import asyncio
import logging
import time

import aiohttp
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from config import SA_AUTO_ADD_DELAY, SA_ENABLE_115_PUSH, SA_PARENT_ID, SA_TOKEN, SA_URL
from hdhive_openapi_state import HDHiveOpenAPIState
from utils import detect_provider_by_website, detect_share_provider, is_115_share_link

logger = logging.getLogger(__name__)

SA_HTTP_TIMEOUT = aiohttp.ClientTimeout(total=30, connect=10, sock_read=30)
SA_HTTP_HEADERS = {"Accept": "application/json", "User-Agent": "MediaBot/1.0"}


class HDHiveOpenAPISymediaFlow:
    def __init__(self, state: HDHiveOpenAPIState):
        self.state = state

    async def handle_link_extracted(
        self,
        wait_msg: Message,
        link: str,
        code: str = "无",
        auto_unlock: bool = False,
        website: str | None = None,
        requester_user_id: int | None = None,
    ):
        provider_key, provider_name = detect_provider_by_website(website)
        if provider_key == "unknown":
            provider_key, provider_name = detect_share_provider(link)
        button_text = f"🔗 打开{provider_name}"

        can_auto_add_sa = SA_URL and SA_PARENT_ID and SA_ENABLE_115_PUSH and provider_key == "115"
        if can_auto_add_sa:
            task_key = self.state.make_message_state_key(wait_msg)
            if not task_key:
                await wait_msg.edit_text("❌ 无法记录自动添加状态，请稍后重试", parse_mode="HTML")
                return

            kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=button_text, url=link)]])
            unlock_text = "🤖 自动解锁 · " if auto_unlock else ""
            await wait_msg.edit_text(
                f"✅ <b>{unlock_text}提取成功</b>\n\n"
                f"<blockquote>\n"
                f"🔗 <a href='{link}'>{provider_name}链接</a>\n"
                f"🔑 提取码: <code>{code}</code>\n"
                f"</blockquote>\n\n"
                f"⏱️ 将在 {SA_AUTO_ADD_DELAY} 秒后自动添加到 Symedia\n"
                f"💡 不需要的话发送 /hdc 取消（仅115支持自动添加）",
                reply_markup=kb,
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
            task = asyncio.create_task(self.auto_add_to_sa(task_key, link, wait_msg, countdown=SA_AUTO_ADD_DELAY))
            self.state.pending_sa_tasks[task_key] = {
                "link": link,
                "task": task,
                "cancelled": False,
                "user_id": requester_user_id,
                "created_at": time.time(),
            }
            logger.info("⏰ 已启动自动添加倒计时: %s", link)
            return

        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=button_text, url=link)]])
        unlock_text = "🤖 自动解锁 · " if auto_unlock else ""
        extra_tip = ""
        if SA_URL and SA_PARENT_ID and provider_key != "115":
            extra_tip = "\n\n💡 当前为非115链接，已跳过 Symedia 自动添加"
        elif SA_URL and SA_PARENT_ID and provider_key == "115" and not SA_ENABLE_115_PUSH:
            extra_tip = "\n\n💡 已关闭115自动推送到 Symedia（SA_ENABLE_115_PUSH=0）"

        await wait_msg.edit_text(
            f"✅ <b>{unlock_text}提取成功</b>\n\n"
            f"<blockquote>\n"
            f"🔗 <a href='{link}'>{provider_name}链接</a>\n"
            f"🔑 提取码: <code>{code}</code>\n"
            f"</blockquote>"
            f"{extra_tip}",
            reply_markup=kb,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )

    async def auto_add_to_sa(self, task_key: str, link: str, original_message: Message, countdown: int = 60):
        try:
            if not SA_ENABLE_115_PUSH:
                logger.info("⏭️ 已禁用115推送到SA，跳过自动添加: %s", link)
                return
            if not is_115_share_link(link):
                logger.info("⏭️ 跳过自动添加到SA（非115链接）: %s", link)
                return

            step = 10 if countdown > 10 else max(1, countdown)
            for remaining in range(countdown, 0, -step):
                if task_key in self.state.pending_sa_tasks and self.state.pending_sa_tasks[task_key].get("cancelled"):
                    logger.info("⏹️ 用户取消了自动添加: %s", link)
                    return
                try:
                    await original_message.edit_text(
                        f"✅ <b>提取成功</b>\n\n"
                        f"<blockquote>\n"
                        f"🔗 <a href='{link}'>115网盘链接</a>\n"
                        f"</blockquote>\n\n"
                        f"⏱️ 将在 {remaining} 秒后自动添加到 Symedia\n"
                        f"💡 不需要的话发送 /hdc 取消",
                        parse_mode="HTML",
                        disable_web_page_preview=True,
                    )
                except Exception as exc:
                    logger.debug("更新自动添加倒计时消息失败: %s", exc)
                await asyncio.sleep(step if remaining > step else remaining)

            if task_key in self.state.pending_sa_tasks and self.state.pending_sa_tasks[task_key].get("cancelled"):
                return

            logger.info("⏰ 倒计时结束，自动添加到SA: %s", link)
            api_url = f"{SA_URL}/api/v1/plugin/cloud_helper/add_share_urls_115"
            payload = {"urls": [link], "parent_id": SA_PARENT_ID}
            async with aiohttp.ClientSession(timeout=SA_HTTP_TIMEOUT, headers=SA_HTTP_HEADERS) as session:
                async with session.post(api_url, params={"token": SA_TOKEN}, json=payload) as response:
                    if response.status == 200:
                        data = await response.json()
                        message = data.get("message", "添加成功")
                        notification = (
                            "🎬 <b>已自动添加到Symedia</b>\n"
                            "━━━━━━━━━━━━━━━━\n\n"
                            f"📊 <b>状态:</b> {message}\n"
                            f"🔗 <b>链接:</b> <code>{link}</code>\n"
                            f"📁 <b>目录ID:</b> <code>{SA_PARENT_ID}</code>\n"
                            f"⏰ <b>方式:</b> 自动添加"
                        )
                        await original_message.edit_text(notification, parse_mode="HTML")
                        logger.info("✅ 自动添加到SA成功: %s - %s", link, message)
                    else:
                        error_text = await response.text()
                        logger.error("❌ SA API返回错误: %s - %s", response.status, error_text)
                        await original_message.edit_text(f"❌ 自动添加到SA失败: HTTP {response.status}", parse_mode="HTML")
        except asyncio.CancelledError:
            logger.info("⏹️ 自动添加任务被取消: %s", link)
        except Exception as exc:
            logger.error("❌ 自动添加到SA失败: %s", exc)
            try:
                await original_message.edit_text(f"❌ 自动添加失败: {str(exc)}", parse_mode="HTML")
            except Exception as inner_exc:
                logger.debug("更新自动添加失败提示消息失败: %s", inner_exc)
        finally:
            if task_key in self.state.pending_sa_tasks:
                del self.state.pending_sa_tasks[task_key]

    async def cancel_latest_sa_task(self, message: Message):
        user_id = message.from_user.id
        latest_message_id = None
        latest_task = None
        latest_created_at = -1.0
        for msg_id, task_info in self.state.pending_sa_tasks.items():
            if task_info.get("cancelled"):
                continue
            if task_info.get("user_id") != user_id:
                continue
            created_at = float(task_info.get("created_at") or 0)
            if created_at >= latest_created_at:
                latest_created_at = created_at
                latest_message_id = msg_id
                latest_task = task_info

        if latest_task:
            latest_task["cancelled"] = True
            latest_task["task"].cancel()
            await message.reply(
                f"✅ 已取消最近一次自动添加任务\n\n"
                f"🔗 链接: {latest_task['link']}",
                parse_mode="HTML",
            )
            logger.info(
                "❌ 用户取消了自动添加任务: user_id=%s message_id=%s link=%s",
                user_id,
                latest_message_id,
                latest_task["link"],
            )
            return

        await message.reply("⚠️ 没有进行中的自动添加任务", parse_mode="HTML")

    async def handle_send_to_sa_callback(self, callback: CallbackQuery):
        try:
            link = callback.data.replace("send_to_group:", "")
            if not is_115_share_link(link):
                await callback.answer("❌ 仅支持115链接添加到Symedia", show_alert=True)
                return
            if not SA_ENABLE_115_PUSH:
                await callback.answer("⚠️ 已禁用115推送到Symedia", show_alert=True)
                return
            if not SA_URL or not SA_PARENT_ID:
                await callback.answer("❌ 未配置SA，无法添加到Symedia", show_alert=True)
                return

            api_url = f"{SA_URL}/api/v1/plugin/cloud_helper/add_share_urls_115"
            payload = {"urls": [link], "parent_id": SA_PARENT_ID}
            await callback.answer("⏳ 正在添加到Symedia...", show_alert=False)
            async with aiohttp.ClientSession(timeout=SA_HTTP_TIMEOUT, headers=SA_HTTP_HEADERS) as session:
                async with session.post(api_url, params={"token": SA_TOKEN}, json=payload) as response:
                    if response.status == 200:
                        data = await response.json()
                        message = data.get("message", "添加成功")
                        notification = (
                            "🎬 <b>已添加到Symedia</b>\n"
                            "━━━━━━━━━━━━━━━━\n\n"
                            f"📊 <b>状态:</b> {message}\n"
                            f"🔗 <b>链接:</b> <code>{link}</code>\n"
                            f"📁 <b>目录ID:</b> <code>{SA_PARENT_ID}</code>"
                        )
                        await callback.message.reply(notification, parse_mode="HTML")
                        await callback.message.edit_reply_markup(reply_markup=None)
                        logger.info("✅ 成功添加到SA: %s - %s", link, message)
                    else:
                        error_text = await response.text()
                        logger.error("❌ SA API返回错误: %s - %s", response.status, error_text)
                        await callback.message.reply(f"❌ 添加到 Symedia 失败: HTTP {response.status}", parse_mode="HTML")
        except aiohttp.ClientError as exc:
            logger.error("❌ 网络请求失败: %s", exc)
            await callback.answer("❌ 网络请求失败，请检查SA配置", show_alert=True)
        except Exception as exc:
            logger.error("❌ 添加到SA失败: %s", exc)
            await callback.answer(f"❌ 添加失败: {str(exc)}", show_alert=True)
