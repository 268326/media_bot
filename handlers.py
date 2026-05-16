"""
Telegram 处理器模块
处理所有命令和回调查询
"""
import logging
import asyncio
import contextlib
import html
import os
import time
from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    Message,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    BufferedInputFile,
)

from config import (
    BOT_USER_IDS,
    BOT_USER_ID_SET,
    LOG_PATH,
)
from ass_formatter import (
    format_mux_running,
    format_rescan_running,
    format_subset_running,
)
from ass_mux_pipeline import MuxProgressEvent
from ass_service import (
    ass_service,
    ASS_MENU_PREFIX,
    ASS_MUX_PREFIX,
)
from danmu_service import fetch_bilibili_danmaku_xml, DanmuError
from checkin_service import daily_check_in
from hdhive_openapi_client import get_user_points
from hdhive_openapi_flow import hdhive_openapi_flow_service
from formatter import (
    format_points_message,
    format_error_message,
    format_help_message,
    format_start_message
)
from emby_task_service import emby_task_service
from emby_task_formatter import (
    EMBY_TASK_CALLBACK_PREFIX,
    build_category_summary,
    build_task_detail,
    build_tasks_panel,
    describe_filter_mode,
    filter_tasks_for_view,
)
from strm_service import strm_service
from strm_prune_service import strm_prune_service

# 创建路由器
router = Router()

# 待确认的 STRM 清理任务 {"chat_id:message_id": {"user_id": int, "created_at": float, "preview_text": str}}
rm_strm_pending_confirms: dict[str, dict] = {}
RM_STRM_CONFIRM_TTL = 900

# Emby 任务列表状态缓存："chat_id:message_id" -> {"tasks": list[dict], "page": int, "owner_user_id": int}
emby_task_state: dict[str, dict] = {}
EMBY_TASK_STATE_LIMIT = 128


def trim_dict_cache(cache: dict, limit: int) -> None:
    while len(cache) > limit:
        oldest_key = next(iter(cache))
        cache.pop(oldest_key, None)


def make_message_state_key(message: Message | None) -> str | None:
    return hdhive_openapi_flow_service.make_message_state_key(message)


async def sync_ass_mux_view(bot, chat_id: int, owner_user_id: int):
    session = ass_service.get_mux_session(chat_id, owner_user_id)
    if not session:
        return

    panel_text = await ass_service.build_mux_panel_text(chat_id, owner_user_id)
    panel_kb = ass_service.build_mux_plan_keyboard(chat_id, owner_user_id)
    panel_message_id = session.awaiting_message_id
    if panel_message_id:
        try:
            await bot.edit_message_text(
                panel_text,
                chat_id=chat_id,
                message_id=panel_message_id,
                reply_markup=panel_kb,
                parse_mode="HTML",
            )
        except Exception as exc:
            logging.debug("更新 /ass 主面板失败: %s", exc)


async def pump_ass_mux_progress(bot, chat_id: int, message_id: int, session, progress_queue: asyncio.Queue[MuxProgressEvent | None]):
    last_processed = 0
    last_total = sum(1 for item in session.plan.items if getattr(item, 'subs', None)) if session.plan else 0
    while True:
        event = await progress_queue.get()
        if event is None:
            return
        last_processed = max(0, int(event.processed))
        last_total = max(last_processed, int(event.total))
        current_file = html.escape(event.current_file or "")
        text = format_mux_running(
            processed=last_processed,
            total=last_total,
            dry_run=bool(session.dry_run),
        )
        if current_file:
            text += f"\n📝 当前完成: <code>{current_file}</code>"
        try:
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=text,
                parse_mode="HTML",
            )
        except Exception as exc:
            logging.debug("更新 /ass 字幕内封进度消息失败: %s", exc)



def _build_emby_task_state(tasks: list[dict], *, page: int, owner_user_id: int, filter_mode: str = "all") -> dict:
    return {
        "tasks": tasks,
        "page": page,
        "owner_user_id": owner_user_id,
        "filter_mode": filter_mode,
    }


PRO_QUICK_TASK_NAMES = [
    "Refresh Episode",
    "Update Plugin",
    "Scan External Tracks",
    "Build Douban Cache",
    "Refresh Chinese Actor",
    "Extract MediaInfo",
]


def _find_task_by_name(tasks: list[dict], task_name: str) -> dict | None:
    for task in tasks:
        if str(task.get("name") or task.get("display_name") or "") == str(task_name or ""):
            return task
    return None

def _parse_emby_task_callback_value(raw: str) -> tuple[int, str]:
    text = str(raw or "")
    if "|" not in text:
        return 0, text
    page_raw, task_id = text.split("|", 1)
    try:
        page = int(page_raw)
    except ValueError:
        page = 0
    return page, task_id


def _build_pro_quick_actions(tasks: list[dict], filter_mode: str) -> list[tuple[str, str]]:
    if filter_mode != "pro":
        return []
    actions: list[tuple[str, str]] = []
    for task_name in PRO_QUICK_TASK_NAMES:
        task = _find_task_by_name(tasks, task_name)
        if not task:
            continue
        task_label = str(task.get("display_name") or task.get("name") or task_name)
        if len(task_label) > 10:
            task_label = task_label[:9] + "…"
        actions.append((task_label, str(task.get("id") or "")))
    return actions


