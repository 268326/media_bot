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
    HDHIVE_PARSE_INCOMING_LINKS,
)
from ass_service import (
    ass_service,
    ASS_MENU_PREFIX,
    ASS_MUX_PREFIX,
)
from danmu_service import fetch_bilibili_danmaku_xml, DanmuError
from checkin_service import daily_check_in
from utils import parse_hdhive_link, detect_share_provider, is_115_share_link, detect_provider_by_website
from hdhive_client import (
    get_resources_by_tmdb_id,
    fetch_download_link, 
    unlock_resource,
    unlock_and_fetch,
    get_user_points
)
from hdhive_unlock_service import UnlockQueueNotice
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
from strm_service import strm_service
from strm_prune_service import strm_prune_service

# 创建路由器
router = Router()

# 待添加到SA的任务字典 {"chat_id:message_id": {"link": str, "task": asyncio.Task, "cancelled": bool, "user_id": int|None, "created_at": float}}
pending_sa_tasks: dict[str, dict] = {}
# 待确认的 STRM 清理任务 {"chat_id:message_id": {"user_id": int, "created_at": float, "preview_text": str}}
rm_strm_pending_confirms: dict[str, dict] = {}
RM_STRM_CONFIRM_TTL = 900
RESOURCE_WEBSITE_CACHE_LIMIT = 2048
RESOURCE_LIST_STATE_LIMIT = 256
TMDB_SEARCH_STATE_LIMIT = 256
# 资源网盘类型缓存：resource_id -> website
resource_website_cache: dict[str, str] = {}
# 资源列表状态缓存："chat_id:message_id" -> {"resources": list, "media_type": str, "title": str|None}
resource_list_state: dict[str, dict] = {}
# TMDB 候选列表状态缓存："chat_id:message_id" -> {"results": list[dict], "page": int}
tmdb_search_state: dict[str, dict] = {}

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


def trim_dict_cache(cache: dict, limit: int) -> None:
    while len(cache) > limit:
        oldest_key = next(iter(cache))
        cache.pop(oldest_key, None)


def make_message_state_key(message: Message | None) -> str | None:
    if not message:
        return None
    chat = getattr(message, "chat", None)
    if not chat:
        return None
    return f"{chat.id}:{message.message_id}"


def format_duration_compact(seconds: float) -> str:
    """把秒数格式化为适合 TG 提示的紧凑文本。"""
    total = max(0, int(round(seconds)))
    minutes, sec = divmod(total, 60)
    hours, minutes = divmod(minutes, 60)
    if hours > 0:
        return f"{hours}小时{minutes}分{sec}秒"
    if minutes > 0:
        return f"{minutes}分{sec}秒"
    return f"{sec}秒"


async def notify_auto_unlock_failed(target_msg: Message, fallback_msg: Message):
    """通知自动解锁失败（必要时回退发送新消息）"""
    try:
        await target_msg.edit_text("❌ 自动解锁失败，请稍后重试", parse_mode="HTML")
    except Exception:
        await fallback_msg.reply("❌ 自动解锁失败，请稍后重试", parse_mode="HTML")


async def update_unlock_queue_notice(
    wait_msg: Message,
    notice: UnlockQueueNotice,
    *,
    auto_unlock: bool,
    title: str | None = None,
    tip: str | None = None,
):
    """在 Telegram 消息中展示解锁排队/限速等待状态。"""
    mode_text = title or ("自动解锁排队中" if auto_unlock else "解锁排队中")
    footer_tip = tip or "💡 已触发 HDHive API 解锁限速，正在排队等待"
    ahead_text = (
        f"前面还有 <code>{notice.ahead_count}</code> 个请求"
        if notice.ahead_count > 0
        else "你已在队列最前面，等待下一个可用速率窗口"
    )
    queued_text = format_duration_compact(notice.queued_seconds)
    wait_text = format_duration_compact(notice.wait_seconds)
    try:
        await wait_msg.edit_text(
            f"⏳ <b>{mode_text}...</b>\n\n"
            f"🆔 <code>{notice.resource_id}</code>\n"
            f"📍 当前队列位置: <code>{notice.queue_position}</code>\n"
            f"👥 {ahead_text}\n"
            f"⌛ 已累计等待: <code>{queued_text}</code>\n"
            f"⏱️ 预计至少还要等: <code>{wait_text}</code>\n"
            f"🚦 限速: <code>{notice.rate_limit_per_second}</code> 次/秒\n\n"
            f"{footer_tip}",
            parse_mode="HTML",
        )
    except Exception as exc:
        logging.debug("更新解锁排队提示失败: %s", exc)


def build_unlock_wait_callback(
    wait_msg: Message,
    *,
    auto_unlock: bool,
    title: str | None = None,
    tip: str | None = None,
):
    async def _wait_callback(notice: UnlockQueueNotice):
        await update_unlock_queue_notice(
            wait_msg,
            notice,
            auto_unlock=auto_unlock,
            title=title,
            tip=tip,
        )

    return _wait_callback


