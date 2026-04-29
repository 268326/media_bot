from __future__ import annotations

import asyncio
import html
import logging
import threading

from aiogram import Bot

from ass_config import load_ass_settings_from_env
from ass_pipeline import AssRunSummary, run_ass_pipeline
from ass_utils import AssPipelineError

logger = logging.getLogger(__name__)


class AssService:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.running = False
        self.last_error = ''

    async def run(self, bot: Bot, trigger_chat_id: int) -> tuple[bool, str]:
        acquired = self.lock.acquire(blocking=False)
        if not acquired:
            return False, '⚠️ 已有 /ass 任务在执行，请稍后再试'
        self.running = True
        self.last_error = ''
        try:
            settings = load_ass_settings_from_env()
            summary = await asyncio.to_thread(run_ass_pipeline, settings)
            text = self._format_summary(summary)
            await self._notify(bot, trigger_chat_id, settings.notify_chat_id, text)
            return summary.failed == 0, text
        except Exception as exc:
            self.last_error = str(exc)
            logger.exception('❌ /ass 执行失败')
            text = f'❌ <b>/ass 执行失败</b>\n\n<code>{html.escape(str(exc))}</code>'
            await self._notify(bot, trigger_chat_id, load_ass_settings_from_env().notify_chat_id, text)
            return False, text
        finally:
            self.running = False
            self.lock.release()

    async def _notify(self, bot: Bot, trigger_chat_id: int, notify_chat_id: str, text: str) -> None:
        target = str(notify_chat_id or '').strip()
        if not target:
            return
        if target == str(trigger_chat_id):
            return
        try:
            await bot.send_message(chat_id=int(target), text=text, parse_mode='HTML')
        except Exception:
            logger.exception('⚠️ 发送 /ass 汇总通知失败: chat_id=%s', target)

    def _format_summary(self, summary: AssRunSummary) -> str:
        prefix = '✅' if summary.failed == 0 else '⚠️'
        lines = [
            f'{prefix} <b>/ass 执行完成</b>',
            '',
            f'目录: <code>{html.escape(summary.target_dir)}</code>',
            f'总 ASS: <code>{summary.total_ass}</code>',
            f'处理成功: <code>{summary.processed}</code>',
            f'已跳过: <code>{summary.skipped}</code>',
            f'失败: <code>{summary.failed}</code>',
            f'字体目录: <code>{summary.font_dirs}</code>',
            f'字体包: <code>{summary.archives}</code>',
            f'OTF→TTF 成功: <code>{summary.converted_otf}</code>',
            f'OTF 跳过: <code>{summary.skipped_otf}</code>',
            f'耗时: <code>{summary.duration_s:.1f}s</code>',
        ]
        if summary.failures:
            lines.extend(['', '<b>失败明细：</b>'])
            for item in summary.failures[:10]:
                lines.append(f'• <code>{html.escape(item)}</code>')
            if len(summary.failures) > 10:
                lines.append(f'• ... 其余 <code>{len(summary.failures) - 10}</code> 项已省略')
        return '\n'.join(lines)


ass_service = AssService()