# ==================== 权限检查中间件 ====================

async def check_user_permission(message: Message) -> bool:
    """
    检查用户是否有权限使用机器人
    """
    if not BOT_USER_IDS:
        return True

    user_id = message.from_user.id
    if user_id not in BOT_USER_ID_SET:
        await message.reply(
            format_error_message('permission_denied'),
            parse_mode="HTML"
        )
        logging.warning(f"❌ 用户 {user_id} ({message.from_user.username}) 尝试使用机器人但被拒绝")
        return False
    return True


def resolve_message_owner_id(message: Message | None) -> int | None:
    current = message
    for _ in range(5):
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

    if BOT_USER_IDS and user_id not in BOT_USER_ID_SET:
        await callback.answer("⛔️ 权限不足", show_alert=True)
        logging.warning(f"❌ 用户 {user_id} ({callback.from_user.username}) 尝试点击受限按钮但被拒绝")
        return False

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

    url = args[1].strip()
    wait_msg = await message.reply("⏳ 正在解析弹幕链接...", parse_mode="HTML")

    try:
        filename, xml_bytes = await fetch_bilibili_danmaku_xml(url)
    except DanmuError as exc:
        await wait_msg.edit_text(f"❌ {html.escape(str(exc))}", parse_mode="HTML")
        return
    except Exception as exc:
        logging.exception("❌ 下载弹幕失败")
        await wait_msg.edit_text(f"❌ 下载失败: {html.escape(str(exc))}", parse_mode="HTML")
        return

    try:
        await wait_msg.delete()
    except Exception:
        pass

    await message.reply_document(
        BufferedInputFile(xml_bytes, filename=filename),
        caption=f"✅ 已生成弹幕 XML\n<code>{html.escape(filename)}</code>",
        parse_mode="HTML"
    )




@router.message(Command("ass"))
async def cmd_ass(message: Message):
    if not await check_user_permission(message):
        return

    text, kb = await ass_service.build_mux_menu(message)
    await message.reply(text, reply_markup=kb, parse_mode="HTML")


@router.message(Command("emby_tasks"))
async def cmd_emby_tasks(message: Message):
    """打开 Emby 计划任务面板"""
    if not await check_user_permission(message):
        return

    wait_msg = await message.reply("⏳ 正在获取 Emby 任务列表...", parse_mode="HTML")
    try:
        tasks = await emby_task_service.list_tasks()
    except Exception as exc:
        logging.exception("❌ 获取 Emby 任务列表失败")
        await wait_msg.edit_text(f"❌ 获取 Emby 任务列表失败\n\n<code>{html.escape(str(exc))}</code>", parse_mode="HTML")
        return

    filter_mode = "pro"
    filtered_tasks = filter_tasks_for_view(tasks, filter_mode)
    summary = build_category_summary(tasks)
    header_note = describe_filter_mode(filter_mode)
    panel_text, kb = build_tasks_panel(
        filtered_tasks,
        page=0,
        summary=summary,
        notify_enabled=emby_task_service.notify_enabled,
        filter_mode=filter_mode,
        header_note=header_note,
        quick_actions=_build_pro_quick_actions(tasks, filter_mode),
    )
    await wait_msg.edit_text(panel_text, reply_markup=kb, parse_mode="HTML")
    state_key = make_message_state_key(wait_msg)
    if state_key:
        emby_task_state[state_key] = _build_emby_task_state(
            tasks,
            page=0,
            owner_user_id=message.from_user.id,
            filter_mode=filter_mode,
        )
        trim_dict_cache(emby_task_state, EMBY_TASK_STATE_LIMIT)


@router.message(Command("strm_status"))
async def cmd_strm_status(message: Message):
    """查看 STRM 监控服务状态。"""
    if not await check_user_permission(message):
        return

    status = strm_service.status()
    text = strm_service.build_status_text(status)
    await message.reply(text, parse_mode="HTML")


@router.message(Command("strm_scan"))
async def cmd_strm_scan(message: Message):
    """手动触发一次 STRM 全量扫描。"""
    if not await check_user_permission(message):
        return

    wait_msg = await message.reply("⏳ 正在触发 STRM 重扫...", parse_mode="HTML")
    try:
        result = await strm_service.scan_once()
        text = strm_service.build_scan_result_text(result)
        await wait_msg.edit_text(text, parse_mode="HTML")
    except Exception as exc:
        logging.exception("❌ 手动 STRM 重扫失败")
        await wait_msg.edit_text(f"❌ STRM 重扫失败\n\n<code>{html.escape(str(exc))}</code>", parse_mode="HTML")


@router.message(Command("strm_restart"))
async def cmd_strm_restart(message: Message):
    """手动重启 STRM watcher。"""
    if not await check_user_permission(message):
        return

    wait_msg = await message.reply("⏳ 正在重启 STRM watcher...", parse_mode="HTML")
    try:
        await strm_service.restart()
        status = strm_service.status()
        text = "✅ STRM watcher 已重启\n\n" + strm_service.build_status_text(status)
        await wait_msg.edit_text(text, parse_mode="HTML")
    except Exception as exc:
        logging.exception("❌ 重启 STRM watcher 失败")
        await wait_msg.edit_text(f"❌ 重启 STRM watcher 失败\n\n<code>{html.escape(str(exc))}</code>", parse_mode="HTML")


