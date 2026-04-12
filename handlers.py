"""
Telegram 处理器模块
处理所有命令和回调查询
"""
import logging
import re
import asyncio
import aiohttp
import html
import os
import time
from aiogram import types, Router, F
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    Message,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    BufferedInputFile,
)

from config import (
    ALLOWED_USER_ID,
    SA_URL,
    SA_PARENT_ID,
    SA_AUTO_ADD_DELAY,
    SA_TOKEN,
    SA_ENABLE_115_PUSH,
    AUTO_UNLOCK_THRESHOLD,
    LOG_PATH,
)
from danmu_service import fetch_bilibili_danmaku_xml, DanmuError
from checkin_service import daily_check_in
from utils import parse_hdhive_link, detect_share_provider, is_115_share_link, detect_provider_by_website
from hdhive_client import (
    get_resources_by_tmdb_id,
    fetch_download_link, 
    unlock_and_fetch,
    get_user_points
)
from session_manager import session_manager
from tmdb_api import search_tmdb, get_tmdb_details
from formatter import (
    format_resource_list,
    format_download_link,
    format_unlock_confirmation,
    format_tmdb_info,
    format_points_message,
    format_error_message,
    format_help_message,
    format_start_message
)
from strm_handlers import router as strm_router

# 创建路由器
router = Router()
router.include_router(strm_router)

# 待添加到SA的任务字典 {message_id: {"link": str, "task": asyncio.Task, "cancelled": bool}}
pending_sa_tasks = {}
# 资源网盘类型缓存：resource_id -> website
resource_website_cache: dict[str, str] = {}
# 资源列表状态缓存：message_id -> {"resources": list, "media_type": str, "title": str|None}
resource_list_state: dict[int, dict] = {}
# TMDB 候选列表状态缓存：message_id -> {"results": list[dict], "page": int}
tmdb_search_state: dict[int, dict] = {}

TMDB_PAGE_SIZE = 5


def build_tmdb_candidate_message(results: list[dict], page: int = 0) -> tuple[str, InlineKeyboardMarkup]:
    """构建 TMDB 候选项分页消息。"""
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
        result_text += f"\n<blockquote>\n"
        result_text += f"<b>{display_index}. {item['title']}</b>\n"
        result_text += f"📅 {item.get('release_date', '未知')}"
        if item.get("rating"):
            result_text += f" · ⭐️ {item['rating']:.1f}\n"
        else:
            result_text += "\n"
        result_text += f"{overview}\n"
        result_text += f"</blockquote>"

        button_row.append(
            InlineKeyboardButton(
                text=f"{idx}",
                callback_data=f"select_tmdb:{start + idx - 1}",
            )
        )

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


async def notify_auto_unlock_failed(target_msg: Message, fallback_msg: Message):
    """通知自动解锁失败（必要时回退发送新消息）"""
    try:
        await target_msg.edit_text("❌ 自动解锁失败，请稍后重试", parse_mode="HTML")
    except Exception:
        await fallback_msg.reply("❌ 自动解锁失败，请稍后重试", parse_mode="HTML")


async def handle_link_extracted(
    wait_msg: Message,
    link: str,
    code: str = "无",
    auto_unlock: bool = False,
    website: str | None = None,
):
    """
    统一处理链接提取成功后的逻辑
    
    Args:
        wait_msg: 等待消息对象
        link: 115链接
        code: 提取码
        auto_unlock: 是否是自动解锁
    """
    provider_key, provider_name = detect_provider_by_website(website)
    if provider_key == "unknown":
        provider_key, provider_name = detect_share_provider(link)
    button_text = f"🔗 打开{provider_name}"

    # 仅 115 链接支持自动添加到 SA
    can_auto_add_sa = SA_URL and SA_PARENT_ID and SA_ENABLE_115_PUSH and provider_key == "115"

    if can_auto_add_sa:
        message_id = wait_msg.message_id
        
        # 显示网盘链接按钮
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=button_text, url=link)]
        ])
        
        unlock_text = "🤖 自动解锁 · " if auto_unlock else ""
        
        # 更新消息
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
            disable_web_page_preview=True
        )
        
        # 创建自动添加任务
        task = asyncio.create_task(auto_add_to_sa(message_id, link, wait_msg, countdown=SA_AUTO_ADD_DELAY))
        pending_sa_tasks[message_id] = {
            "link": link,
            "task": task,
            "cancelled": False
        }
        
        logging.info(f"⏰ 已启动自动添加倒计时: {link}")
    else:
        # 非115或未配置SA：只显示链接，不自动添加
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=button_text, url=link)]
        ])
        
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
            disable_web_page_preview=True
        )


# ==================== 权限检查中间件 ====================

async def check_user_permission(message: Message) -> bool:
    """
    检查用户是否有权限使用机器人
    
    Args:
        message: 消息对象
        
    Returns:
        bool: 是否有权限
    """
    if ALLOWED_USER_ID == 0:
        return True  # 未配置限制，允许所有人
    
    user_id = message.from_user.id
    if user_id != ALLOWED_USER_ID:
        await message.reply(
            format_error_message('permission_denied'),
            parse_mode="HTML"
        )
        logging.warning(f"❌ 用户 {user_id} ({message.from_user.username}) 尝试使用机器人但被拒绝")
        return False
    return True


# ==================== 命令处理器 ====================