async def perform_unlock_and_handle_result(
    *,
    wait_msg: Message,
    fallback_msg: Message,
    resource_id: str,
    user_id: int,
    auto_unlock: bool,
    website: str,
) -> bool:
    """统一执行解锁并处理结果展示。"""
    if auto_unlock:
        await wait_msg.edit_text(
            f"🤖 <b>自动解锁中...</b>\n\n"
            f"🆔 <code>{resource_id}</code>\n"
            f"💡 请求已提交到解锁队列",
            parse_mode="HTML"
        )
    else:
        await wait_msg.edit_text(
            f"🔓 <b>正在解锁资源...</b>\n\n"
            f"🆔 <code>{resource_id}</code>\n"
            f"💡 请求已提交到解锁队列",
            parse_mode="HTML"
        )

    wait_callback = build_unlock_wait_callback(
        wait_msg,
        auto_unlock=auto_unlock,
    )

    try:
        result = await unlock_and_fetch(
            resource_id,
            user_id=user_id,
            wait_callback=wait_callback,
        )
    except Exception as exc:
        logging.error("❌ 解锁出错: %s", exc)
        error_text = f"❌ {'自动' if auto_unlock else ''}解锁失败，请稍后重试"
        try:
            await wait_msg.edit_text(error_text, parse_mode="HTML")
        except Exception:
            await fallback_msg.reply(error_text, parse_mode="HTML")
        return False

    if result and result.get("link"):
        await handle_link_extracted(
            wait_msg,
            result["link"],
            result.get("code", "无"),
            auto_unlock=auto_unlock,
            website=result.get("website") or website,
            requester_user_id=user_id,
        )
        return True

    if auto_unlock:
        await notify_auto_unlock_failed(wait_msg, fallback_msg)
    else:
        try:
            await wait_msg.edit_text("❌ 解锁失败，请稍后重试", parse_mode="HTML")
        except Exception:
            await fallback_msg.reply("❌ 解锁失败，请稍后重试", parse_mode="HTML")
    return False


async def handle_unlock_required(
    *,
    wait_msg: Message,
    fallback_msg: Message,
    resource_id: str,
    user_id: int,
    result: dict,
    website: str,
) -> bool:
    """统一处理需要解锁的资源。返回 True 表示流程已结束。"""
    points = result["points"]

    if AUTO_UNLOCK_THRESHOLD > 0 and points <= AUTO_UNLOCK_THRESHOLD:
        logging.info(f"🤖 自动解锁: {points} 积分 <= {AUTO_UNLOCK_THRESHOLD} 积分阈值")
        await perform_unlock_and_handle_result(
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
        session_id = result.get("session_id")
        if session_id:
            await session_manager.close_session(session_id)

        await wait_msg.edit_text(
            format_error_message(
                'insufficient_points',
                f"需要: <code>{points}</code> 积分\n"
                f"当前: <code>{user_points}</code> 积分\n"
                f"缺少: <code>{points - user_points}</code> 积分"
            ),
            parse_mode="HTML"
        )
        return True

    text, kb = format_unlock_confirmation(resource_id, points, user_points)
    await wait_msg.edit_text(text, reply_markup=kb, parse_mode="HTML")
    return True


async def fetch_download_link_and_handle_result(
    *,
    wait_msg: Message,
    fallback_msg: Message,
    resource_id: str,
    user_id: int,
    website: str,
):
    """统一处理无需确认的提取链路（免费/已解锁资源也走解锁队列）。"""
    wait_callback = build_unlock_wait_callback(
        wait_msg,
        auto_unlock=False,
        title="提取排队中",
        tip="💡 HDHive 提取底层也会调用解锁接口，当前正在按限速队列等待",
    )

    try:
        result = await unlock_resource(
            resource_id,
            user_id=user_id,
            wait_callback=wait_callback,
        )
    except Exception as exc:
        logging.error("❌ 提取链接出错: %s", exc)
        await wait_msg.edit_text(
            format_error_message('fetch_failed'),
            parse_mode="HTML"
        )
        return False

    link = str((result or {}).get("full_url") or (result or {}).get("url") or "").strip()
    if not link:
        await wait_msg.edit_text(
            format_error_message('fetch_failed'),
            parse_mode="HTML"
        )
        return False

    await handle_link_extracted(
        wait_msg,
        link,
        (result or {}).get("access_code") or "无",
        auto_unlock=False,
        website=website,
        requester_user_id=user_id,
    )
    return True


async def handle_link_extracted(
    wait_msg: Message,
    link: str,
    code: str = "无",
    auto_unlock: bool = False,
    website: str | None = None,
    requester_user_id: int | None = None,
):
    """
    统一处理链接提取成功后的逻辑
    
    Args:
        wait_msg: 等待消息对象
        link: 115链接
        code: 提取码
        auto_unlock: 是否是自动解锁
        requester_user_id: 触发本次提取的用户 ID
    """
    provider_key, provider_name = detect_provider_by_website(website)
    if provider_key == "unknown":
        provider_key, provider_name = detect_share_provider(link)
    button_text = f"🔗 打开{provider_name}"

    # 仅 115 链接支持自动添加到 SA
    can_auto_add_sa = SA_URL and SA_PARENT_ID and SA_ENABLE_115_PUSH and provider_key == "115"

    if can_auto_add_sa:
        message_id = wait_msg.message_id
        task_key = make_message_state_key(wait_msg)
        if not task_key:
            await wait_msg.edit_text("❌ 无法记录自动添加状态，请稍后重试", parse_mode="HTML")
            return
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
        task = asyncio.create_task(auto_add_to_sa(task_key, link, wait_msg, countdown=SA_AUTO_ADD_DELAY))
        pending_sa_tasks[task_key] = {
            "link": link,
            "task": task,
            "cancelled": False,
            "user_id": requester_user_id,
            "created_at": time.time(),
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


def resolve_message_owner_id(message: Message | None) -> int | None:
    current = message
    for _ in range(3):
        if not current:
            return None
        from_user = getattr(current, "from_user", None)
        if from_user and not getattr(from_user, "is_bot", False):
            return from_user.id
        current = getattr(current, "reply_to_message", None)
    return None


async def check_callback_permission(callback: CallbackQuery) -> bool:
    """检查回调操作者是否有权限。"""
    user_id = callback.from_user.id

    if ALLOWED_USER_ID != 0:
        if user_id != ALLOWED_USER_ID:
            await callback.answer("⛔️ 权限不足", show_alert=True)
            logging.warning(f"❌ 用户 {user_id} ({callback.from_user.username}) 尝试点击受限按钮但被拒绝")
            return False
        return True

    owner_user_id = resolve_message_owner_id(callback.message)
    if owner_user_id and owner_user_id != user_id:
        await callback.answer("⛔️ 只能由发起人操作", show_alert=True)
        logging.warning(
            "❌ 用户 %s (%s) 尝试点击他人会话按钮，owner=%s",
            user_id,
            callback.from_user.username,
            owner_user_id,
        )
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


@router.message(Command("ass"))
async def cmd_ass(message: Message):
    if not await check_user_permission(message):
        return

    text, kb = await ass_service.build_mux_menu(message)
    await message.reply(text, reply_markup=kb, parse_mode="HTML")


async def sync_ass_mux_view(bot, chat_id: int, user_id: int):
    session = ass_service.get_mux_session(chat_id, user_id)
    if not session:
        return

    panel_text = await ass_service.build_mux_panel_text(chat_id, user_id)
    panel_kb = ass_service.build_mux_plan_keyboard(chat_id, user_id)
    preview_text = await ass_service.build_mux_preview_text(chat_id, user_id)

    if session.awaiting_message_id:
        try:
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=session.awaiting_message_id,
                text=panel_text,
                reply_markup=panel_kb,
                parse_mode="HTML",
            )
        except Exception:
            pass

    if session.preview_message_id:
        try:
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=session.preview_message_id,
                text=preview_text,
                parse_mode="HTML",
            )
        except Exception:
            pass