@router.message(Command("rm_strm"))
async def cmd_rm_strm(message: Message):
    """预览 STRM 空目录清理，并通过按钮确认实际删除。"""
    if not await check_user_permission(message):
        return

    wait_msg = await message.reply("⏳ 正在扫描可删除的 STRM 空目录...", parse_mode="HTML")
    try:
        result = await strm_prune_service.preview(message.from_user.id)
    except Exception as exc:
        logging.exception("❌ 预览 STRM 空目录清理失败")
        await wait_msg.edit_text(f"❌ 预览失败\n\n<code>{html.escape(str(exc))}</code>", parse_mode="HTML")
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
    await hdhive_openapi_flow_service.cancel_latest_sa_task(message)


@router.message(Command("llog"))
async def cmd_tail_log(message: Message):
    """发送最新30行日志"""
    if not await check_user_permission(message):
        return

    log_path = str(LOG_PATH or '').strip()
    if log_path in ('', '0', 'false', 'False', 'none', 'None'):
        await message.reply(
            "⚠️ 当前未启用文件日志；请直接查看 Docker 日志，或在 .env 中设置 <code>MEDIA_BOT_LOG_PATH</code> 并开启 <code>MEDIA_BOT_LOG_TO_FILE=1</code>",
            parse_mode="HTML"
        )
        return

    if not os.path.exists(log_path):
        await message.reply(
            f"⚠️ 未找到日志文件 {html.escape(os.path.basename(log_path))}",
            parse_mode="HTML"
        )
        return

    try:
        with open(log_path, "rb") as f:
            f.seek(0, os.SEEK_END)
            file_size = f.tell()
            chunk_size = 8192
            seek_pos = max(0, file_size - chunk_size)
            f.seek(seek_pos)
            raw = f.read()
        text = raw.decode("utf-8", errors="replace")
        lines = text.splitlines()
        if seek_pos > 0 and lines:
            lines = lines[1:]
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
    """处理搜索命令。"""
    if not await check_user_permission(message):
        return

    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        cmd = "/hdt" if search_type == "tv" else "/hdm"
        await message.reply(f"请使用: <code>{cmd} 名字或链接</code>", parse_mode="HTML")
        return

    await hdhive_openapi_flow_service.handle_search_input(message, args[1], search_type)


async def handle_resource_link(message: Message, resource_id: str, resource_url: str | None = None):
    await hdhive_openapi_flow_service.handle_resource_link(message, resource_id, resource_url)


async def handle_tmdb_link(message: Message, tmdb_id: str, media_type: str):
    await hdhive_openapi_flow_service.handle_tmdb_link(message, tmdb_id, media_type)