@router.message(Command("start"))
async def cmd_start(message: Message):
    """启动命令 - 检查Bot状态"""
    if not await check_user_permission(message):
        return
    
    bot = message.bot
    text = format_start_message(bot.id, message.from_user.id)
    await message.reply(text, parse_mode="HTML")


@router.message(Command("help"))
async def cmd_help(message: Message):
    """显示帮助信息"""
    if not await check_user_permission(message):
        return
    
    text = format_help_message()
    await message.reply(text, parse_mode="HTML")


@router.message(Command("points"))
async def cmd_check_points(message: Message):
    """查询用户积分"""
    if not await check_user_permission(message):
        return
    
    wait_msg = await message.reply("⏳ 正在查询积分...", parse_mode="HTML")
    
    points = await get_user_points()
    
    if points is not None:
        text = format_points_message(points)
        await wait_msg.edit_text(text, parse_mode="HTML")
    else:
        await wait_msg.edit_text(
            format_error_message('points_unavailable'),
            parse_mode="HTML"
        )


@router.message(Command("checkin"))
async def cmd_checkin(message: Message):
    """执行每日签到"""
    if not await check_user_permission(message):
        return

    wait_msg = await message.reply("⏳ 正在执行每日签到...", parse_mode="HTML")
    result = await daily_check_in()

    before_points = result.get("before_points")
    after_points = result.get("after_points")
    points_text = ""
    if before_points is not None or after_points is not None:
        points_text = (
            f"\n\n<blockquote>\n"
            f"签到前积分: <code>{before_points if before_points is not None else '未知'}</code>\n"
            f"签到后积分: <code>{after_points if after_points is not None else '未知'}</code>\n"
            f"</blockquote>"
        )

    if result.get("success"):
        title = "✅ 今日已签到" if result.get("already_checked_in") else "✅ 签到成功"
        await wait_msg.edit_text(
            f"{title}\n\n"
            f"{html.escape(result.get('message', ''))}"
            f"{points_text}",
            parse_mode="HTML"
        )
    else:
        await wait_msg.edit_text(
            f"❌ 签到失败\n\n"
            f"{html.escape(result.get('message', '未知错误'))}"
            f"{points_text}",
            parse_mode="HTML"
        )


@router.message(Command("danmu"))
async def cmd_danmu(message: Message):
    """下载B站弹幕(XML)"""
    if not await check_user_permission(message):
        return

    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.reply(
            "请使用: <code>/danmu B站链接</code>",
            parse_mode="HTML"
        )
        return

    link = args[1].strip()
    logging.info("🎬 /danmu 请求: user=%s link=%s", message.from_user.id, link)
    wait_msg = await message.reply("⏳ 正在获取弹幕...", parse_mode="HTML")

    try:
        result = await fetch_bilibili_danmaku_xml(link)
    except DanmuError as e:
        await wait_msg.edit_text(
            f"❌ 获取弹幕失败\n\n{html.escape(str(e))}",
            parse_mode="HTML"
        )
        return
    except Exception as e:
        logging.error(f"❌ 获取弹幕失败: {e}")
        await wait_msg.edit_text("❌ 获取弹幕失败，请稍后重试", parse_mode="HTML")
        return

    filename = f"{result.filename}.xml"
    file = BufferedInputFile(result.content, filename=filename)
    caption = f"✅ 弹幕已获取\n\n<code>{html.escape(filename)}</code>"
    await message.reply_document(file, caption=caption, parse_mode="HTML")
    await wait_msg.delete()
    logging.info("✅ /danmu 完成: user=%s filename=%s cid=%s", message.from_user.id, filename, result.cid)


@router.message(Command("hdc"))
async def cmd_cancel_sa(message: Message):
    """取消最近一次的自动添加到SA任务"""
    if not await check_user_permission(message):
        return
    
    # 查找最近的一个未取消的任务
    latest_task = None
    latest_message_id = None
    
    for msg_id, task_info in pending_sa_tasks.items():
        if not task_info["cancelled"]:
            latest_message_id = msg_id
            latest_task = task_info
            break
    
    if latest_task:
        # 取消任务
        latest_task["cancelled"] = True
        latest_task["task"].cancel()
        
        await message.reply(
            f"✅ 已取消自动添加任务\n\n"
            f"🔗 链接: {latest_task['link']}",
            parse_mode="HTML"
        )
        logging.info(f"❌ 用户取消了自动添加任务: {latest_task['link']}")
    else:
        await message.reply("⚠️ 没有进行中的自动添加任务", parse_mode="HTML")


@router.message(Command("llog"))
async def cmd_tail_log(message: Message):
    """发送最新30行日志"""
    if not await check_user_permission(message):
        return

    log_path = LOG_PATH

    if not os.path.exists(log_path):
        await message.reply(
            f"⚠️ 未找到日志文件 {html.escape(os.path.basename(log_path))}",
            parse_mode="HTML"
        )
        return

    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except Exception as e:
        logging.warning(f"⚠️ 读取日志失败: {e}")
        await message.reply("❌ 读取日志失败", parse_mode="HTML")
        return

    if not lines:
        await message.reply("⚠️ 日志为空", parse_mode="HTML")
        return

    tail_text = "".join(lines[-30:]).strip()
    if not tail_text:
        await message.reply("⚠️ 日志为空", parse_mode="HTML")
        return

    safe_text = html.escape(tail_text)
    if len(safe_text) > 3500:
        safe_text = "...(truncated)\n" + safe_text[-3500:]

    await message.reply(
        f"🧾 最近30行日志:\n<pre>{safe_text}</pre>",
        parse_mode="HTML"
    )