@router.message(Command("strm_status"))
async def cmd_strm_status(message: Message):
    if not await check_user_permission(message):
        return

    st = strm_service.status()
    text = (
        "🧩 <b>STRM 监控状态</b>\n\n"
        f"启用: <code>{st['enabled']}</code>\n"
        f"已启动: <code>{st['started']}</code>\n"
        f"运行中: <code>{st['running']}</code>\n"
        f"WATCH_DIR: <code>{st['watch_dir'] or '-'}</code>\n"
        f"DONE_DIR: <code>{st['done_dir'] or '-'}</code>\n"
        f"FAILED_DIR: <code>{st['failed_dir'] or '-'}</code>\n"
        f"LAST_ERROR: <code>{st['last_error'] or '-'}</code>"
    )
    await message.reply(text, parse_mode="HTML")


@router.message(Command("strm_scan"))
async def cmd_strm_scan(message: Message):
    if not await check_user_permission(message):
        return

    wait_msg = await message.reply("🔄 正在触发 STRM 手动重扫…", parse_mode="HTML")
    result = await strm_service.scan()
    prefix = "✅" if result.get("ok") else "❌"
    await wait_msg.edit_text(f"{prefix} {result.get('message', '未知结果')}", parse_mode="HTML")


@router.message(Command("strm_restart"))
async def cmd_strm_restart(message: Message):
    if not await check_user_permission(message):
        return

    wait_msg = await message.reply("♻️ 正在重启 STRM watcher…", parse_mode="HTML")
    result = await strm_service.restart()
    prefix = "✅" if result.get("ok") else "❌"
    await wait_msg.edit_text(f"{prefix} {result.get('message', '未知结果')}", parse_mode="HTML")


@router.message(Command("rm_strm"))
async def cmd_rm_strm(message: Message):
    if not await check_user_permission(message):
        return

    now = time.time()
    expired_keys = [
        msg_id
        for msg_id, payload in rm_strm_pending_confirms.items()
        if now - float(payload.get("created_at") or 0) > RM_STRM_CONFIRM_TTL
    ]
    for msg_id in expired_keys:
        rm_strm_pending_confirms.pop(msg_id, None)

    wait_msg = await message.reply("🧹 正在预览 STRM 空目录清理结果…", parse_mode="HTML")
    result = await strm_prune_service.run(apply_changes=False)
    if not result.get("ok"):
        await wait_msg.edit_text(f"❌ {result.get('message', '未知结果')}", parse_mode="HTML")
        return

    summary = result.get("summary") or {}
    preview_text = result.get("message", "")
    deletable_total = int(summary.get("deletable_total", 0) or 0)
    if deletable_total <= 0:
        await wait_msg.edit_text(preview_text, parse_mode="HTML")
        return

    state_key = make_message_state_key(wait_msg)
    if not state_key:
        await wait_msg.edit_text("❌ 无法记录确认状态，请稍后重试", parse_mode="HTML")
        return

    rm_strm_pending_confirms[state_key] = {
        "user_id": message.from_user.id,
        "created_at": time.time(),
        "preview_text": preview_text,
    }
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="⚠️ 确认删除", callback_data=f"rm_strm_confirm:{wait_msg.message_id}"),
            InlineKeyboardButton(text="取消", callback_data=f"rm_strm_cancel:{wait_msg.message_id}"),
        ]
    ])
    confirm_text = (
        f"{preview_text}\n\n"
        "<b>确认操作：</b>\n"
        "• 点击“确认删除”后将按当前 .env 配置实际删除以上空目录\n"
        "• 点击“取消”则本次仅保留预览结果"
    )
    await wait_msg.edit_text(confirm_text, reply_markup=kb, parse_mode="HTML")