async def handle_keyword_search(message: Message, keyword: str, search_type: str):
    await hdhive_openapi_flow_service.handle_keyword_search(message, keyword, search_type)


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
        await msg.edit_text(format_subset_running(), parse_mode="HTML")
        ok, text = await ass_service.run_subset(callback.bot, msg.chat.id)
        prefix = "✅" if ok else "❌"
        try:
            await msg.edit_text(text, parse_mode="HTML")
        except Exception:
            await msg.answer(f"{prefix} ASS 任务已完成，请查看机器人日志/汇总消息", parse_mode="HTML")
        return

    if action == "mux_start":
        await callback.answer("开始创建自动字幕内封会话…")
        try:
            await ass_service.start_mux_session(chat_id=msg.chat.id, owner_user_id=callback.from_user.id, mode="auto")
            panel_text = await ass_service.build_mux_panel_text(msg.chat.id, callback.from_user.id)
            kb = ass_service.build_mux_plan_keyboard(msg.chat.id, callback.from_user.id)
            await msg.edit_text(panel_text, reply_markup=kb, parse_mode="HTML")
            ass_service.bind_mux_message_ids(
                msg.chat.id,
                callback.from_user.id,
                panel_message_id=msg.message_id,
            )
            await sync_ass_mux_view(callback.bot, msg.chat.id, callback.from_user.id)
        except Exception as exc:
            logging.exception("❌ 创建 /ass 字幕内封会话失败")
            await msg.edit_text(f"❌ 创建字幕内封会话失败\n\n<code>{html.escape(str(exc))}</code>", parse_mode="HTML")
        return

    if action == "mux_manual_start":
        await callback.answer("开始创建手动字幕内封会话…")
        try:
            await ass_service.start_mux_session(chat_id=msg.chat.id, owner_user_id=callback.from_user.id, mode="manual")
            panel_text = await ass_service.build_mux_panel_text(msg.chat.id, callback.from_user.id)
            kb = ass_service.build_mux_plan_keyboard(msg.chat.id, callback.from_user.id)
            await msg.edit_text(panel_text, reply_markup=kb, parse_mode="HTML")
            ass_service.bind_mux_message_ids(
                msg.chat.id,
                callback.from_user.id,
                panel_message_id=msg.message_id,
            )
            await sync_ass_mux_view(callback.bot, msg.chat.id, callback.from_user.id)
        except Exception as exc:
            logging.exception("❌ 创建 /ass 手动字幕内封会话失败")
            await msg.edit_text(f"❌ 创建手动字幕内封会话失败\n\n<code>{html.escape(str(exc))}</code>", parse_mode="HTML")
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
            ass_service.clear_mux_inline_notice(msg.chat.id, callback.from_user.id)
            await callback.answer("请直接发送默认字幕组")
            await sync_ass_mux_view(callback.bot, msg.chat.id, callback.from_user.id)
            return

        if payload == "prompt_lang":
            session = ass_service.get_mux_session(msg.chat.id, callback.from_user.id)
            ass_service.set_mux_prompt(msg.chat.id, callback.from_user.id, field="default_lang", message_id=session.awaiting_message_id if session and session.awaiting_message_id else msg.message_id)
            ass_service.clear_mux_inline_notice(msg.chat.id, callback.from_user.id)
            await callback.answer("请直接发送默认语言")
            await sync_ass_mux_view(callback.bot, msg.chat.id, callback.from_user.id)
            return

        if payload == "prompt_jobs":
            session = ass_service.get_mux_session(msg.chat.id, callback.from_user.id)
            ass_service.set_mux_prompt(msg.chat.id, callback.from_user.id, field="jobs", message_id=session.awaiting_message_id if session and session.awaiting_message_id else msg.message_id)
            ass_service.clear_mux_inline_notice(msg.chat.id, callback.from_user.id)
            await callback.answer("请直接发送并发数")
            await sync_ass_mux_view(callback.bot, msg.chat.id, callback.from_user.id)
            return

        if payload == "refresh":
            ass_service.set_mux_inline_notice(msg.chat.id, callback.from_user.id, '🔄 <b>重新扫描中…</b> 正在刷新目录和计划')
            await msg.edit_text(format_rescan_running(), parse_mode="HTML")
            await ass_service.rebuild_mux_plan(msg.chat.id, callback.from_user.id)
            ass_service.set_mux_inline_notice(msg.chat.id, callback.from_user.id, '✅ <b>已重新扫描</b> 如修改了默认字幕组/语言，新计划已生效。')
            await sync_ass_mux_view(callback.bot, msg.chat.id, callback.from_user.id)
            return

        if payload.startswith("preview:"):
            mode = payload.split(":", 1)[1]
            session = ass_service.get_mux_session(msg.chat.id, callback.from_user.id)
            if not session:
                raise RuntimeError("当前会话已失效，请重新发送 /ass")
            session.preview_mode = mode if mode in ("summary", "list") else "summary"
            session.preview_page = 0
            session.touch()
            await callback.answer("已切换预览模式")
            await sync_ass_mux_view(callback.bot, msg.chat.id, callback.from_user.id)
            return

        if payload.startswith("preview_page:"):
            page = int(payload.split(":", 1)[1])
            session = ass_service.get_mux_session(msg.chat.id, callback.from_user.id)
            if not session:
                raise RuntimeError("当前会话已失效，请重新发送 /ass")
            if page != session.preview_page:
                session.preview_page = max(0, page)
                session.touch()
                await sync_ass_mux_view(callback.bot, msg.chat.id, callback.from_user.id)
            await callback.answer()
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
            ass_service.clear_mux_inline_notice(msg.chat.id, callback.from_user.id)
            await callback.answer("打开条目编辑")
            text = ass_service.format_mux_item_detail(msg.chat.id, callback.from_user.id, item_index)
            kb = ass_service.build_mux_item_keyboard(msg.chat.id, callback.from_user.id, item_index)
            await msg.edit_text(text, reply_markup=kb, parse_mode="HTML")
            return

        if payload.startswith("open_add_sub_picker:"):
            _, item_raw = payload.split(":", 1)
            item_index = int(item_raw)
            ass_service.prepare_mux_add_sub_picker(msg.chat.id, callback.from_user.id, item_index)
            ass_service.clear_mux_inline_notice(msg.chat.id, callback.from_user.id)
            await callback.answer("打开字幕候选列表")
            text = ass_service.format_mux_add_sub_picker(msg.chat.id, callback.from_user.id, item_index)
            kb = ass_service.build_mux_add_sub_picker_keyboard(msg.chat.id, callback.from_user.id, item_index)
            await msg.edit_text(text, reply_markup=kb, parse_mode="HTML")
            return

        if payload.startswith("prompt_add_sub:"):
            _, item_raw = payload.split(":", 1)
            item_index = int(item_raw)
            session = ass_service.get_mux_session(msg.chat.id, callback.from_user.id)
            ass_service.set_mux_prompt(msg.chat.id, callback.from_user.id, field="add_sub_file", item_index=item_index, message_id=session.awaiting_message_id if session and session.awaiting_message_id else msg.message_id)
            ass_service.clear_mux_inline_notice(msg.chat.id, callback.from_user.id)
            await callback.answer("请直接发送要添加的字幕文件名")
            await sync_ass_mux_view(callback.bot, msg.chat.id, callback.from_user.id)
            return

        if payload.startswith("add_sub_page:"):
            _, item_raw, page_raw = payload.split(":", 2)
            item_index = int(item_raw)
            page = int(page_raw)
            ass_service.set_mux_add_sub_picker_page(msg.chat.id, callback.from_user.id, page)
            text = ass_service.format_mux_add_sub_picker(msg.chat.id, callback.from_user.id, item_index)
            kb = ass_service.build_mux_add_sub_picker_keyboard(msg.chat.id, callback.from_user.id, item_index)
            await msg.edit_text(text, reply_markup=kb, parse_mode="HTML")
            await callback.answer()
            return

        if payload.startswith("toggle_add_sub:"):
            _, candidate_raw = payload.split(":", 1)
            candidate_index = int(candidate_raw)
            session = ass_service.get_mux_session(msg.chat.id, callback.from_user.id)
            if not session or session.selected_item_index is None:
                raise RuntimeError("当前未选中视频项，请重新进入单集编辑")
            item_index = session.selected_item_index
            ass_service.toggle_mux_add_sub_candidate(msg.chat.id, callback.from_user.id, candidate_index)
            text = ass_service.format_mux_add_sub_picker(msg.chat.id, callback.from_user.id, item_index)
            kb = ass_service.build_mux_add_sub_picker_keyboard(msg.chat.id, callback.from_user.id, item_index)
            await msg.edit_text(text, reply_markup=kb, parse_mode="HTML")
            await callback.answer()
            return

        if payload.startswith("prompt_subfile:"):
            _, item_raw, sub_raw = payload.split(":", 2)
            item_index = int(item_raw)
            sub_index = int(sub_raw)
            session = ass_service.get_mux_session(msg.chat.id, callback.from_user.id)
            ass_service.set_mux_prompt(msg.chat.id, callback.from_user.id, field="sub_file", item_index=item_index, sub_index=sub_index, message_id=session.awaiting_message_id if session and session.awaiting_message_id else msg.message_id)
            ass_service.clear_mux_inline_notice(msg.chat.id, callback.from_user.id)
            await callback.answer("请直接发送新的字幕文件名")
            await sync_ass_mux_view(callback.bot, msg.chat.id, callback.from_user.id)
            return

        if payload.startswith("confirm_add_sub:"):
            _, item_raw = payload.split(":", 1)
            item_index = int(item_raw)
            result_text = ass_service.confirm_mux_add_sub_candidates(msg.chat.id, callback.from_user.id)
            ass_service.set_mux_inline_notice(msg.chat.id, callback.from_user.id, result_text)
            await callback.answer("已批量添加字幕")
            text = ass_service.format_mux_item_detail(msg.chat.id, callback.from_user.id, item_index)
            kb = ass_service.build_mux_item_keyboard(msg.chat.id, callback.from_user.id, item_index)
            await msg.edit_text(text, reply_markup=kb, parse_mode="HTML")
            return

        if payload.startswith("pick_add_sub:"):
            _, candidate_raw = payload.split(":", 1)
            candidate_index = int(candidate_raw)
            session = ass_service.get_mux_session(msg.chat.id, callback.from_user.id)
            if not session or session.selected_item_index is None:
                raise RuntimeError("当前未选中视频项，请重新进入单集编辑")
            item_index = session.selected_item_index
            result_text = ass_service.pick_mux_add_sub_candidate(msg.chat.id, callback.from_user.id, candidate_index)
            ass_service.set_mux_inline_notice(msg.chat.id, callback.from_user.id, result_text)
            await callback.answer("已添加该字幕")
            text = ass_service.format_mux_item_detail(msg.chat.id, callback.from_user.id, item_index)
            kb = ass_service.build_mux_item_keyboard(msg.chat.id, callback.from_user.id, item_index)
            await msg.edit_text(text, reply_markup=kb, parse_mode="HTML")
            return

        if payload.startswith("prompt_subgroup:"):
            _, item_raw, sub_raw = payload.split(":", 2)
            item_index = int(item_raw)
            sub_index = int(sub_raw)
            session = ass_service.get_mux_session(msg.chat.id, callback.from_user.id)
            ass_service.set_mux_prompt(msg.chat.id, callback.from_user.id, field="track_group", item_index=item_index, sub_index=sub_index, message_id=session.awaiting_message_id if session and session.awaiting_message_id else msg.message_id)
            ass_service.clear_mux_inline_notice(msg.chat.id, callback.from_user.id)
            await callback.answer("请直接发送字幕组")
            await sync_ass_mux_view(callback.bot, msg.chat.id, callback.from_user.id)
            return

        if payload.startswith("remove_sub:"):
            _, item_raw, sub_raw = payload.split(":", 2)
            item_index = int(item_raw)
            sub_index = int(sub_raw)
            result_text = ass_service.remove_mux_subtitle_from_item(msg.chat.id, callback.from_user.id, item_index, sub_index)
            ass_service.set_mux_inline_notice(msg.chat.id, callback.from_user.id, result_text)
            await callback.answer("已删除该字幕")
            text = ass_service.format_mux_item_detail(msg.chat.id, callback.from_user.id, item_index)
            kb = ass_service.build_mux_item_keyboard(msg.chat.id, callback.from_user.id, item_index)
            await msg.edit_text(text, reply_markup=kb, parse_mode="HTML")
            return

        if payload.startswith("prompt_sublang:"):
            _, item_raw, sub_raw = payload.split(":", 2)
            item_index = int(item_raw)
            sub_index = int(sub_raw)
            session = ass_service.get_mux_session(msg.chat.id, callback.from_user.id)
            ass_service.set_mux_prompt(msg.chat.id, callback.from_user.id, field="track_lang", item_index=item_index, sub_index=sub_index, message_id=session.awaiting_message_id if session and session.awaiting_message_id else msg.message_id)
            ass_service.clear_mux_inline_notice(msg.chat.id, callback.from_user.id)
            await callback.answer("请直接发送字幕语言")
            await sync_ass_mux_view(callback.bot, msg.chat.id, callback.from_user.id)
            return

        if payload == "cancel_prompt":
            ass_service.clear_mux_prompt(msg.chat.id, callback.from_user.id)
            ass_service.set_mux_inline_notice(msg.chat.id, callback.from_user.id, 'ℹ️ 已取消当前输入。')
            await callback.answer("已取消当前输入")
            await sync_ass_mux_view(callback.bot, msg.chat.id, callback.from_user.id)
            return

        if payload == "back_plan":
            ass_service.clear_mux_prompt(msg.chat.id, callback.from_user.id)
            ass_service.clear_mux_inline_notice(msg.chat.id, callback.from_user.id)
            await callback.answer("返回计划列表")
            await sync_ass_mux_view(callback.bot, msg.chat.id, callback.from_user.id)
            return

        if payload == "run_confirm":
            ass_service.clear_mux_inline_notice(msg.chat.id, callback.from_user.id)
            await callback.answer("请确认是否执行")
            text = ass_service.format_mux_run_confirm(msg.chat.id, callback.from_user.id)
            kb = ass_service.build_mux_run_confirm_keyboard()
            await msg.edit_text(text, reply_markup=kb, parse_mode="HTML")
            return

        if payload == "run_now":
            session = ass_service.get_mux_session(msg.chat.id, callback.from_user.id)
            if not session or not session.plan:
                raise RuntimeError("当前会话已失效，请重新发送 /ass")
            total_items = ass_service.count_mux_executable_items(msg.chat.id, callback.from_user.id)
            progress_queue: asyncio.Queue[MuxProgressEvent | None] = asyncio.Queue()
            loop = asyncio.get_running_loop()

            def progress_callback(event: MuxProgressEvent) -> None:
                loop.call_soon_threadsafe(progress_queue.put_nowait, event)

            await callback.answer("开始执行字幕内封…")
            await msg.edit_text(
                format_mux_running(
                    processed=0,
                    total=total_items,
                    dry_run=bool(session.dry_run),
                ),
                parse_mode="HTML",
            )
            pump_task = asyncio.create_task(
                pump_ass_mux_progress(callback.bot, msg.chat.id, msg.message_id, session, progress_queue)
            )
            try:
                ok, text = await ass_service.run_mux(
                    callback.bot,
                    msg.chat.id,
                    callback.from_user.id,
                    progress_callback=progress_callback,
                )
            finally:
                await progress_queue.put(None)
                with contextlib.suppress(Exception):
                    await pump_task
            prefix = "✅" if ok else "❌"
            try:
                await msg.edit_text(text, parse_mode="HTML")
            except Exception:
                await msg.answer(f"{prefix} 字幕内封任务已完成，请查看机器人日志/汇总消息", parse_mode="HTML")
            return

        if payload == "cancel":
            ass_service.clear_mux_session(msg.chat.id, callback.from_user.id)
            await callback.answer("已结束本次会话")
            await msg.edit_text("❎ 已结束本次 /ass 字幕内封会话。", parse_mode="HTML")
            return

        await callback.answer()
    except Exception as exc:
        logging.exception("❌ /ass 字幕内封交互失败")
        await callback.answer("操作失败", show_alert=True)
        try:
            await msg.answer(f"❌ 操作失败\n\n<code>{html.escape(str(exc))}</code>", parse_mode="HTML")
        except Exception:
            pass


