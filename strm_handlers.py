"""
STRM Telegram 状态命令处理器
"""
from __future__ import annotations

import html
import logging
import time

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from config import ALLOWED_USER_ID
from formatter import format_error_message
from strm_reason import BATCH_STATUS_ACTIVE, STATUS_ALREADY_OK, STATUS_DONE, STATUS_FAILED, STATUS_MISSING, STATUS_PENDING, STATUS_PROCESSING
from strm_service import strm_service

router = Router()


def format_ts(ts: float | int | None) -> str:
    if not ts:
        return "-"
    try:
        return time.strftime("%m-%d %H:%M:%S", time.localtime(float(ts)))
    except Exception:
        return "-"


def render_batch_lines(batch: dict) -> list[str]:
    counts = batch.get("counts") or {}
    status = str(batch.get("status") or BATCH_STATUS_ACTIVE)
    status_label = str(batch.get("status_label") or status)
    folder_key = html.escape(str(batch.get("folder_key") or "-"))
    lines = [
        f"• <code>{folder_key}</code>",
        f"  状态: <b>{html.escape(status_label)}</b>",
        (
            "  统计: "
            f"P<b>{counts.get(STATUS_PENDING, 0)}</b> / "
            f"R<b>{counts.get(STATUS_PROCESSING, 0)}</b> / "
            f"D<b>{counts.get(STATUS_DONE, 0)}</b> / "
            f"OK<b>{counts.get(STATUS_ALREADY_OK, 0)}</b> / "
            f"F<b>{counts.get(STATUS_FAILED, 0)}</b> / "
            f"M<b>{counts.get(STATUS_MISSING, 0)}</b>"
        ),
        f"  扫描: <code>{html.escape(format_ts(batch.get('last_scan_at')))}</code>",
        f"  更新: <code>{html.escape(format_ts(batch.get('updated_at')))}</code>",
    ]
    samples = batch.get("samples") or []
    if samples:
        lines.append(f"  阻塞样本: <code>{html.escape(', '.join(samples))}</code>")
    return lines


async def check_user_permission(message: Message) -> bool:
    if ALLOWED_USER_ID == 0:
        return True

    user_id = message.from_user.id
    if user_id != ALLOWED_USER_ID:
        await message.reply(format_error_message('permission_denied'), parse_mode="HTML")
        logging.warning("❌ 用户 %s (%s) 尝试使用 STRM 状态命令但被拒绝", user_id, message.from_user.username)
        return False
    return True


@router.message(Command("strm_status"))
async def cmd_strm_status(message: Message):
    if not await check_user_permission(message):
        return

    st = strm_service.status()
    lines = [
        "🧩 <b>STRM 监控状态</b>",
        "",
        f"启用: <code>{st['enabled']}</code>",
        f"已启动: <code>{st['started']}</code>",
        f"运行中: <code>{st['running']}</code>",
        f"WATCH_DIR: <code>{html.escape(st['watch_dir'] or '-')}</code>",
        f"DONE_DIR: <code>{html.escape(st['done_dir'] or '-')}</code>",
        f"FAILED_DIR: <code>{html.escape(st['failed_dir'] or '-')}</code>",
        f"STATE_DIR: <code>{html.escape(st.get('state_dir') or '-')}</code>",
        f"LAST_ERROR: <code>{html.escape(st['last_error'] or '-')}</code>",
    ]
    if st.get("batch_status_error"):
        lines.append(f"BATCH_STATUS_ERROR: <code>{html.escape(st['batch_status_error'])}</code>")

    lines.extend(
        [
            "",
            "📦 <b>批次看板</b>",
            f"manifest 总数: <b>{st.get('manifest_total', 0)}</b>",
            f"活跃批次: <b>{st.get('active_total', 0)}</b>",
            f"阻塞批次: <b>{st.get('blocked_total', 0)}</b>",
        ]
    )

    batches = st.get("batches") or []
    if batches:
        lines.append("")
        lines.append("🗂 <b>最近批次</b>")
        for batch in batches:
            lines.extend(render_batch_lines(batch))
    else:
        lines.append("")
        lines.append("🗂 <b>最近批次</b>")
        lines.append("暂无 manifest 记录")

    await message.reply("\n".join(lines), parse_mode="HTML")


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