@router.message(Command("hdc"))
async def cmd_cancel_sa(message: Message):
    """取消当前用户最近一次的自动添加到SA任务"""
    if not await check_user_permission(message):
        return

    user_id = message.from_user.id
    latest_message_id = None
    latest_task = None
    latest_created_at = -1.0

    for msg_id, task_info in pending_sa_tasks.items():
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
            parse_mode="HTML"
        )
        logging.info(
            "❌ 用户取消了自动添加任务: user_id=%s message_id=%s link=%s",
            user_id,
            latest_message_id,
            latest_task["link"],
        )
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
    
    try:
        # 使用会话管理模式
        result = await fetch_download_link(
            resource_id,
            user_id=user_id,
            keep_session=True,
            start_url=resource_url,
        )
    except Exception as exc:
        logging.error("❌ 资源链接提取前置检查失败: %s", exc)
        await wait_msg.edit_text(
            format_error_message('fetch_failed'),
            parse_mode="HTML"
        )
        return
    website = (result or {}).get("website") or resource_website_cache.get(resource_id, "")
    
    if result and result.get("need_unlock"):
        if await handle_unlock_required(
            wait_msg=wait_msg,
            fallback_msg=message,
            resource_id=resource_id,
            user_id=user_id,
            result=result,
            website=website,
        ):
            return
    
    elif result and (result.get("link") or result.get("need_unlock") is False):
        # 成功提取链接 - 统一使用队列处理底层 unlock 接口
        await fetch_download_link_and_handle_result(
            wait_msg=wait_msg,
            fallback_msg=message,
            resource_id=resource_id,
            user_id=user_id,
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
            trim_dict_cache(resource_website_cache, RESOURCE_WEBSITE_CACHE_LIMIT)

    text, kb = format_resource_list(resources, media_type, provider_filter="115")
    await wait_msg.edit_text(text, reply_markup=kb, parse_mode="HTML")
    state_key = make_message_state_key(wait_msg)
    if state_key:
        resource_list_state[state_key] = {
            "resources": resources,
            "media_type": media_type,
            "title": None,
        }
        trim_dict_cache(resource_list_state, RESOURCE_LIST_STATE_LIMIT)


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
            state_key = make_message_state_key(sent_msg)
            if state_key:
                tmdb_search_state[state_key] = {
                    "results": tmdb_result,
                    "page": 0,
                }
                trim_dict_cache(tmdb_search_state, TMDB_SEARCH_STATE_LIMIT)
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
            state_key = make_message_state_key(sent_msg)
            if state_key:
                resource_list_state[state_key] = {
                    "resources": resources,
                    "media_type": media_type,
                    "title": None,
                }
                trim_dict_cache(resource_list_state, RESOURCE_LIST_STATE_LIMIT)
        else:
            await wait_msg.edit_text(text, reply_markup=kb, parse_mode="HTML")
            state_key = make_message_state_key(wait_msg)
            if state_key:
                resource_list_state[state_key] = {
                    "resources": resources,
                    "media_type": media_type,
                    "title": None,
                }
                trim_dict_cache(resource_list_state, RESOURCE_LIST_STATE_LIMIT)

        elapsed = int((time.monotonic() - start_ts) * 1000)
        logging.info("✅ 关键词搜索完成: keyword=%s type=%s resources=%s cost_ms=%s", keyword, search_type, len(resources), elapsed)
            
    except Exception as e:
        logging.error(f"❌ 搜索出错: {e}")
        await wait_msg.edit_text(f"❌ 运行出错: {e}", parse_mode="HTML")



@router.callback_query(F.data.startswith(ASS_MENU_PREFIX))
async def callback_ass_menu(callback: CallbackQuery):
    if not await check_callback_permission(callback):
        return

    msg = callback.message
    if not msg:
        await callback.answer()
        return

    action = (callback.data or "")[len(ASS_MENU_PREFIX):]
    if action == "subset":
        await callback.answer("开始执行子集化字体…")
        await msg.edit_text("⏳ 正在执行 ASS 字幕子集化字体内封，请稍候…", parse_mode="HTML")
        ok, text = await ass_service.run_subset(callback.bot, msg.chat.id)
        prefix = "✅" if ok else "❌"
        try:
            await msg.edit_text(text, parse_mode="HTML")
        except Exception:
            await msg.answer(f"{prefix} ASS 任务已完成，请查看机器人日志/汇总消息", parse_mode="HTML")
        return

    if action == "mux_start":
        await callback.answer("开始创建字幕内封会话…")
        try:
            await ass_service.start_mux_session(chat_id=msg.chat.id, owner_user_id=callback.from_user.id)
            panel_text = await ass_service.build_mux_panel_text(msg.chat.id, callback.from_user.id)
            kb = ass_service.build_mux_plan_keyboard(msg.chat.id, callback.from_user.id)
            await msg.edit_text(panel_text, reply_markup=kb, parse_mode="HTML")
            preview_msg = await msg.answer("🎞️ <b>计划预览</b>\n\n尚未生成计划。", parse_mode="HTML")
            ass_service.bind_mux_message_ids(
                msg.chat.id,
                callback.from_user.id,
                panel_message_id=msg.message_id,
                preview_message_id=preview_msg.message_id,
            )
            await sync_ass_mux_view(callback.bot, msg.chat.id, callback.from_user.id)
        except Exception as exc:
            logging.exception("❌ 创建 /ass 字幕内封会话失败")
            await msg.edit_text(f"❌ 创建字幕内封会话失败\n\n<code>{html.escape(str(exc))}</code>", parse_mode="HTML")
        return

    await callback.answer()


@router.callback_query(F.data.startswith(ASS_MUX_PREFIX))
async def callback_ass_mux(callback: CallbackQuery):
    if not await check_callback_permission(callback):
        return

    msg = callback.message
    if not msg:
        await callback.answer()
        return

    if not ass_service.ensure_mux_owner(msg.chat.id, callback.from_user.id):
        await callback.answer("只能由发起人操作", show_alert=True)
        return

    payload = (callback.data or "")[len(ASS_MUX_PREFIX):]

    try:
        if payload == "toggle_delete":
            session = ass_service.get_mux_session(msg.chat.id, callback.from_user.id)
            if not session:
                raise RuntimeError("当前会话已失效，请重新发送 /ass")
            session.delete_external_subs = not session.delete_external_subs
            session.touch()
            await callback.answer("已切换删除外挂字幕开关")
            await sync_ass_mux_view(callback.bot, msg.chat.id, callback.from_user.id)
            return

        if payload == "toggle_dry":
            session = ass_service.get_mux_session(msg.chat.id, callback.from_user.id)
            if not session:
                raise RuntimeError("当前会话已失效，请重新发送 /ass")
            session.dry_run = not session.dry_run
            session.touch()
            await callback.answer("已切换 DRY-RUN 开关")
            await sync_ass_mux_view(callback.bot, msg.chat.id, callback.from_user.id)
            return

        if payload == "prompt_group":
            session = ass_service.get_mux_session(msg.chat.id, callback.from_user.id)
            ass_service.set_mux_prompt(msg.chat.id, callback.from_user.id, field="default_group", message_id=session.awaiting_message_id if session and session.awaiting_message_id else msg.message_id)
            await callback.answer("请发送默认字幕组")
            await msg.answer(
                "✏️ 请输入新的默认字幕组\n\n"
                "• 直接发送文字 = 设置字幕组\n"
                "• 发送 <code>-</code> = 清空字幕组\n"
                "• 修改后请点击“重新扫描生成计划”使其生效",
                parse_mode="HTML",
            )
            return

        if payload == "prompt_lang":
            session = ass_service.get_mux_session(msg.chat.id, callback.from_user.id)
            ass_service.set_mux_prompt(msg.chat.id, callback.from_user.id, field="default_lang", message_id=session.awaiting_message_id if session and session.awaiting_message_id else msg.message_id)
            await callback.answer("请发送默认语言")
            await msg.answer(
                "🌐 请输入新的默认语言，例如：<code>chs</code> / <code>cht</code> / <code>eng</code> / <code>chs_eng</code>",
                parse_mode="HTML",
            )
            return

        if payload == "refresh":
            await callback.answer("正在重新扫描生成计划…")
            await msg.edit_text("🔄 正在扫描目录并生成字幕内封计划…", parse_mode="HTML")
            session, preview = await ass_service.rebuild_mux_plan(msg.chat.id, callback.from_user.id)
            await sync_ass_mux_view(callback.bot, msg.chat.id, callback.from_user.id)
            return

        if payload.startswith("page:"):
            _, page_raw = payload.split(":", 1)
            session = ass_service.get_mux_session(msg.chat.id, callback.from_user.id)
            if not session:
                raise RuntimeError("当前会话已失效，请重新发送 /ass")
            page = int(page_raw)
            if page != session.plan_page:
                session.plan_page = max(0, page)
                session.touch()
                await sync_ass_mux_view(callback.bot, msg.chat.id, callback.from_user.id)
            await callback.answer()
            return

        if payload.startswith("edit_item:"):
            _, index_raw = payload.split(":", 1)
            item_index = int(index_raw)
            session = ass_service.get_mux_session(msg.chat.id, callback.from_user.id)
            await callback.answer("打开条目编辑")
            text = ass_service.format_mux_item_detail(msg.chat.id, callback.from_user.id, item_index)
            kb = ass_service.build_mux_item_keyboard(msg.chat.id, callback.from_user.id, item_index)
            await msg.edit_text(text, reply_markup=kb, parse_mode="HTML")
            return

        if payload.startswith("prompt_subfile:"):
            _, item_raw, sub_raw = payload.split(":", 2)
            item_index = int(item_raw)
            sub_index = int(sub_raw)
            session = ass_service.get_mux_session(msg.chat.id, callback.from_user.id)
            candidates = ass_service.list_mux_candidate_subs(msg.chat.id, callback.from_user.id, item_index)
            ass_service.set_mux_prompt(msg.chat.id, callback.from_user.id, field="sub_file", item_index=item_index, sub_index=sub_index, message_id=session.awaiting_message_id if session and session.awaiting_message_id else msg.message_id)
            await callback.answer("请发送新的字幕文件名")
            lines = [
                "🧩 请输入新的字幕文件名（只输入文件名，不要带路径）",
                "",
                "<b>同目录可选字幕：</b>",
            ]
            if candidates:
                for name in candidates[:20]:
                    lines.append(f"• <code>{html.escape(name)}</code>")
                if len(candidates) > 20:
                    lines.append(f"• ... 其余 <code>{len(candidates) - 20}</code> 个已省略")
            else:
                lines.append("• <code>（未发现 .ass/.sup）</code>")
            await msg.answer("\n".join(lines), parse_mode="HTML")
            return

        if payload.startswith("prompt_subgroup:"):
            _, item_raw, sub_raw = payload.split(":", 2)
            item_index = int(item_raw)
            sub_index = int(sub_raw)
            session = ass_service.get_mux_session(msg.chat.id, callback.from_user.id)
            ass_service.set_mux_prompt(msg.chat.id, callback.from_user.id, field="track_group", item_index=item_index, sub_index=sub_index, message_id=session.awaiting_message_id if session and session.awaiting_message_id else msg.message_id)
            await callback.answer("请发送字幕组")
            await msg.answer(
                "✏️ 请输入新的字幕组\n\n• 直接发送文字 = 设置字幕组\n• 发送 <code>-</code> = 清空字幕组",
                parse_mode="HTML",
            )
            return

        if payload.startswith("prompt_sublang:"):
            _, item_raw, sub_raw = payload.split(":", 2)
            item_index = int(item_raw)
            sub_index = int(sub_raw)
            session = ass_service.get_mux_session(msg.chat.id, callback.from_user.id)
            ass_service.set_mux_prompt(msg.chat.id, callback.from_user.id, field="track_lang", item_index=item_index, sub_index=sub_index, message_id=session.awaiting_message_id if session and session.awaiting_message_id else msg.message_id)
            await callback.answer("请发送字幕语言")
            await msg.answer(
                "🌐 请输入新的字幕语言，例如：<code>chs</code> / <code>cht</code> / <code>eng</code> / <code>chs_eng</code>",
                parse_mode="HTML",
            )
            return

        if payload == "back_plan":
            ass_service.clear_mux_prompt(msg.chat.id, callback.from_user.id)
            await callback.answer("返回计划列表")
            await sync_ass_mux_view(callback.bot, msg.chat.id, callback.from_user.id)
            return

        if payload == "run_confirm":
            await callback.answer("请确认是否执行")
            text = ass_service.format_mux_run_confirm(msg.chat.id, callback.from_user.id)
            kb = ass_service.build_mux_run_confirm_keyboard()
            await msg.edit_text(text, reply_markup=kb, parse_mode="HTML")
            return

        if payload == "run_now":
            await callback.answer("开始执行字幕内封…")
            await msg.edit_text("⏳ 正在执行字幕内封，请稍候…\n\n详细过程请查看 Docker 日志。", parse_mode="HTML")
            ok, text = await ass_service.run_mux(callback.bot, msg.chat.id, callback.from_user.id)
            prefix = "✅" if ok else "❌"
            try:
                await msg.edit_text(text, parse_mode="HTML")
            except Exception:
                await msg.answer(f"{prefix} 字幕内封任务已完成，请查看机器人日志/汇总消息", parse_mode="HTML")
            return

        if payload == "cancel":
            session = ass_service.get_mux_session(msg.chat.id, callback.from_user.id)
            preview_message_id = session.preview_message_id if session else None
            ass_service.clear_mux_session(msg.chat.id, callback.from_user.id)
            await callback.answer("已结束本次会话")
            await msg.edit_text("❎ 已结束本次 /ass 字幕内封会话。", parse_mode="HTML")
            if preview_message_id:
                try:
                    await callback.bot.edit_message_text(
                        chat_id=msg.chat.id,
                        message_id=preview_message_id,
                        text="❎ 该预览已随本次 /ass 会话结束。",
                        parse_mode="HTML",
                    )
                except Exception:
                    pass
            return

        await callback.answer()
    except Exception as exc:
        logging.exception("❌ /ass 字幕内封交互失败")
        await callback.answer("操作失败", show_alert=True)
        try:
            await msg.answer(f"❌ 操作失败\n\n<code>{html.escape(str(exc))}</code>", parse_mode="HTML")
        except Exception:
            pass



@router.callback_query(F.data.startswith("rm_strm_confirm:"))
async def callback_rm_strm_confirm(callback: CallbackQuery):
    if not await check_callback_permission(callback):
        return

    msg = callback.message
    if not msg:
        await callback.answer()
        return

    key = (callback.data or "").split(":", 1)[1]
    if str(msg.message_id) != key:
        await callback.answer("确认消息不匹配", show_alert=True)
        return

    state_key = make_message_state_key(msg)
    pending = rm_strm_pending_confirms.get(state_key or "")
    if not pending:
        await callback.answer("确认已失效，请重新执行 /rm_strm", show_alert=True)
        return

    if pending.get("user_id") != callback.from_user.id:
        await callback.answer("只能由发起人确认删除", show_alert=True)
        return

    created_at = float(pending.get("created_at") or 0)
    if time.time() - created_at > RM_STRM_CONFIRM_TTL:
        if state_key:
            rm_strm_pending_confirms.pop(state_key, None)
        await msg.edit_reply_markup(reply_markup=None)
        await callback.answer("确认已过期，请重新执行 /rm_strm", show_alert=True)
        return

    await callback.answer("开始删除…")
    await msg.edit_text("🧹 已确认，正在执行 STRM 空目录实际删除…", parse_mode="HTML")
    result = await strm_prune_service.run(apply_changes=True)
    if state_key:
        rm_strm_pending_confirms.pop(state_key, None)
    prefix = "✅" if result.get("ok") else "❌"
    await msg.edit_text(f"{prefix} {result.get('message', '未知结果')}", parse_mode="HTML")


@router.callback_query(F.data.startswith("rm_strm_cancel:"))
async def callback_rm_strm_cancel(callback: CallbackQuery):
    if not await check_callback_permission(callback):
        return

    msg = callback.message
    if not msg:
        await callback.answer()
        return

    key = (callback.data or "").split(":", 1)[1]
    if str(msg.message_id) != key:
        await callback.answer("取消消息不匹配", show_alert=True)
        return

    state_key = make_message_state_key(msg)
    pending = rm_strm_pending_confirms.get(state_key or "")
    if not pending:
        await callback.answer("这条确认已失效", show_alert=True)
        return

    if pending.get("user_id") != callback.from_user.id:
        await callback.answer("只能由发起人取消", show_alert=True)
        return

    preview_text = str(pending.get("preview_text") or "🧹 本次仅执行了预览")
    if state_key:
        rm_strm_pending_confirms.pop(state_key, None)
    await callback.answer("已取消")
    await msg.edit_text(f"{preview_text}\n\n❎ 已取消实际删除，本次仅保留预览结果。", parse_mode="HTML")


@router.callback_query(F.data.startswith("pf:"))
async def callback_provider_filter(callback: CallbackQuery):
    """切换资源网盘筛选（常驻按钮）"""
    if not await check_callback_permission(callback):
        return

    msg = callback.message
    if not msg:
        await callback.answer()
        return

    state_key = make_message_state_key(msg)
    state = resource_list_state.get(state_key or "")
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
    await callback.answer()


@router.callback_query(F.data.startswith("tmdb_page:"))
async def callback_tmdb_page(callback: CallbackQuery):
    """切换 TMDB 候选列表分页。"""
    if not await check_callback_permission(callback):
        return

    msg = callback.message
    if not msg:
        await callback.answer()
        return

    state_key = make_message_state_key(msg)
    state = tmdb_search_state.get(state_key or "")
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

    关键行为：
    - 保留原始资源选择页，便于继续点其他资源
    - 新开一条回复消息承载“提取中 / 解锁确认 / 提取结果”

    Callback data 格式: tv_1:resource_id 或 movie_1:resource_id
    """
    if not await check_callback_permission(callback):
        return

    await callback.answer()

    wait_msg: Message | None = None

    try:
        msg = callback.message
        if not msg:
            return

        user_id = callback.from_user.id

        # 解析回调数据
        data_parts = (callback.data or "").split(":")
        if len(data_parts) != 2:
            await msg.answer("❌ 数据格式错误")
            return

        resource_id = data_parts[1]

        # 单独发一条结果消息，避免覆盖资源选择页
        wait_msg = await msg.reply(
            f"⏳ 正在提取链接...\n🆔 <code>{resource_id}</code>",
            parse_mode="HTML"
        )

        # 提取链接（使用会话管理）
        try:
            result = await fetch_download_link(resource_id, user_id=user_id, keep_session=True)
        except Exception as exc:
            logging.error("❌ 资源选择前置检查失败: %s", exc)
            await wait_msg.edit_text(
                format_error_message('fetch_failed'),
                parse_mode="HTML"
            )
            return
        website = (result or {}).get("website") or resource_website_cache.get(resource_id, "")

        if result and result.get("need_unlock"):
            if await handle_unlock_required(
                wait_msg=wait_msg,
                fallback_msg=msg,
                resource_id=resource_id,
                user_id=user_id,
                result=result,
                website=website,
            ):
                return

        if result and (result.get("link") or result.get("need_unlock") is False):
            await fetch_download_link_and_handle_result(
                wait_msg=wait_msg,
                fallback_msg=msg,
                resource_id=resource_id,
                user_id=user_id,
                website=result.get("website") or website,
            )
            return

        await wait_msg.edit_text(
            format_error_message('fetch_failed'),
            parse_mode="HTML"
        )

    except Exception as e:
        logging.error(f"❌ 回调处理出错: {e}")
        if wait_msg is not None:
            await wait_msg.edit_text(f"❌ 处理出错: {e}", parse_mode="HTML")
        elif callback.message:
            await callback.message.answer(f"❌ 处理出错: {e}", parse_mode="HTML")


@router.callback_query(F.data.startswith("unlock:"))
async def callback_unlock_resource(callback: CallbackQuery):
    """
    处理解锁确认回调
    
    Callback data 格式: unlock:resource_id
    """
    if not await check_callback_permission(callback):
        return

    await callback.answer("🔓 正在解锁...")
    
    try:
        user_id = callback.from_user.id
        resource_id = callback.data.split(":")[1]

        await perform_unlock_and_handle_result(
            wait_msg=callback.message,
            fallback_msg=callback.message,
            resource_id=resource_id,
            user_id=user_id,
            auto_unlock=False,
            website=resource_website_cache.get(resource_id, ""),
        )
            
    except Exception as e:
        logging.error(f"❌ 解锁出错: {e}")
        await callback.message.edit_text(f"❌ 解锁出错: {e}", parse_mode="HTML")


@router.callback_query(F.data == "cancel_unlock")
async def callback_cancel_unlock(callback: CallbackQuery):
    """处理取消解锁回调"""
    if not await check_callback_permission(callback):
        return

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


async def auto_add_to_sa(task_key: str, link: str, original_message: Message, countdown: int = 60):
    """
    倒计时自动添加到SA
    
    Args:
        task_key: 任务键（chat_id:message_id）
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
            if task_key in pending_sa_tasks and pending_sa_tasks[task_key].get("cancelled"):
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
        if task_key in pending_sa_tasks and pending_sa_tasks[task_key].get("cancelled"):
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
        if task_key in pending_sa_tasks:
            del pending_sa_tasks[task_key]


@router.callback_query(F.data.startswith("send_to_group:"))
async def callback_send_to_sa(callback: CallbackQuery):
    """
    发送115链接到SA（Symedia）
    
    Callback data 格式: send_to_group:115_link
    """
    if not await check_callback_permission(callback):
        return

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
                    await callback.message.reply(
                        f"❌ 添加到 Symedia 失败: HTTP {response.status}",
                        parse_mode="HTML"
                    )
        
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

    session = ass_service.get_mux_session(message.chat.id, message.from_user.id)
    if session and session.awaiting_field and message.from_user.id == session.owner_user_id and message.text and not text.startswith('/'):
        try:
            result_text = ass_service.apply_mux_text_input(message.chat.id, message.from_user.id, text)
            await message.reply(result_text, parse_mode="HTML")
            await sync_ass_mux_view(message.bot, message.chat.id, message.from_user.id)
        except Exception as exc:
            logging.exception("❌ 处理 /ass 字幕内封输入失败")
            await message.reply(f"❌ 输入无效\n\n<code>{html.escape(str(exc))}</code>", parse_mode="HTML")
        return

    # 如果是命令，跳过（由其他handler处理）
    if text.startswith('/'):
        return

    # 是否启用“直接发送 HDHive 链接自动解析”
    if not HDHIVE_PARSE_INCOMING_LINKS:
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
        
        try:
            # 使用会话管理
            result = await fetch_download_link(
                resource_id,
                user_id=user_id,
                keep_session=True,
                start_url=resource_url,
            )
        except Exception as exc:
            logging.error("❌ 直接链接前置检查失败: %s", exc)
            await wait_msg.edit_text(
                format_error_message('fetch_failed'),
                parse_mode="HTML"
            )
            return
        website = (result or {}).get("website") or resource_website_cache.get(resource_id, "")
        
        if result and result.get("need_unlock"):
            if await handle_unlock_required(
                wait_msg=wait_msg,
                fallback_msg=message,
                resource_id=resource_id,
                user_id=user_id,
                result=result,
                website=website,
            ):
                return
        
        elif result and (result.get("link") or result.get("need_unlock") is False):
            await fetch_download_link_and_handle_result(
                wait_msg=wait_msg,
                fallback_msg=message,
                resource_id=resource_id,
                user_id=user_id,
                website=result.get("website") or website,
            )
            return

        await wait_msg.edit_text(
            format_error_message('fetch_failed'),
            parse_mode="HTML"
        )
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
            state_key = make_message_state_key(wait_msg)
            if state_key:
                resource_list_state[state_key] = {
                    "resources": resources,
                    "media_type": media_type,
                    "title": None,
                }
                trim_dict_cache(resource_list_state, RESOURCE_LIST_STATE_LIMIT)
            return


@router.callback_query(F.data.startswith("select_tmdb:"))
async def callback_select_tmdb(callback: CallbackQuery):
    """
    处理用户选择TMDB搜索结果
    
    Callback data 格式: select_tmdb:tmdb_id:media_type
    """
    if not await check_callback_permission(callback):
        return

    await callback.answer()

    try:
        msg = callback.message
        if not msg:
            return

        tmdb_info = None
        state_key = make_message_state_key(msg)
        state = tmdb_search_state.get(state_key or "")

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
            if state_key:
                tmdb_search_state.pop(state_key, None)
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
            state_key = make_message_state_key(sent_msg)
            if state_key:
                resource_list_state[state_key] = {
                    "resources": resources,
                    "media_type": media_type,
                    "title": None,
                }
                trim_dict_cache(resource_list_state, RESOURCE_LIST_STATE_LIMIT)
        else:
            await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
            state_key = make_message_state_key(callback.message)
            if state_key:
                resource_list_state[state_key] = {
                    "resources": resources,
                    "media_type": media_type,
                    "title": None,
                }
                trim_dict_cache(resource_list_state, RESOURCE_LIST_STATE_LIMIT)
        
    except Exception as e:
        logging.error(f"❌ 处理TMDB选择失败: {e}")
        await callback.message.edit_text(f"❌ 处理失败: {e}", parse_mode="HTML")