# ==================== Emby 任务面板回调 ====================

@router.callback_query(F.data.startswith(EMBY_TASK_CALLBACK_PREFIX))
async def callback_emby_tasks(callback: CallbackQuery):
    if not await check_callback_permission(callback):
        return

    msg = callback.message
    if not msg:
        await callback.answer()
        return

    await callback.answer()

    state_key = make_message_state_key(msg)
    state = emby_task_state.get(state_key or "")
    if not state:
        try:
            tasks = await emby_task_service.list_tasks()
        except Exception as exc:
            logging.exception("❌ 刷新 Emby 任务列表失败")
            await msg.edit_text(f"❌ 刷新 Emby 任务列表失败\n\n<code>{html.escape(str(exc))}</code>", parse_mode="HTML")
            return
        state = _build_emby_task_state(tasks, page=0, owner_user_id=callback.from_user.id, filter_mode="pro")
        if state_key:
            emby_task_state[state_key] = state
            trim_dict_cache(emby_task_state, EMBY_TASK_STATE_LIMIT)

    if state.get("owner_user_id") and state["owner_user_id"] != callback.from_user.id:
        await callback.answer("⛔️ 只能由发起人操作", show_alert=True)
        return

    data = callback.data[len(EMBY_TASK_CALLBACK_PREFIX):]
    tasks = state.get("tasks") or []
    page = int(state.get("page") or 0)
    filter_mode = str(state.get("filter_mode") or "all")

    async def _refresh_tasks_state(*, new_page: int | None = None, notice: str | None = None, force_tasks: list[dict] | None = None):
        fresh_tasks = force_tasks
        if fresh_tasks is None:
            fresh_tasks = await emby_task_service.list_tasks()
        state["tasks"] = fresh_tasks
        current_page = page if new_page is None else new_page
        state["page"] = current_page
        filtered = filter_tasks_for_view(fresh_tasks, filter_mode)
        summary = build_category_summary(fresh_tasks)
        header_note = describe_filter_mode(filter_mode)
        panel_text, kb = build_tasks_panel(
            filtered,
            page=current_page,
            summary=summary,
            notify_enabled=emby_task_service.notify_enabled,
            filter_mode=filter_mode,
            header_note=header_note,
            quick_actions=_build_pro_quick_actions(fresh_tasks, filter_mode),
        )
        if notice:
            panel_text = f"{notice}\n\n{panel_text}"
        await msg.edit_text(panel_text, reply_markup=kb, parse_mode="HTML")
        if state_key:
            emby_task_state[state_key] = state
            trim_dict_cache(emby_task_state, EMBY_TASK_STATE_LIMIT)

    try:
        if data == "refresh":
            await _refresh_tasks_state(new_page=page)
            return

        if data.startswith("page:"):
            try:
                new_page = int(data.split(":", 1)[1])
            except ValueError:
                await callback.answer("页码无效", show_alert=True)
                return
            await _refresh_tasks_state(new_page=new_page, force_tasks=tasks)
            return

        if data.startswith("view:"):
            requested_mode = (data.split(":", 1)[1] or "all").strip().lower()
            normalized_mode = requested_mode if requested_mode in {"all", "running", "pro", "queued", "completed", "failed"} else "all"
            if normalized_mode == filter_mode:
                await _refresh_tasks_state(new_page=0, force_tasks=tasks)
                return
            state["filter_mode"] = normalized_mode
            filter_mode = normalized_mode
            await _refresh_tasks_state(new_page=0, force_tasks=tasks)
            return

        if data == "notify":
            enabled = await emby_task_service.toggle_notify_enabled()
            status_text = "✅ 已开启后台通知" if enabled else "🔕 已关闭后台通知"
            await _refresh_tasks_state(new_page=page, notice=status_text)
            return

        if data.startswith("detail:"):
            task_id = data.split(":", 1)[1]
            detail = await emby_task_service.get_task(task_id)
            detail_text, kb = build_task_detail(detail, page=page, owner_user_id=callback.from_user.id)
            await msg.edit_text(detail_text, reply_markup=kb, parse_mode="HTML")
            return

        if data.startswith("back:"):
            page_raw = data.split(":", 1)[1]
            try:
                back_page = int(page_raw)
            except ValueError:
                back_page = 0
            await _refresh_tasks_state(new_page=back_page)
            return

        if data.startswith("quick_start:"):
            task_id = data.split(":", 1)[1]
            started = await emby_task_service.start_task(task_id)
            if not started:
                await callback.answer("❌ 启动失败", show_alert=True)
                return
            await callback.answer("✅ 已启动任务")
            await _refresh_tasks_state(new_page=page, notice="✅ 已启动任务")
            return

        if data.startswith("start:"):
            task_id = data.split(":", 1)[1]
            started = await emby_task_service.start_task(task_id)
            if not started:
                await callback.answer("❌ 启动失败", show_alert=True)
                return
            await callback.answer("✅ 已启动任务")
            try:
                detail = await emby_task_service.get_task(task_id)
                detail_text, kb = build_task_detail(detail, page=page, owner_user_id=callback.from_user.id)
                detail_text = f"✅ 已启动任务\n\n{detail_text}"
                await msg.edit_text(detail_text, reply_markup=kb, parse_mode="HTML")
            except Exception:
                await _refresh_tasks_state(new_page=page, notice="✅ 已启动任务")
            return

        if data.startswith("stop:"):
            task_id = data.split(":", 1)[1]
            stopped = await emby_task_service.stop_task(task_id)
            if not stopped:
                await callback.answer("❌ 停止失败", show_alert=True)
                return
            await callback.answer("🛑 已请求停止任务")
            try:
                detail = await emby_task_service.get_task(task_id)
                detail_text, kb = build_task_detail(detail, page=page, owner_user_id=callback.from_user.id)
                detail_text = f"🛑 已请求停止任务\n\n{detail_text}"
                await msg.edit_text(detail_text, reply_markup=kb, parse_mode="HTML")
            except Exception:
                await _refresh_tasks_state(new_page=page, notice="🛑 已请求停止任务")
            return

        await callback.answer("未知操作", show_alert=True)
    except Exception as exc:
        logging.exception("❌ 处理 Emby 任务回调失败")
        await msg.edit_text(f"❌ 操作失败\n\n<code>{html.escape(str(exc))}</code>", parse_mode="HTML")