@router.message(Command("hdt"))
async def cmd_search_tv(message: Message):
    """搜索剧集"""
    await handle_search(message, "tv")


@router.message(Command("hdm"))
async def cmd_search_movie(message: Message):
    """搜索电影"""
    await handle_search(message, "movie")


# ==================== 核心搜索处理 ====================

async def handle_search(message: Message, search_type: str):
    """
    处理搜索命令
    
    Args:
        message: 消息对象
        search_type: 'tv' 或 'movie'
    """
    # 权限检查
    if not await check_user_permission(message):
        return
    
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        cmd = "/hdt" if search_type == "tv" else "/hdm"
        await message.reply(
            f"请使用: <code>{cmd} 名字或链接</code>",
            parse_mode="HTML"
        )
        return
    
    user_input = args[1]
    
    # 解析链接
    link_info = parse_hdhive_link(user_input)
    
    # ========== 优先级1: 资源直接链接 ==========
    if link_info["type"] == "resource":
        await handle_resource_link(message, link_info["id"], link_info.get("resource_url"))
        return
    
    # ========== 优先级2: TMDB页面链接 ==========
    if link_info["type"] == "tmdb":
        await handle_tmdb_link(message, link_info["id"], link_info["media_type"])
        return
    
    # ========== 优先级3: 关键词搜索 ==========
    await handle_keyword_search(message, user_input, search_type)


async def handle_resource_link(message: Message, resource_id: str, resource_url: str | None = None):
    """
    处理资源直接链接
    
    Args:
        message: 消息对象
        resource_id: 资源ID
    """
    user_id = message.from_user.id
    
    wait_msg = await message.reply(
        f"🔗 <b>检测到资源链接</b>\n\n"
        f"🆔 <code>{resource_id}</code>\n"
        f"⏳ 正在提取链接...",
        parse_mode="HTML"
    )
    
    # 使用会话管理模式
    result = await fetch_download_link(
        resource_id,
        user_id=user_id,
        keep_session=True,
        start_url=resource_url,
    )
    website = (result or {}).get("website") or resource_website_cache.get(resource_id, "")
    
    if result and result.get("need_unlock"):
        # 需要解锁
        points = result["points"]
        
        # 检查是否自动解锁
        if AUTO_UNLOCK_THRESHOLD > 0 and points <= AUTO_UNLOCK_THRESHOLD:
            # 自动解锁
            logging.info(f"🤖 自动解锁: {points} 积分 <= {AUTO_UNLOCK_THRESHOLD} 积分阈值")
            await wait_msg.edit_text(
                f"🤖 <b>自动解锁中...</b>\n\n"
                f"🆔 <code>{resource_id}</code>\n"
                f"💰 消耗积分: <code>{points}</code>",
                parse_mode="HTML"
            )
            
            # 执行解锁
            unlock_result = await unlock_and_fetch(resource_id, user_id=user_id)
            
            if unlock_result and unlock_result.get("link"):
                await handle_link_extracted(
                    wait_msg,
                    unlock_result["link"],
                    unlock_result.get("code", "无"),
                    auto_unlock=True,
                    website=unlock_result.get("website") or website,
                )
            else:
                await notify_auto_unlock_failed(wait_msg, message)
            return

        user_points = await get_user_points()

        if user_points is not None and user_points < points:
            # 积分不足，关闭会话
            session_id = result.get("session_id")
            if session_id:
                await session_manager.close_session(session_id)
            
            await wait_msg.edit_text(
                format_error_message('insufficient_points',
                    f"需要: <code>{points}</code> 积分\n"
                    f"当前: <code>{user_points}</code> 积分\n"
                    f"缺少: <code>{points - user_points}</code> 积分"),
                parse_mode="HTML"
            )
            return
        
        # 询问是否解锁（会话保持打开）
        text, kb = format_unlock_confirmation(resource_id, points, user_points)
        await wait_msg.edit_text(text, reply_markup=kb, parse_mode="HTML")
        return
    
    elif result and result.get("link"):
        # 成功提取链接 - 统一使用 handle_link_extracted 处理
        await handle_link_extracted(
            wait_msg,
            result["link"],
            result.get("code", "无"),
            auto_unlock=False,
            website=result.get("website") or website,
        )
        return
    else:
        await wait_msg.edit_text(
            format_error_message('fetch_failed'),
            parse_mode="HTML"
        )


async def handle_tmdb_link(message: Message, tmdb_id: str, media_type: str):
    """
    处理 TMDB 页面链接
    
    Args:
        message: 消息对象
        tmdb_id: TMDB ID 或 UUID
        media_type: 'movie' 或 'tv'
    """
    type_name = "🎬 电影" if media_type == "movie" else "📺 剧集"
    
    wait_msg = await message.reply(
        f"🔗 <b>检测到{type_name}页面</b>\n\n"
        f"🆔 TMDB ID: <code>{tmdb_id}</code>\n"
        f"⏳ 正在获取资源列表...",
        parse_mode="HTML"
    )
    
    resources = await get_resources_by_tmdb_id(tmdb_id, media_type)
    
    if not resources:
        await wait_msg.edit_text(
            format_error_message('no_resources'),
            parse_mode="HTML"
        )
        return
    
    # 格式化并发送资源列表
    for res in resources:
        rid = str(res.get("id") or "")
        website = str(res.get("website") or "")
        if rid and website:
            resource_website_cache[rid] = website

    text, kb = format_resource_list(resources, media_type, provider_filter="115")
    await wait_msg.edit_text(text, reply_markup=kb, parse_mode="HTML")
    resource_list_state[wait_msg.message_id] = {
        "resources": resources,
        "media_type": media_type,
        "title": None,
    }


