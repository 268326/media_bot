from __future__ import annotations

import logging
import re
import time
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from config import HDHIVE_PARSE_INCOMING_LINKS
from formatter import format_error_message, format_resource_list, format_tmdb_info
from hdhive_openapi_client import fetch_download_link, get_resources_by_tmdb_id
from tmdb_api import get_tmdb_details, search_tmdb
from utils import parse_hdhive_link
from hdhive_openapi_state import HDHiveOpenAPIState, TMDB_PAGE_SIZE

logger = logging.getLogger(__name__)


class HDHiveOpenAPISearchFlow:
    def __init__(self, state: HDHiveOpenAPIState):
        self.state = state
        self.unlock_required_handler = None
        self.fetch_result_handler = None
        self.resource_link_handler = None
        self.tmdb_link_handler = None

    def build_tmdb_candidate_message(self, results: list[dict], page: int = 0) -> tuple[str, InlineKeyboardMarkup]:
        total = len(results)
        total_pages = max(1, (total + TMDB_PAGE_SIZE - 1) // TMDB_PAGE_SIZE)
        current_page = min(max(page, 0), total_pages - 1)
        start = current_page * TMDB_PAGE_SIZE
        page_items = results[start:start + TMDB_PAGE_SIZE]

        result_text = f"🔍 <b>找到 {total} 个匹配结果</b>\n"
        result_text += f"第 <code>{current_page + 1}</code>/<code>{total_pages}</code> 页\n"
        result_text += "─────────────────\n"
        result_text += "请选择你要的是哪一个:\n"

        button_row: list[InlineKeyboardButton] = []
        for idx, item in enumerate(page_items, 1):
            overview = item.get("overview", "暂无简介")
            if len(overview) > 100:
                overview = overview[:100] + "…"
            display_index = start + idx
            result_text += "\n<blockquote>\n"
            result_text += f"<b>{display_index}. {item['title']}</b>\n"
            result_text += f"📅 {item.get('release_date', '未知')}"
            if item.get("rating"):
                result_text += f" · ⭐️ {item['rating']:.1f}\n"
            else:
                result_text += "\n"
            result_text += f"{overview}\n"
            result_text += "</blockquote>"
            button_row.append(InlineKeyboardButton(text=f"{idx}", callback_data=f"select_tmdb:{start + idx - 1}"))

        inline_keyboard: list[list[InlineKeyboardButton]] = []
        if button_row:
            inline_keyboard.append(button_row)
        nav_row: list[InlineKeyboardButton] = []
        if current_page > 0:
            nav_row.append(InlineKeyboardButton(text="⬅️ 上一页", callback_data=f"tmdb_page:{current_page - 1}"))
        nav_row.append(InlineKeyboardButton(text=f"{current_page + 1}/{total_pages}", callback_data="tmdb_page:noop"))
        if current_page < total_pages - 1:
            nav_row.append(InlineKeyboardButton(text="下一页 ➡️", callback_data=f"tmdb_page:{current_page + 1}"))
        inline_keyboard.append(nav_row)

        result_text += "\n─────────────────\n"
        result_text += "<b>点击数字选择</b>"
        return result_text, InlineKeyboardMarkup(inline_keyboard=inline_keyboard)

    async def handle_resource_link(self, message: Message, resource_id: str, resource_url: str | None = None):
        _ = resource_url
        if self.unlock_required_handler is None or self.fetch_result_handler is None:
            raise RuntimeError("search flow handlers 未配置")

        user_id = message.from_user.id
        wait_msg = await message.reply(
            f"🔗 <b>检测到资源链接</b>\n\n"
            f"🆔 <code>{resource_id}</code>\n"
            f"⏳ 正在提取链接...",
            parse_mode="HTML",
        )
        try:
            result = await fetch_download_link(resource_id, user_id=user_id)
        except Exception as exc:
            logger.error("❌ 资源链接提取前置检查失败: %s", exc)
            await wait_msg.edit_text(format_error_message("fetch_failed"), parse_mode="HTML")
            return

        website = (result or {}).get("website") or self.state.resource_website_cache.get(resource_id, "")
        if result and result.get("need_unlock"):
            if await self.unlock_required_handler(
                wait_msg=wait_msg,
                fallback_msg=message,
                resource_id=resource_id,
                user_id=user_id,
                result=result,
                website=website,
            ):
                return
        elif result and (result.get("link") or result.get("need_unlock") is False):
            await self.fetch_result_handler(
                wait_msg=wait_msg,
                fallback_msg=message,
                resource_id=resource_id,
                user_id=user_id,
                website=result.get("website") or website,
            )
            return
        await wait_msg.edit_text(format_error_message("fetch_failed"), parse_mode="HTML")

    async def handle_tmdb_link(self, message: Message, tmdb_id: str, media_type: str):
        type_name = "🎬 电影" if media_type == "movie" else "📺 剧集"
        wait_msg = await message.reply(
            f"🔗 <b>检测到{type_name}页面</b>\n\n"
            f"🆔 TMDB ID: <code>{tmdb_id}</code>\n"
            f"⏳ 正在获取资源列表...",
            parse_mode="HTML",
        )
        try:
            resources = await get_resources_by_tmdb_id(tmdb_id, media_type)
        except Exception as exc:
            logger.error("❌ TMDB 页面资源获取失败: tmdb_id=%s media_type=%s error=%s", tmdb_id, media_type, exc)
            await wait_msg.edit_text(format_error_message("fetch_failed"), parse_mode="HTML")
            return
        if not resources:
            await wait_msg.edit_text(format_error_message("no_resources"), parse_mode="HTML")
            return
        self.state.cache_resource_websites(resources)
        text, kb = format_resource_list(resources, media_type, provider_filter="115")
        await wait_msg.edit_text(text, reply_markup=kb, parse_mode="HTML")
        self.state.save_resource_list_state(wait_msg, resources, media_type, title=None)

    async def handle_keyword_search(self, message: Message, keyword: str, search_type: str):
        media_type = "tv" if search_type == "tv" else "movie"
        wait_msg = await message.reply(f"🔍 搜索中 · {keyword}", parse_mode="HTML")
        try:
            start_ts = time.monotonic()
            tmdb_info = None
            tmdb_result = await search_tmdb(keyword, media_type)
            if not tmdb_result:
                await wait_msg.edit_text(format_error_message("no_results"), parse_mode="HTML")
                return
            if isinstance(tmdb_result, list):
                await wait_msg.delete()
                result_text, kb = self.build_tmdb_candidate_message(tmdb_result, page=0)
                sent_msg = await message.reply(result_text, reply_markup=kb, parse_mode="HTML")
                self.state.save_tmdb_search_state(sent_msg, tmdb_result, page=0)
                elapsed = int((time.monotonic() - start_ts) * 1000)
                logger.info("✅ TMDB多结果返回: keyword=%s type=%s count=%s cost_ms=%s", keyword, search_type, len(tmdb_result), elapsed)
                return

            tmdb_info = tmdb_result
            tmdb_id = tmdb_result["tmdb_id"]
            result_type = tmdb_result["media_type"]
            title = tmdb_result["title"]
            if tmdb_info.get("poster_url"):
                info_text = format_tmdb_info(tmdb_info)
                try:
                    await message.reply_photo(photo=tmdb_info["poster_url"], caption=info_text, parse_mode="HTML")
                except Exception:
                    await message.reply(info_text, parse_mode="HTML")

            logger.info("✅ TMDB匹配成功，获取资源: %s (ID: %s)", title, tmdb_id)
            resources = await get_resources_by_tmdb_id(tmdb_id, result_type)
            if not resources:
                error_msg = format_error_message("no_results")
                if tmdb_info:
                    await message.reply(error_msg, parse_mode="HTML")
                else:
                    await wait_msg.edit_text(error_msg, parse_mode="HTML")
                return

            self.state.cache_resource_websites(resources)
            text, kb = format_resource_list(resources, media_type, provider_filter="115")
            if tmdb_info:
                sent_msg = await message.reply(text, reply_markup=kb, parse_mode="HTML")
                self.state.save_resource_list_state(sent_msg, resources, media_type, title=None)
            else:
                await wait_msg.edit_text(text, reply_markup=kb, parse_mode="HTML")
                self.state.save_resource_list_state(wait_msg, resources, media_type, title=None)

            elapsed = int((time.monotonic() - start_ts) * 1000)
            logger.info("✅ 关键词搜索完成: keyword=%s type=%s resources=%s cost_ms=%s", keyword, search_type, len(resources), elapsed)
        except Exception as exc:
            logger.error("❌ 搜索出错: %s", exc)
            await wait_msg.edit_text(f"❌ 运行出错: {exc}", parse_mode="HTML")

    async def handle_search_input(self, message: Message, user_input: str, search_type: str):
        link_info = parse_hdhive_link(user_input)
        if link_info["type"] == "resource":
            await self.handle_resource_link(message, link_info["id"], link_info.get("resource_url"))
            return
        if link_info["type"] == "tmdb":
            await self.handle_tmdb_link(message, link_info["id"], link_info["media_type"])
            return
        await self.handle_keyword_search(message, user_input, search_type)

    async def handle_provider_filter_callback(self, callback: CallbackQuery):
        msg = callback.message
        if not msg:
            await callback.answer()
            return
        state_key = self.state.make_message_state_key(msg)
        state = self.state.resource_list_state.get(state_key or "")
        if not state:
            await callback.answer("列表已过期，请重新搜索", show_alert=True)
            return
        provider = (callback.data or "pf:115").split(":", 1)[1]
        text, kb = format_resource_list(state["resources"], state["media_type"], title=state.get("title"), provider_filter=provider)
        await msg.edit_text(text, reply_markup=kb, parse_mode="HTML")
        await callback.answer()

    async def handle_tmdb_page_callback(self, callback: CallbackQuery):
        msg = callback.message
        if not msg:
            await callback.answer()
            return
        state_key = self.state.make_message_state_key(msg)
        state = self.state.tmdb_search_state.get(state_key or "")
        if not state:
            await callback.answer("列表已过期，请重新搜索", show_alert=True)
            return
        action = (callback.data or "tmdb_page:noop").split(":", 1)[1]
        if action == "noop":
            await callback.answer()
            return
        try:
            page = int(action)
        except ValueError:
            await callback.answer("页码无效", show_alert=True)
            return
        state["page"] = page
        text, kb = self.build_tmdb_candidate_message(state["results"], page=page)
        await msg.edit_text(text, reply_markup=kb, parse_mode="HTML")
        await callback.answer()

    async def handle_resource_callback(self, callback: CallbackQuery):
        await callback.answer()
        wait_msg: Message | None = None
        try:
            msg = callback.message
            if not msg:
                return
            if self.unlock_required_handler is None or self.fetch_result_handler is None:
                raise RuntimeError("search flow handlers 未配置")
            user_id = callback.from_user.id
            data_parts = (callback.data or "").split(":")
            if len(data_parts) != 2:
                await msg.answer("❌ 数据格式错误")
                return
            resource_id = data_parts[1]
            wait_msg = await msg.reply(f"⏳ 正在提取链接...\n🆔 <code>{resource_id}</code>", parse_mode="HTML")
            try:
                result = await fetch_download_link(resource_id, user_id=user_id)
            except Exception as exc:
                logger.error("❌ 资源选择前置检查失败: %s", exc)
                await wait_msg.edit_text(format_error_message("fetch_failed"), parse_mode="HTML")
                return
            website = (result or {}).get("website") or self.state.resource_website_cache.get(resource_id, "")
            if result and result.get("need_unlock"):
                if await self.unlock_required_handler(
                    wait_msg=wait_msg,
                    fallback_msg=msg,
                    resource_id=resource_id,
                    user_id=user_id,
                    result=result,
                    website=website,
                ):
                    return
            if result and (result.get("link") or result.get("need_unlock") is False):
                await self.fetch_result_handler(
                    wait_msg=wait_msg,
                    fallback_msg=msg,
                    resource_id=resource_id,
                    user_id=user_id,
                    website=result.get("website") or website,
                )
                return
            await wait_msg.edit_text(format_error_message("fetch_failed"), parse_mode="HTML")
        except Exception as exc:
            logger.error("❌ 回调处理出错: %s", exc)
            if wait_msg is not None:
                await wait_msg.edit_text(f"❌ 处理出错: {exc}", parse_mode="HTML")
            elif callback.message:
                await callback.message.answer(f"❌ 处理出错: {exc}", parse_mode="HTML")

    async def handle_direct_link_message(self, message: Message):
        text = message.text or message.caption
        if not text:
            return
        if text.startswith("/"):
            return
        if not HDHIVE_PARSE_INCOMING_LINKS:
            return
        urls: list[str] = []
        entities = message.entities or message.caption_entities
        if entities:
            for entity in entities:
                if entity.type == "url":
                    urls.append(text[entity.offset:entity.offset + entity.length])
                elif entity.type == "text_link":
                    urls.append(entity.url)
        if not urls:
            return
        hdhive_urls = [url for url in urls if "hdhive.com" in url]
        if not hdhive_urls:
            return
        logger.info("📎 从消息中提取到 %s 个HDHive链接: %s", len(hdhive_urls), hdhive_urls)
        resource_url = None
        for url in hdhive_urls:
            if "/resource/115/" in url:
                resource_url = url
                break
        if not resource_url:
            for url in hdhive_urls:
                if "/resource/" in url:
                    resource_url = url
                    break
        if resource_url:
            resource_match = re.search(r"/resource/(?:115/)?([a-f0-9-]+)", resource_url)
            if not resource_match:
                await message.reply("❌ 资源链接格式不正确，无法解析资源ID", parse_mode="HTML")
                return
            await self.handle_resource_link(message, resource_match.group(1), resource_url)
            return
        tmdb_url = None
        for url in hdhive_urls:
            if "/movie/" in url or "/tv/" in url:
                tmdb_url = url
                break
        if tmdb_url:
            link_info = parse_hdhive_link(tmdb_url)
            if link_info["type"] == "tmdb":
                await self.handle_tmdb_link(message, link_info["id"], link_info["media_type"])

    async def handle_select_tmdb_callback(self, callback: CallbackQuery):
        await callback.answer()
        try:
            msg = callback.message
            if not msg:
                return
            state_key = self.state.make_message_state_key(msg)
            state = self.state.tmdb_search_state.get(state_key or "")
            if not state:
                await callback.answer("列表已过期，请重新搜索", show_alert=True)
                return
            parts = (callback.data or "").split(":")
            if len(parts) != 2:
                await callback.answer("数据格式错误", show_alert=True)
                return
            try:
                selected_index = int(parts[1])
            except ValueError:
                await callback.answer("选择项无效", show_alert=True)
                return
            results = state.get("results") or []
            if selected_index < 0 or selected_index >= len(results):
                await callback.answer("列表已过期，请重新搜索", show_alert=True)
                return
            tmdb_info = results[selected_index]
            tmdb_id = str(tmdb_info["tmdb_id"])
            media_type = tmdb_info["media_type"]
            if state_key:
                self.state.tmdb_search_state.pop(state_key, None)
            await callback.message.edit_text("⏳ 正在获取TMDB信息...", parse_mode="HTML")
            tmdb_info = await get_tmdb_details(int(tmdb_id), media_type) or tmdb_info
            if tmdb_info:
                info_text = f"<b>{tmdb_info['title']}</b>\n"
                if tmdb_info.get("release_date"):
                    info_text += f"{tmdb_info['release_date']}"
                if tmdb_info.get("rating"):
                    info_text += f" · ⭐️ {tmdb_info['rating']:.1f}\n"
                else:
                    info_text += "\n"
                overview = tmdb_info.get("overview", "暂无简介")
                if len(overview) > 200:
                    overview = overview[:200] + "…"
                info_text += f"\n{overview}\n\n正在获取资源…"
                if tmdb_info.get("poster_url"):
                    try:
                        await callback.message.answer_photo(photo=tmdb_info["poster_url"], caption=info_text, parse_mode="HTML")
                        await callback.message.delete()
                    except Exception as exc:
                        logger.warning("发送图片失败: %s", exc)
                        await callback.message.edit_text(info_text, parse_mode="HTML")
                else:
                    await callback.message.edit_text(info_text, parse_mode="HTML")
            else:
                await callback.message.edit_text("⏳ 正在获取资源...", parse_mode="HTML")
            resources = await get_resources_by_tmdb_id(int(tmdb_id), media_type)
            if not resources:
                if tmdb_info:
                    await callback.message.answer("❌ 未找到资源", parse_mode="HTML")
                else:
                    await callback.message.edit_text("❌ 未找到资源", parse_mode="HTML")
                return
            self.state.cache_resource_websites(resources)
            text, kb = format_resource_list(resources, media_type, provider_filter="115")
            if tmdb_info and tmdb_info.get("poster_url"):
                sent_msg = await callback.message.answer(text, reply_markup=kb, parse_mode="HTML")
                self.state.save_resource_list_state(sent_msg, resources, media_type, title=None)
            else:
                await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
                self.state.save_resource_list_state(callback.message, resources, media_type, title=None)
        except Exception as exc:
            logger.error("❌ 处理TMDB选择出错: %s", exc)
            if callback.message:
                await callback.message.answer(f"❌ 处理出错: {exc}", parse_mode="HTML")