# ==================== /rm_strm 确认按钮 ====================

@router.callback_query(F.data.startswith("rm_strm_confirm:"))
async def callback_rm_strm_confirm(callback: CallbackQuery):
    if not await check_callback_permission(callback):
        return

    msg = callback.message
    if not msg:
        await callback.answer()
        return

    state_key = make_message_state_key(msg)
    pending = rm_strm_pending_confirms.get(state_key or "")
    if not pending:
        await callback.answer("⚠️ 该预览已失效，请重新执行 /rm_strm", show_alert=True)
        return

    owner_user_id = pending.get("user_id")
    if owner_user_id and owner_user_id != callback.from_user.id:
        await callback.answer("只能由发起人确认删除", show_alert=True)
        return

    created_at = float(pending.get("created_at") or 0)
    if created_at and time.time() - created_at > RM_STRM_CONFIRM_TTL:
        rm_strm_pending_confirms.pop(state_key, None)
        await callback.answer("⚠️ 该预览已过期，请重新执行 /rm_strm", show_alert=True)
        return

    await callback.answer("⚠️ 正在执行实际删除...", show_alert=False)
    await msg.edit_text("⏳ 正在执行 STRM 空目录实际删除...", parse_mode="HTML")

    try:
        result = await strm_prune_service.execute(callback.from_user.id)
        preview_text = str(pending.get("preview_text") or "")
        result_text = result.get("message", "✅ 已完成 STRM 空目录清理")
        summary = result.get("summary") or {}
        deleted_total = int(summary.get("deleted_total", 0) or 0)
        deleted_dirs = int(summary.get("deleted_dirs", 0) or 0)
        deleted_roots = int(summary.get("deleted_roots", 0) or 0)

        final_text = (
            "✅ <b>STRM 空目录清理已完成</b>\n\n"
            f"📂 删除目录数: <code>{deleted_total}</code>"
        )
        if deleted_roots:
            final_text += f"\n🚩 其中根目录删除: <code>{deleted_roots}</code>"
        if deleted_dirs:
            final_text += f"\n📁 其中普通目录删除: <code>{deleted_dirs}</code>"
        final_text += f"\n\n{result_text}"
        if preview_text:
            final_text += f"\n\n<b>删除前预览：</b>\n{preview_text}"

        if state_key:
            rm_strm_pending_confirms.pop(state_key, None)
        await msg.edit_text(final_text, parse_mode="HTML")
    except Exception as exc:
        logging.exception("❌ 执行 STRM 空目录清理失败")
        preview_text = str(pending.get("preview_text") or "")
        if state_key:
            rm_strm_pending_confirms.pop(state_key, None)
        await msg.edit_text(
            f"❌ STRM 空目录清理失败\n\n<code>{html.escape(str(exc))}</code>"
            + (f"\n\n<b>删除前预览：</b>\n{preview_text}" if preview_text else ""),
            parse_mode="HTML",
        )