async def handle_keyword_search(message: Message, keyword: str, search_type: str):
    """
    处理关键词搜索
    
    Args:
        message: 消息对象
        keyword: 搜索关键词
        search_type: 'tv' 或 'movie'
    """
    type_name = "📺 剧集" if search_type == "tv" else "🎬 电影"
    media_type = "tv" if search_type == "tv" else "movie"
    wait_msg = await message.reply(f"🔍 搜索中 · {keyword}", parse_mode="HTML")
    
    try:
        start_ts = time.monotonic()
        tmdb_info = None
        resources = None

        tmdb_result = await search_tmdb(keyword, media_type)

        if not tmdb_result:
            await wait_msg.edit_text(format_error_message('no_results'), parse_mode="HTML")
            return

        # 检查是否返回的是列表(多个结果)
        if isinstance(tmdb_result, list):
            # 多个搜索结果，让用户选择
            await wait_msg.delete()

            result_text, kb = build_tmdb_candidate_message(tmdb_result, page=0)
            sent_msg = await message.reply(result_text, reply_markup=kb, parse_mode="HTML")
            tmdb_search_state[sent_msg.message_id] = {
                "results": tmdb_result,
                "page": 0,
            }
            elapsed = int((time.monotonic() - start_ts) * 1000)
            logging.info("✅ TMDB多结果返回: keyword=%s type=%s count=%s cost_ms=%s", keyword, search_type, len(tmdb_result), elapsed)
            return

        # 单个结果兼容路径：如果调用方未来传了 dict，这里仍可继续工作
        tmdb_info = tmdb_result
        tmdb_id = tmdb_result["tmdb_id"]
        result_type = tmdb_result["media_type"]
        title = tmdb_result["title"]

        # 发送TMDB信息（图片+简介）
        if tmdb_info.get("poster_url"):
            info_text = format_tmdb_info(tmdb_info)
            try:
                await message.reply_photo(
                    photo=tmdb_info["poster_url"],
                    caption=info_text,
                    parse_mode="HTML"
                )
            except Exception:
                await message.reply(info_text, parse_mode="HTML")

        # 获取资源列表
        logging.info(f"✅ TMDB匹配成功，获取资源: {title} (ID: {tmdb_id})")
        resources = await get_resources_by_tmdb_id(tmdb_id, result_type)
        
        if not resources:
            error_msg = format_error_message('no_results')
            if tmdb_info:
                await message.reply(error_msg, parse_mode="HTML")
            else:
                await wait_msg.edit_text(error_msg, parse_mode="HTML")
            return

        for res in resources:
            rid = str(res.get("id") or "")
            website = str(res.get("website") or "")
            if rid and website:
                resource_website_cache[rid] = website
        
        # 格式化资源列表
        text, kb = format_resource_list(resources, media_type, provider_filter="115")
        
        # 发送资源列表
        if tmdb_info:
            sent_msg = await message.reply(text, reply_markup=kb, parse_mode="HTML")
            resource_list_state[sent_msg.message_id] = {
                "resources": resources,
                "media_type": media_type,
                "title": None,
            }
        else:
            await wait_msg.edit_text(text, reply_markup=kb, parse_mode="HTML")
            resource_list_state[wait_msg.message_id] = {
                "resources": resources,
                "media_type": media_type,
                "title": None,
            }

        elapsed = int((time.monotonic() - start_ts) * 1000)
        logging.info("✅ 关键词搜索完成: keyword=%s type=%s resources=%s cost_ms=%s", keyword, search_type, len(resources), elapsed)
            
    except Exception as e:
        logging.error(f"❌ 搜索出错: {e}")
        await wait_msg.edit_text(f"❌ 运行出错: {e}", parse_mode="HTML")


# ==================== 回调查询处理器 ====================

@router.callback_query(F.data.startswith("pf:"))
async def callback_provider_filter(callback: CallbackQuery):
    """切换资源网盘筛选（常驻按钮）"""
    await callback.answer()
    msg = callback.message
    if not msg:
        return

    state = resource_list_state.get(msg.message_id)
    if not state:
        await callback.answer("列表已过期，请重新搜索", show_alert=True)
        return

    provider = (callback.data or "pf:115").split(":", 1)[1]
    text, kb = format_resource_list(
        state["resources"],
        state["media_type"],
        title=state.get("title"),
        provider_filter=provider,
    )
    await msg.edit_text(text, reply_markup=kb, parse_mode="HTML")


