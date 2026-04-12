"""
STRM Telegram 状态命令处理器
"""
from __future__ import annotations

import logging

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from config import ALLOWED_USER_ID
from formatter import format_error_message
from strm_service import strm_service

router = Router()


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