@router.callback_query(F.data.startswith("rm_strm_cancel:"))
async def callback_rm_strm_cancel(callback: CallbackQuery):
    if not await check_callback_permission(callback):
        return

    msg = callback.message
    if not msg:
        await callback.answer()
        return

    state_key = make_message_state_key(msg)
    pending = rm_strm_pending_confirms.get(state_key or "")
    if not pending:
        await callback.answer("⚠️ 该预览已失效", show_alert=True)
        return

    owner_user_id = pending.get("user_id")
    if owner_user_id and owner_user_id != callback.from_user.id:
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
    await hdhive_openapi_flow_service.handle_provider_filter_callback(callback)


@router.callback_query(F.data.startswith("tmdb_page:"))
async def callback_tmdb_page(callback: CallbackQuery):
    """切换 TMDB 候选列表分页。"""
    if not await check_callback_permission(callback):
        return
    await hdhive_openapi_flow_service.handle_tmdb_page_callback(callback)

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
    await hdhive_openapi_flow_service.handle_resource_callback(callback)


@router.callback_query(F.data.startswith("unlock:"))
async def callback_unlock_resource(callback: CallbackQuery):
    """
    处理解锁确认回调
    
    Callback data 格式: unlock:resource_id
    """
    if not await check_callback_permission(callback):
        return
    await hdhive_openapi_flow_service.handle_unlock_callback(callback)