@router.callback_query(F.data.startswith("tmdb_page:"))
async def callback_tmdb_page(callback: CallbackQuery):
    """切换 TMDB 候选列表分页。"""
    msg = callback.message
    if not msg:
        await callback.answer()
        return

    state = tmdb_search_state.get(msg.message_id)
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
    text, kb = build_tmdb_candidate_message(state["results"], page=page)
    await msg.edit_text(text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()

@router.callback_query(F.data.regexp(r"^(movie|tv)_\d+:"))
async def callback_get_resource(callback: CallbackQuery):
    """
    处理资源选择回调（点击数字按钮）
    
    Callback data 格式: tv_1:resource_id 或 movie_1:resource_id
    """
    await callback.answer()
    
    try:
        user_id = callback.from_user.id
        
        # 解析回调数据
        data_parts = callback.data.split(":")
        if len(data_parts) != 2:
            await callback.message.answer("❌ 数据格式错误")
            return
        
        resource_id = data_parts[1]
        
        # 更新消息显示提取中状态
        await callback.message.edit_text(
            f"⏳ 正在提取链接...\n🆔 <code>{resource_id}</code>",
            parse_mode="HTML"
        )
        
        # 提取链接（使用会话管理）
        result = await fetch_download_link(resource_id, user_id=user_id, keep_session=True)
        website = (result or {}).get("website") or resource_website_cache.get(resource_id, "")
        
        if result and result.get("need_unlock"):
            # 需要解锁
            points = result["points"]
            
            # 检查是否自动解锁
            if AUTO_UNLOCK_THRESHOLD > 0 and points <= AUTO_UNLOCK_THRESHOLD:
                # 自动解锁
                logging.info(f"🤖 自动解锁: {points} 积分 <= {AUTO_UNLOCK_THRESHOLD} 积分阈值")
                await callback.message.edit_text(
                    f"🤖 <b>自动解锁中...</b>\n\n"
                    f"🆔 <code>{resource_id}</code>\n"
                    f"💰 消耗积分: <code>{points}</code>",
                    parse_mode="HTML"
                )
                
                # 执行解锁
                unlock_result = await unlock_and_fetch(resource_id, user_id=user_id)
                
                if unlock_result and unlock_result.get("link"):
                    await handle_link_extracted(
                        callback.message,
                        unlock_result["link"],
                        unlock_result.get("code", "无"),
                        auto_unlock=True,
                        website=unlock_result.get("website") or website,
                    )
                else:
                    await notify_auto_unlock_failed(callback.message, callback.message)
                return
            
            user_points = await get_user_points()

            if user_points is not None and user_points < points:
                # 积分不足，关闭会话
                session_id = result.get("session_id")
                if session_id:
                    await session_manager.close_session(session_id)
                
                await callback.message.edit_text(
                    format_error_message('insufficient_points',
                        f"需要: <code>{points}</code> 积分\n"
                        f"当前: <code>{user_points}</code> 积分\n"
                        f"缺少: <code>{points - user_points}</code> 积分"),
                    parse_mode="HTML"
                )
                return
            
            # 询问是否解锁（会话保持打开）
            text, kb = format_unlock_confirmation(resource_id, points, user_points)
            await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
            
        elif result and result.get("link"):
            # 成功提取链接
            await handle_link_extracted(
                callback.message,
                result["link"],
                result.get("code", "无"),
                website=result.get("website") or website,
            )
            return
        else:
            await callback.message.edit_text(
                format_error_message('fetch_failed'),
                parse_mode="HTML"
            )
            
    except Exception as e:
        logging.error(f"❌ 回调处理出错: {e}")
        await callback.message.edit_text(f"❌ 处理出错: {e}", parse_mode="HTML")


@router.callback_query(F.data.startswith("unlock:"))
async def callback_unlock_resource(callback: CallbackQuery):
    """
    处理解锁确认回调
    
    Callback data 格式: unlock:resource_id
    """
    await callback.answer("🔓 正在解锁...")
    
    try:
        user_id = callback.from_user.id
        resource_id = callback.data.split(":")[1]
        
        # 更新消息
        await callback.message.edit_text(
            f"🔓 <b>正在解锁资源...</b>\n\n🆔 <code>{resource_id}</code>",
            parse_mode="HTML"
        )
        
        # 执行解锁（使用已有会话）
        result = await unlock_and_fetch(resource_id, user_id=user_id)
        
        if result and result.get("link"):
            await handle_link_extracted(
                callback.message,
                result["link"],
                result.get("code", "无"),
                website=result.get("website") or resource_website_cache.get(resource_id, ""),
            )
        else:
            await callback.message.edit_text(
                "❌ 解锁失败，请稍后重试",
                parse_mode="HTML"
            )
            
    except Exception as e:
        logging.error(f"❌ 解锁出错: {e}")
        await callback.message.edit_text(f"❌ 解锁出错: {e}", parse_mode="HTML")


@router.callback_query(F.data == "cancel_unlock")
async def callback_cancel_unlock(callback: CallbackQuery):
    """处理取消解锁回调"""
    await callback.answer("已取消")
    
    # 关闭会话
    user_id = callback.from_user.id
    # 从消息中提取 resource_id
    import re
    match = re.search(r'🆔\s*<code>([a-f0-9-]+)</code>', callback.message.text or "")
    if match:
        resource_id = match.group(1)
        session_id = f"{user_id}:{resource_id}"
        await session_manager.close_session(session_id)
        logging.info(f"🚫 用户取消解锁，已关闭会话: {session_id}")
    
    await callback.message.edit_text("❌ 已取消解锁", parse_mode="HTML")


async def auto_add_to_sa(message_id: int, link: str, original_message: Message, countdown: int = 60):
    """
    倒计时自动添加到SA
    
    Args:
        message_id: 消息ID
        link: 115链接
        original_message: 原始消息对象
        countdown: 倒计时秒数（默认60秒）
    """
    try:
        if not SA_ENABLE_115_PUSH:
            logging.info("⏭️ 已禁用115推送到SA，跳过自动添加: %s", link)
            return

        if not is_115_share_link(link):
            logging.info("⏭️ 跳过自动添加到SA（非115链接）: %s", link)
            return

        # 等待倒计时（每10秒更新一次）
        step = 10 if countdown > 10 else max(1, countdown)
        for remaining in range(countdown, 0, -step):
            # 检查是否被取消
            if message_id in pending_sa_tasks and pending_sa_tasks[message_id].get("cancelled"):
                logging.info(f"⏹️ 用户取消了自动添加: {link}")
                return
            
            # 更新倒计时显示
            try:
                await original_message.edit_text(
                    f"✅ <b>提取成功</b>\n\n"
                    f"<blockquote>\n"
                    f"🔗 <a href='{link}'>115网盘链接</a>\n"
                    f"</blockquote>\n\n"
                    f"⏱️ 将在 {remaining} 秒后自动添加到 Symedia\n"
                    f"💡 不需要的话发送 /hdc 取消",
                    parse_mode="HTML",
                    disable_web_page_preview=True
                )
            except:
                pass
            
            await asyncio.sleep(step if remaining > step else remaining)
        
        # 倒计时结束，检查是否被取消
        if message_id in pending_sa_tasks and pending_sa_tasks[message_id].get("cancelled"):
            return
        
        # 执行添加到SA
        logging.info(f"⏰ 倒计时结束，自动添加到SA: {link}")
        
        # 构建API URL
        api_url = f"{SA_URL}/api/v1/plugin/cloud_helper/add_share_urls_115"
        
        # 构建请求体
        payload = {
            "urls": [link],
            "parent_id": SA_PARENT_ID
        }
        
        # 发送POST请求
        async with aiohttp.ClientSession() as session:
            async with session.post(api_url, params={"token": SA_TOKEN}, json=payload) as response:
                if response.status == 200:
                    data = await response.json()
                    message = data.get("message", "添加成功")
                    
                    # 通知用户成功
                    notification = (
                        "🎬 <b>已自动添加到Symedia</b>\n"
                        "━━━━━━━━━━━━━━━━\n\n"
                        f"📊 <b>状态:</b> {message}\n"
                        f"🔗 <b>链接:</b> <code>{link}</code>\n"
                        f"📁 <b>目录ID:</b> <code>{SA_PARENT_ID}</code>\n"
                        f"⏰ <b>方式:</b> 自动添加"
                    )
                    
                    await original_message.edit_text(notification, parse_mode="HTML")
                    
                    logging.info(f"✅ 自动添加到SA成功: {link} - {message}")
                else:
                    error_text = await response.text()
                    logging.error(f"❌ SA API返回错误: {response.status} - {error_text}")
                    await original_message.edit_text(
                        f"❌ 自动添加到SA失败: HTTP {response.status}",
                        parse_mode="HTML"
                    )
        
    except asyncio.CancelledError:
        logging.info(f"⏹️ 自动添加任务被取消: {link}")
    except Exception as e:
        logging.error(f"❌ 自动添加到SA失败: {e}")
        try:
            await original_message.edit_text(
                f"❌ 自动添加失败: {str(e)}",
                parse_mode="HTML"
            )
        except:
            pass
    finally:
        # 清理任务
        if message_id in pending_sa_tasks:
            del pending_sa_tasks[message_id]


@router.callback_query(F.data.startswith("send_to_group:"))
async def callback_send_to_sa(callback: CallbackQuery):
    """
    发送115链接到SA（Symedia）
    
    Callback data 格式: send_to_group:115_link
    """
    try:
        # 提取链接
        link = callback.data.replace("send_to_group:", "")

        if not is_115_share_link(link):
            await callback.answer("❌ 仅支持115链接添加到Symedia", show_alert=True)
            return
        
        # 检查是否配置了SA
        if not SA_ENABLE_115_PUSH:
            await callback.answer("⚠️ 已禁用115推送到Symedia", show_alert=True)
            return

        if not SA_URL or not SA_PARENT_ID:
            await callback.answer("❌ 未配置SA，无法添加到Symedia", show_alert=True)
            return
        
        # 构建API URL
        api_url = f"{SA_URL}/api/v1/plugin/cloud_helper/add_share_urls_115"
        
        # 构建请求体
        payload = {
            "urls": [link],
            "parent_id": SA_PARENT_ID
        }
        
        # 显示处理状态
        await callback.answer("⏳ 正在添加到Symedia...", show_alert=False)
        
        # 发送POST请求
        async with aiohttp.ClientSession() as session:
            async with session.post(api_url, params={"token": SA_TOKEN}, json=payload) as response:
                if response.status == 200:
                    data = await response.json()
                    message = data.get("message", "添加成功")
                    
                    # 通知用户成功
                    await callback.answer(f"✅ {message}", show_alert=True)
                    
                    # 发送详细通知消息
                    notification = (
                        "🎬 <b>已添加到Symedia</b>\n"
                        "━━━━━━━━━━━━━━━━\n\n"
                        f"📊 <b>状态:</b> {message}\n"
                        f"🔗 <b>链接:</b> <code>{link}</code>\n"
                        f"📁 <b>目录ID:</b> <code>{SA_PARENT_ID}</code>"
                    )
                    
                    await callback.message.reply(notification, parse_mode="HTML")
                    
                    # 移除按钮
                    await callback.message.edit_reply_markup(reply_markup=None)
                    
                    logging.info(f"✅ 成功添加到SA: {link} - {message}")
                else:
                    error_text = await response.text()
                    logging.error(f"❌ SA API返回错误: {response.status} - {error_text}")
                    await callback.answer(f"❌ 添加失败: HTTP {response.status}", show_alert=True)
        
    except aiohttp.ClientError as e:
        logging.error(f"❌ 网络请求失败: {e}")
        await callback.answer("❌ 网络请求失败，请检查SA配置", show_alert=True)
    except Exception as e:
        logging.error(f"❌ 添加到SA失败: {e}")
        await callback.answer(f"❌ 添加失败: {str(e)}", show_alert=True)


# ==================== 直接链接处理（放在所有Command之后）====================

@router.message(F.text | F.photo | F.video | F.document)
async def handle_direct_link(message: Message):
    """
    处理直接发送的链接（无需命令）- 从 entities 提取，支持图片/视频的caption
    """
    # 权限检查
    if not await check_user_permission(message):
        return
    
    # 获取文本内容 (text 或 caption)
    text = message.text or message.caption
    
    if not text:
        return
    
    # 如果是命令，跳过（由其他handler处理）
    if text.startswith('/'):
        return
    
    # 获取 entities (text 或 caption_entities)
    entities = message.entities or message.caption_entities
    
    # 从 entities 中提取所有链接
    urls = []
    if entities:
        for entity in entities:
            # url: 纯文本URL
            # text_link: 超链接(文字背后的URL)
            if entity.type == "url":
                # 从消息文本中提取URL
                url = text[entity.offset:entity.offset + entity.length]
                urls.append(url)
            elif entity.type == "text_link":
                # 直接从entity.url获取
                urls.append(entity.url)
    
    # 如果没有找到任何链接，忽略
    if not urls:
        return
    
    # 过滤出 hdhive.com 链接
    hdhive_urls = [url for url in urls if 'hdhive.com' in url]
    
    if not hdhive_urls:
        return
    
    logging.info(f"📎 从消息中提取到 {len(hdhive_urls)} 个HDHive链接: {hdhive_urls}")
    
    # 优先处理resource/115链接，其次resource链接（即使消息中有其他链接）
    resource_url = None
    for url in hdhive_urls:
        if '/resource/115/' in url:
            resource_url = url
            break
    if not resource_url:
        for url in hdhive_urls:
            if '/resource/' in url:
                resource_url = url
                break
    
    if resource_url:
        user_id = message.from_user.id
        
        # 从URL中提取resource ID
        resource_match = re.search(r'/resource/(?:115/)?([a-f0-9-]+)', resource_url)
        if resource_match:
            resource_id = resource_match.group(1)
        else:
            await message.reply(
                "❌ 资源链接格式不正确，无法解析资源ID",
                parse_mode="HTML"
            )
            return
        
        wait_msg = await message.reply(
            f"🔗 <b>检测到资源链接</b>\n\n"
            f"🆔 <code>{resource_id}</code>\n"
            f"⏳ 正在提取链接...",
            parse_mode="HTML"
        )
        
        # 使用会话管理
        result = await fetch_download_link(
            resource_id,
            user_id=user_id,
            keep_session=True,
            start_url=resource_url,
        )
        website = (result or {}).get("website") or resource_website_cache.get(resource_id, "")
        
        if result and result.get("need_unlock"):
            # 需要解锁
            points = result["points"]
            
            # 检查是否自动解锁
            if AUTO_UNLOCK_THRESHOLD > 0 and points <= AUTO_UNLOCK_THRESHOLD:
                # 自动解锁
                logging.info(f"🤖 自动解锁: {points} 积分 <= {AUTO_UNLOCK_THRESHOLD} 积分阈值")
                await wait_msg.edit_text(
                    f"🤖 <b>自动解锁中...</b>\n\n"
                    f"🆔 <code>{resource_id}</code>\n"
                    f"💰 消耗积分: <code>{points}</code>",
                    parse_mode="HTML"
                )
                
                # 执行解锁
                unlock_result = await unlock_and_fetch(resource_id, user_id=user_id)
                
                if unlock_result and unlock_result.get("link"):
                    # 使用统一的处理函数
                    await handle_link_extracted(
                        wait_msg,
                        unlock_result["link"],
                        unlock_result.get("code", "无"),
                        auto_unlock=True,
                        website=unlock_result.get("website") or website,
                    )
                else:
                    await notify_auto_unlock_failed(wait_msg, message)
                return
            
            user_points = await get_user_points()

            if user_points is not None and user_points < points:
                # 积分不足，关闭会话
                session_id = result.get("session_id")
                if session_id:
                    await session_manager.close_session(session_id)
                
                await wait_msg.edit_text(
                    f"❌ <b>积分不足</b>\n\n"
                    f"需要: <code>{points}</code> 积分\n"
                    f"当前: <code>{user_points}</code> 积分\n"
                    f"缺少: <code>{points - user_points}</code> 积分",
                    parse_mode="HTML"
                )
                return
            
            # 询问是否解锁（会话保持打开）
            text, kb = format_unlock_confirmation(resource_id, points, user_points)
            await wait_msg.edit_text(text, reply_markup=kb, parse_mode="HTML")
            return
        
        elif result and result.get("link"):
            # 成功提取链接
            link = result["link"]
            code = result.get("code", "无")
            
            # 使用统一的处理函数
            await handle_link_extracted(
                wait_msg,
                link,
                code,
                auto_unlock=False,
                website=result.get("website") or website,
            )
            return
        
        # 已处理资源链接，不再处理其他链接
        return
    
    # 处理TMDB链接（tv/movie）- 从提取的URL中查找
    tmdb_url = None
    for url in hdhive_urls:
        if '/movie/' in url or '/tv/' in url:
            tmdb_url = url
            break
    
    if tmdb_url:
        # 解析TMDB链接
        link_info = parse_hdhive_link(tmdb_url)
        if link_info["type"] == "tmdb":
            tmdb_id = link_info["id"]
            media_type = link_info["media_type"]
            type_name = "🎬 电影" if media_type == "movie" else "📺 剧集"
            
            wait_msg = await message.reply(
                f"🔗 <b>检测到{type_name}页面</b>\n\n"
                f"🆔 TMDB ID: <code>{tmdb_id}</code>\n"
                f"⏳ 正在获取资源列表...",
                parse_mode="HTML"
            )
            
            resources = await get_resources_by_tmdb_id(tmdb_id, media_type)
            
            if not resources:
                await wait_msg.edit_text(f"❌ 该{type_name}暂无资源", parse_mode="HTML")
                return

            for res in resources:
                rid = str(res.get("id") or "")
                website = str(res.get("website") or "")
                if rid and website:
                    resource_website_cache[rid] = website
            
            text, kb = format_resource_list(resources, media_type, provider_filter="115")
            await wait_msg.edit_text(text, reply_markup=kb, parse_mode="HTML")
            resource_list_state[wait_msg.message_id] = {
                "resources": resources,
                "media_type": media_type,
                "title": None,
            }
            return


@router.callback_query(F.data.startswith("select_tmdb:"))
async def callback_select_tmdb(callback: CallbackQuery):
    """
    处理用户选择TMDB搜索结果
    
    Callback data 格式: select_tmdb:tmdb_id:media_type
    """
    try:
        msg = callback.message
        if not msg:
            await callback.answer()
            return

        tmdb_info = None
        state = tmdb_search_state.get(msg.message_id)

        if state:
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
            tmdb_search_state.pop(msg.message_id, None)
        else:
            # 兼容旧格式: select_tmdb:12345:movie
            parts = (callback.data or "").split(":")
            if len(parts) != 3:
                await callback.answer("列表已过期，请重新搜索", show_alert=True)
                return
            tmdb_id = parts[1]
            media_type = parts[2]
        
        # 显示加载状态
        await callback.message.edit_text("⏳ 正在获取TMDB信息...", parse_mode="HTML")
        
        # 获取TMDB详细信息
        tmdb_info = await get_tmdb_details(int(tmdb_id), media_type) or tmdb_info
        
        if tmdb_info:
            # 构建TMDB信息文本
            info_text = f"<b>{tmdb_info['title']}</b>\n"
            if tmdb_info.get("release_date"):
                info_text += f"{tmdb_info['release_date']}"
            if tmdb_info.get("rating"):
                info_text += f" · ⭐️ {tmdb_info['rating']:.1f}\n"
            else:
                info_text += "\n"
            
            # 简介长度限制
            overview = tmdb_info.get('overview', '暂无简介')
            if len(overview) > 200:
                overview = overview[:200] + "…"
            
            info_text += f"\n{overview}\n\n"
            info_text += "正在获取资源…"
            
            # 如果有海报图片，发送图片+文字
            if tmdb_info.get("poster_url"):
                try:
                    await callback.message.answer_photo(
                        photo=tmdb_info["poster_url"],
                        caption=info_text,
                        parse_mode="HTML"
                    )
                    await callback.message.delete()
                except Exception as e:
                    logging.warning(f"发送图片失败: {e}")
                    await callback.message.edit_text(info_text, parse_mode="HTML")
            else:
                await callback.message.edit_text(info_text, parse_mode="HTML")
        else:
            # 如果获取TMDB信息失败，继续显示加载状态
            await callback.message.edit_text("⏳ 正在获取资源...", parse_mode="HTML")
        
        # 获取资源列表
        resources = await get_resources_by_tmdb_id(int(tmdb_id), media_type)
        
        if not resources:
            # 如果有TMDB信息，在新消息中显示未找到资源
            if tmdb_info:
                await callback.message.answer("❌ 未找到资源", parse_mode="HTML")
            else:
                await callback.message.edit_text("❌ 未找到资源", parse_mode="HTML")
            return

        for res in resources:
            rid = str(res.get("id") or "")
            website = str(res.get("website") or "")
            if rid and website:
                resource_website_cache[rid] = website
        
        text, kb = format_resource_list(resources, media_type, provider_filter="115")

        # 如果已经发送了TMDB信息，在新消息中显示资源列表
        if tmdb_info and tmdb_info.get("poster_url"):
            sent_msg = await callback.message.answer(text, reply_markup=kb, parse_mode="HTML")
            resource_list_state[sent_msg.message_id] = {
                "resources": resources,
                "media_type": media_type,
                "title": None,
            }
        else:
            await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
            resource_list_state[callback.message.message_id] = {
                "resources": resources,
                "media_type": media_type,
                "title": None,
            }
        
    except Exception as e:
        logging.error(f"❌ 处理TMDB选择失败: {e}")
        await callback.message.edit_text(f"❌ 处理失败: {e}", parse_mode="HTML")