@router.callback_query(F.data == "cancel_unlock")
async def callback_cancel_unlock(callback: CallbackQuery):
    """处理取消解锁回调"""
    if not await check_callback_permission(callback):
        return
    await hdhive_openapi_flow_service.handle_cancel_unlock_callback(callback)


async def auto_add_to_sa(task_key: str, link: str, original_message: Message, countdown: int = 60):
    await hdhive_openapi_flow_service.auto_add_to_sa(task_key, link, original_message, countdown=countdown)


@router.callback_query(F.data.startswith("send_to_group:"))
async def callback_send_to_sa(callback: CallbackQuery):
    """
    发送115链接到SA（Symedia）
    
    Callback data 格式: send_to_group:115_link
    """
    if not await check_callback_permission(callback):
        return
    await hdhive_openapi_flow_service.handle_send_to_sa_callback(callback)


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
            ass_service.set_mux_inline_notice(message.chat.id, message.from_user.id, result_text)
            await sync_ass_mux_view(message.bot, message.chat.id, message.from_user.id)
        except Exception as exc:
            logging.exception("❌ 处理 /ass 字幕内封输入失败")
            ass_service.set_mux_inline_notice(message.chat.id, message.from_user.id, f"❌ <b>输入无效</b>\n\n<code>{html.escape(str(exc))}</code>")
            await sync_ass_mux_view(message.bot, message.chat.id, message.from_user.id)
        return

    await hdhive_openapi_flow_service.handle_direct_link_message(message)


@router.callback_query(F.data.startswith("select_tmdb:"))
async def callback_select_tmdb(callback: CallbackQuery):
    """处理用户选择TMDB搜索结果。"""
    if not await check_callback_permission(callback):
        return
    await hdhive_openapi_flow_service.handle_select_tmdb_callback(callback)
