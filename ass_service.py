from __future__ import annotations

import asyncio
import html
import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message

from ass_config import load_ass_settings_from_env
from ass_mux_config import AssMuxSettings, load_ass_mux_settings_from_env
from ass_mux_pipeline import MuxRunSummary, collect_mux_plan_stats, fmt_bytes, run_mux_plan
from ass_mux_planner import (
    MuxPlan,
    MuxPlanItem,
    SubtitleTrackPlan,
    build_mux_plan,
    format_mux_plan_preview,
    parse_lang,
    short_ep_display,
    short_title_from_mkv,
    write_mux_plan,
)
from ass_pipeline import AssRunSummary, run_ass_pipeline
from ass_utils import AssPipelineError

logger = logging.getLogger(__name__)

ASS_MENU_PREFIX = "ass_menu:"
ASS_MUX_PREFIX = "ass_mux:"
ASS_MUX_SESSION_TTL = 1800


@dataclass(slots=True)
class AssMuxSession:
    chat_id: int
    owner_user_id: int
    settings: AssMuxSettings
    plan: MuxPlan | None = None
    default_group: str = ""
    default_lang: str = "chs"
    delete_external_subs: bool = False
    dry_run: bool = False
    plan_page: int = 0
    selected_item_index: int | None = None
    selected_sub_index: int | None = None
    awaiting_field: str | None = None
    awaiting_message_id: int | None = None
    created_at: float = field(default_factory=time.monotonic)
    updated_at: float = field(default_factory=time.monotonic)

    def touch(self) -> None:
        now = time.monotonic()
        self.updated_at = now
        if self.created_at <= 0:
            self.created_at = now


class AssService:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.running = False
        self.last_error = ''
        self.mux_sessions: dict[str, AssMuxSession] = {}

    @staticmethod
    def _session_key(chat_id: int, owner_user_id: int) -> str:
        return f'{chat_id}:{owner_user_id}'

    async def run_subset(self, bot: Bot, trigger_chat_id: int) -> tuple[bool, str]:
        acquired = self.lock.acquire(blocking=False)
        if not acquired:
            return False, '⚠️ 已有 /ass 任务在执行，请稍后再试'
        self.running = True
        self.last_error = ''
        try:
            settings = load_ass_settings_from_env()
            summary = await asyncio.to_thread(run_ass_pipeline, settings)
            text = self._format_subset_summary(summary)
            await self._notify(bot, trigger_chat_id, settings.notify_chat_id, text)
            return summary.failed == 0, text
        except Exception as exc:
            self.last_error = str(exc)
            logger.exception('❌ /ass 字体子集化执行失败')
            text = f'❌ <b>/ass 字体子集化执行失败</b>\n\n<code>{html.escape(str(exc))}</code>'
            await self._notify(bot, trigger_chat_id, load_ass_settings_from_env().notify_chat_id, text)
            return False, text
        finally:
            self.running = False
            self.lock.release()

    async def build_mux_menu(self, message: Message) -> tuple[str, InlineKeyboardMarkup]:
        settings = load_ass_mux_settings_from_env()
        text = (
            '🎬 <b>/ass 功能菜单</b>\n\n'
            '请选择要执行的功能：\n'
            '• <b>子集化字体</b>：扫描 ASS / 字体 / 压缩包，生成 <code>.assfonts.ass</code>\n'
            '• <b>内封字幕</b>：把同目录匹配到的 <code>.ass/.sup</code> 内封进 <code>.mkv</code>\n\n'
            f'字幕内封目录: <code>{html.escape(str(settings.target_dir))}</code>\n'
            f'默认语言: <code>{html.escape(settings.default_lang)}</code>\n'
            f'并发: <code>{settings.jobs}</code>'
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text='🔤 子集化字体', callback_data=f'{ASS_MENU_PREFIX}subset'),
                InlineKeyboardButton(text='🎞️ 内封字幕', callback_data=f'{ASS_MENU_PREFIX}mux_start'),
            ]
        ])
        return text, kb

    async def start_mux_session(self, *, chat_id: int, owner_user_id: int) -> AssMuxSession:
        self.cleanup_mux_sessions()
        settings = load_ass_mux_settings_from_env()
        session = AssMuxSession(
            chat_id=chat_id,
            owner_user_id=owner_user_id,
            settings=settings,
            default_group=settings.default_group,
            default_lang=settings.default_lang,
            delete_external_subs=settings.delete_external_subs_default,
        )
        self.mux_sessions[self._session_key(chat_id, owner_user_id)] = session
        logger.info('🎬 创建 /ass 字幕内封会话: chat_id=%s user_id=%s target=%s', chat_id, owner_user_id, settings.target_dir)
        return session

    def get_mux_session(self, chat_id: int, owner_user_id: int | None = None) -> AssMuxSession | None:
        self.cleanup_mux_sessions()
        if owner_user_id is not None:
            return self.mux_sessions.get(self._session_key(chat_id, owner_user_id))
        matches = [session for session in self.mux_sessions.values() if session.chat_id == chat_id]
        if len(matches) == 1:
            return matches[0]
        return None

    def cleanup_mux_sessions(self) -> None:
        if not self.mux_sessions:
            return
        now = time.monotonic()
        expired = [
            chat_id
            for chat_id, session in self.mux_sessions.items()
            if now and now - session.updated_at > ASS_MUX_SESSION_TTL
        ]
        for chat_id in expired:
            self.mux_sessions.pop(chat_id, None)

    def clear_mux_session(self, chat_id: int, owner_user_id: int) -> None:
        self.mux_sessions.pop(self._session_key(chat_id, owner_user_id), None)

    def ensure_mux_owner(self, chat_id: int, user_id: int) -> bool:
        session = self.get_mux_session(chat_id, user_id)
        if not session:
            return False
        return session.owner_user_id == user_id

    async def build_mux_settings_summary(self, chat_id: int, owner_user_id: int) -> str:
        session = self.get_mux_session(chat_id, owner_user_id)
        if not session:
            raise AssPipelineError('当前没有进行中的字幕内封会话，请重新发送 /ass')
        session.touch()
        return self._format_mux_session(session)

    async def build_mux_panel_text(self, chat_id: int, owner_user_id: int) -> str:
        session = self.get_mux_session(chat_id, owner_user_id)
        if not session:
            raise AssPipelineError('当前没有进行中的字幕内封会话，请重新发送 /ass')
        session.touch()
        base = self._format_mux_session(session)
        if not session.plan:
            return base

        preview_lines = ['🎞️ <b>当前页计划预览</b>', '']
        current_items = self._current_plan_page_items(session)
        for index, item in current_items:
            title = short_title_from_mkv(Path(item.mkv).name) or Path(item.mkv).stem
            ep = short_ep_display(Path(item.mkv).name)
            prefix = f'{index + 1}. {ep} {title}'.strip()
            preview_lines.append(f'• <code>{html.escape(prefix)}</code>')
            preview_lines.append(f'  ↳ MKV: <code>{html.escape(item.mkv)}</code>')
            for sub_idx, sub in enumerate(item.subs, 1):
                preview_lines.append(
                    f'  ↳ 字幕{sub_idx}: <code>{html.escape(Path(sub.file).name)}</code> / '
                    f'<code>{html.escape(sub.track_name)}</code> / <code>{html.escape(sub.mkv_lang)}</code>'
                )
            preview_lines.append('')
        if not current_items:
            preview_lines.append('• <code>当前页没有条目</code>')
        return base + '\n\n' + '\n'.join(preview_lines).rstrip()

    async def rebuild_mux_plan(self, chat_id: int, owner_user_id: int) -> tuple[AssMuxSession, str]:
        session = self.get_mux_session(chat_id, owner_user_id)
        if not session:
            raise AssPipelineError('当前没有进行中的字幕内封会话，请重新发送 /ass')
        session.touch()
        plan = await asyncio.to_thread(
            build_mux_plan,
            session.settings,
            default_group=session.default_group,
            default_lang=session.default_lang,
        )
        session.plan = plan
        session.plan_page = 0
        write_mux_plan(plan, session.settings.plan_path.expanduser().resolve())
        logger.info('🎬 /ass 生成字幕内封计划: chat_id=%s items=%s tracks=%s plan=%s', chat_id, len(plan.items), plan.total_sub_tracks, session.settings.plan_path)
        text = format_mux_plan_preview(plan)
        return session, text

    def _current_plan_page_items(self, session: AssMuxSession, *, page_size: int = 8) -> list[tuple[int, MuxPlanItem]]:
        if not session.plan:
            return []
        total_items = len(session.plan.items)
        total_pages = max(1, (total_items + page_size - 1) // page_size)
        session.plan_page = min(max(session.plan_page, 0), total_pages - 1)
        start = session.plan_page * page_size
        end = min(start + page_size, total_items)
        return [(index, session.plan.items[index]) for index in range(start, end)]

    def build_mux_plan_keyboard(self, chat_id: int, owner_user_id: int) -> InlineKeyboardMarkup:
        session = self.get_mux_session(chat_id, owner_user_id)
        if not session:
            raise AssPipelineError('当前没有进行中的字幕内封会话，请重新发送 /ass')

        rows: list[list[InlineKeyboardButton]] = []
        if session.plan:
            current_items = self._current_plan_page_items(session)
            for index, _item in current_items:
                rows.append([
                    InlineKeyboardButton(text=f'编辑 {index + 1}', callback_data=f'{ASS_MUX_PREFIX}edit_item:{index}')
                ])

            page_size = 8
            total_items = len(session.plan.items)
            total_pages = max(1, (total_items + page_size - 1) // page_size)
            if total_pages > 1:
                nav_row: list[InlineKeyboardButton] = []
                if session.plan_page > 0:
                    nav_row.append(InlineKeyboardButton(text='⬅️ 上一页', callback_data=f'{ASS_MUX_PREFIX}page:{session.plan_page - 1}'))
                nav_row.append(InlineKeyboardButton(text=f'{session.plan_page + 1}/{total_pages}', callback_data=f'{ASS_MUX_PREFIX}page:{session.plan_page}'))
                if session.plan_page < total_pages - 1:
                    nav_row.append(InlineKeyboardButton(text='下一页 ➡️', callback_data=f'{ASS_MUX_PREFIX}page:{session.plan_page + 1}'))
                rows.append(nav_row)

        rows.append([
            InlineKeyboardButton(text=f'删除外挂字幕: {"开" if session.delete_external_subs else "关"}', callback_data=f'{ASS_MUX_PREFIX}toggle_delete'),
            InlineKeyboardButton(text=f'DRY-RUN: {"开" if session.dry_run else "关"}', callback_data=f'{ASS_MUX_PREFIX}toggle_dry'),
        ])
        rows.append([
            InlineKeyboardButton(text='✏️ 改默认字幕组', callback_data=f'{ASS_MUX_PREFIX}prompt_group'),
            InlineKeyboardButton(text='🌐 改默认语言', callback_data=f'{ASS_MUX_PREFIX}prompt_lang'),
        ])
        action_row = [InlineKeyboardButton(text='🔄 重新扫描生成计划', callback_data=f'{ASS_MUX_PREFIX}refresh')]
        if session.plan:
            action_row.append(InlineKeyboardButton(text='▶️ 开始执行', callback_data=f'{ASS_MUX_PREFIX}run_confirm'))
        rows.append(action_row)
        rows.append([
            InlineKeyboardButton(text='❎ 结束本次会话', callback_data=f'{ASS_MUX_PREFIX}cancel')
        ])
        return InlineKeyboardMarkup(inline_keyboard=rows)

    def build_mux_item_keyboard(self, chat_id: int, owner_user_id: int, item_index: int) -> InlineKeyboardMarkup:
        session = self.get_mux_session(chat_id, owner_user_id)
        if not session or not session.plan:
            raise AssPipelineError('当前还没有生成计划')
        item = session.plan.items[item_index]
        rows: list[list[InlineKeyboardButton]] = []
        for sub_index, _sub in enumerate(item.subs, 1):
            rows.append([
                InlineKeyboardButton(text=f'改字幕 {sub_index} 文件', callback_data=f'{ASS_MUX_PREFIX}prompt_subfile:{item_index}:{sub_index - 1}'),
            ])
            rows.append([
                InlineKeyboardButton(text=f'改字幕 {sub_index} 字幕组', callback_data=f'{ASS_MUX_PREFIX}prompt_subgroup:{item_index}:{sub_index - 1}'),
                InlineKeyboardButton(text=f'改字幕 {sub_index} 语言', callback_data=f'{ASS_MUX_PREFIX}prompt_sublang:{item_index}:{sub_index - 1}'),
            ])
        rows.append([
            InlineKeyboardButton(text='⬅️ 返回计划列表', callback_data=f'{ASS_MUX_PREFIX}back_plan')
        ])
        return InlineKeyboardMarkup(inline_keyboard=rows)

    def build_mux_run_confirm_keyboard(self) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text='▶️ 确认执行', callback_data=f'{ASS_MUX_PREFIX}run_now'),
                InlineKeyboardButton(text='⬅️ 返回', callback_data=f'{ASS_MUX_PREFIX}back_plan'),
            ]
        ])

    def format_mux_run_confirm(self, chat_id: int, owner_user_id: int) -> str:
        session = self.get_mux_session(chat_id, owner_user_id)
        if not session or not session.plan:
            raise AssPipelineError('当前还没有生成计划')
        stats = collect_mux_plan_stats(session.settings, session.plan)
        free_bytes = int(stats['tmp_free_bytes'])
        total_bytes = int(stats['tmp_total_bytes'])
        est_tmp = int(stats['estimated_tmp_bytes'])
        total_source = int(stats['total_source_size_bytes'])
        avg_source = int(stats['avg_source_size_bytes'])
        max_source = int(stats['max_source_size_bytes'])
        same_fs_count = int(stats['same_fs_count'])
        missing_count = int(stats['missing_count'])
        duplicate_subtitle_refs = int(stats['duplicate_subtitle_refs'])
        temp_same_fs = bool(stats['temp_same_filesystem'])

        lines = [
            '⚠️ <b>确认执行字幕内封</b>',
            '',
            f'目录: <code>{html.escape(str(session.settings.target_dir))}</code>',
            f'计划视频: <code>{len(session.plan.items)}</code>',
            f'字幕轨道: <code>{session.plan.total_sub_tracks}</code>',
            f'并发: <code>{session.settings.jobs}</code>',
            f'DRY-RUN: <code>{session.dry_run}</code>',
            f'删除外挂字幕: <code>{session.delete_external_subs}</code>',
            f'超时保护: <code>标准模式</code>（空闲 <code>{session.settings.idle_timeout_s}s</code> / 软告警 <code>{session.settings.soft_warn_after_s}s</code> / 极限 <code>{session.settings.hard_cap_s}s</code>）',
            '',
            '<b>执行前资源评估：</b>',
            f'源视频总大小: <code>{fmt_bytes(total_source)}</code>',
            f'单集大小: 平均 <code>{fmt_bytes(avg_source)}</code> / 最大 <code>{fmt_bytes(max_source)}</code>',
            f'预计临时占用: <code>{fmt_bytes(est_tmp)}</code>',
            f'临时目录剩余: <code>{fmt_bytes(free_bytes)}</code>' + (f' / <code>{fmt_bytes(total_bytes)}</code>' if total_bytes > 0 else ''),
            f'临时目录与视频同分区: <code>{temp_same_fs}</code> (<code>{same_fs_count}</code>/<code>{len(session.plan.items)}</code>)',
        ]
        if duplicate_subtitle_refs > 0:
            lines.append(f'重复引用字幕文件: <code>{duplicate_subtitle_refs}</code>')
        if missing_count > 0:
            lines.append(f'缺失 MKV: <code>{missing_count}</code>')
        if free_bytes > 0 and est_tmp > free_bytes:
            lines.append('⚠️ <b>警告：</b> 临时目录剩余空间可能不足，建议先调小并发或修改临时目录。')
        lines.extend(['', '确认后将开始调用 <code>mkvmerge</code> 写回视频文件。'])
        return '\n'.join(lines)

    def format_mux_item_detail(self, chat_id: int, owner_user_id: int, item_index: int) -> str:
        session = self.get_mux_session(chat_id, owner_user_id)
        if not session or not session.plan:
            raise AssPipelineError('当前还没有生成计划')
        item = session.plan.items[item_index]
        lines = [
            f'📝 <b>编辑第 {item_index + 1} 项</b>',
            '',
            f'MKV: <code>{html.escape(item.mkv)}</code>',
        ]
        for idx, sub in enumerate(item.subs, 1):
            lines.extend([
                '',
                f'<b>字幕 {idx}</b>',
                f'文件: <code>{html.escape(sub.file)}</code>',
                f'字幕组: <code>{html.escape(sub.group or "-")}</code>',
                f'语言输入: <code>{html.escape(sub.lang_raw)}</code>',
                f'轨道语言: <code>{html.escape(sub.mkv_lang)}</code>',
                f'轨道名: <code>{html.escape(sub.track_name)}</code>',
            ])
        return '\n'.join(lines)

    def list_mux_candidate_subs(self, chat_id: int, owner_user_id: int, item_index: int) -> list[str]:
        session = self.get_mux_session(chat_id, owner_user_id)
        if not session or not session.plan:
            raise AssPipelineError('当前还没有生成计划')
        item = session.plan.items[item_index]
        item_dir = (session.settings.target_dir.expanduser().resolve() / Path(item.mkv).parent).resolve()
        if not item_dir.is_dir():
            return []
        candidates: list[str] = []
        for path in sorted(item_dir.iterdir()):
            if path.is_file() and path.suffix.lower() in ('.ass', '.sup'):
                candidates.append(path.name)
        return candidates

    def set_mux_prompt(self, chat_id: int, owner_user_id: int, *, field: str, item_index: int | None = None, sub_index: int | None = None, message_id: int | None = None) -> None:
        session = self.get_mux_session(chat_id, owner_user_id)
        if not session:
            raise AssPipelineError('当前没有进行中的字幕内封会话，请重新发送 /ass')
        session.selected_item_index = item_index
        session.selected_sub_index = sub_index
        session.awaiting_field = field
        session.awaiting_message_id = message_id
        session.touch()

    def clear_mux_prompt(self, chat_id: int, owner_user_id: int) -> None:
        session = self.get_mux_session(chat_id, owner_user_id)
        if not session:
            return
        session.selected_item_index = None
        session.selected_sub_index = None
        session.awaiting_field = None
        session.awaiting_message_id = None
        session.touch()

    def _parse_lang_or_raise(self, raw: str) -> tuple[str, str]:
        mkv_lang, lang_cn = parse_lang(raw)
        if mkv_lang == 'und' and lang_cn == '未知':
            raise AssPipelineError('无法识别语言，请输入如 chs / cht / eng / jpn / chs_eng')
        return mkv_lang, lang_cn

    def _resolve_track(self, session: AssMuxSession) -> tuple[MuxPlanItem, SubtitleTrackPlan]:
        if session.plan is None:
            raise AssPipelineError('当前还没有生成计划')
        if session.selected_item_index is None or session.selected_sub_index is None:
            raise AssPipelineError('当前没有选中要编辑的字幕项')
        try:
            item = session.plan.items[session.selected_item_index]
            sub = item.subs[session.selected_sub_index]
        except IndexError as exc:
            raise AssPipelineError('选中的字幕项不存在，请重新生成计划') from exc
        return item, sub

    def apply_mux_text_input(self, chat_id: int, owner_user_id: int, text: str) -> str:
        session = self.get_mux_session(chat_id, owner_user_id)
        if not session:
            raise AssPipelineError('当前没有进行中的字幕内封会话，请重新发送 /ass')
        if not session.awaiting_field:
            raise AssPipelineError('当前没有等待输入的字段')

        raw = text.strip()
        field = session.awaiting_field

        if field == 'default_group':
            session.default_group = '' if raw == '-' else raw
            self.clear_mux_prompt(chat_id, owner_user_id)
            return f'✅ 默认字幕组已更新为: <code>{html.escape(session.default_group or "-")}</code>\n请点击“重新扫描生成计划”使其生效。'

        if field == 'default_lang':
            if not raw:
                raise AssPipelineError('默认语言不能为空，例如 chs / cht / eng / chs_eng')
            self._parse_lang_or_raise(raw)
            session.default_lang = raw
            self.clear_mux_prompt(chat_id, owner_user_id)
            return f'✅ 默认语言已更新为: <code>{html.escape(session.default_lang)}</code>\n请点击“重新扫描生成计划”使其生效。'

        item, sub = self._resolve_track(session)

        if field == 'sub_file':
            if not raw:
                raise AssPipelineError('字幕文件名不能为空')
            candidate_name = Path(raw).name
            if candidate_name != raw:
                raise AssPipelineError('请只输入同目录字幕文件名，不要带路径')
            candidate = Path(item.mkv).parent / candidate_name
            full_path = (session.settings.target_dir.expanduser().resolve() / candidate).resolve()
            target_root = session.settings.target_dir.expanduser().resolve()
            try:
                full_path.relative_to(target_root)
            except ValueError as exc:
                raise AssPipelineError('字幕文件超出工作目录范围') from exc
            if not full_path.is_file():
                raise AssPipelineError(f'字幕文件不存在: {candidate}')
            if full_path.suffix.lower() not in ('.ass', '.sup'):
                raise AssPipelineError('只支持 .ass 或 .sup 字幕文件')
            sub.file = str(candidate)
            self.clear_mux_prompt(chat_id, owner_user_id)
            session.touch()
            return f'✅ 字幕文件已更新为: <code>{html.escape(sub.file)}</code>'

        if field == 'track_group':
            sub.group = '' if raw == '-' else raw
            mkv_lang, lang_cn = self._parse_lang_or_raise(sub.lang_raw)
            sub.mkv_lang = mkv_lang
            sub.track_name = f'{sub.group} | {lang_cn}' if sub.group else lang_cn
            self.clear_mux_prompt(chat_id, owner_user_id)
            session.touch()
            return (
                '✅ 字幕组已更新\n'
                f'轨道名: <code>{html.escape(sub.track_name)}</code>\n'
                f'语言: <code>{html.escape(sub.mkv_lang)}</code>'
            )

        if field == 'track_lang':
            if not raw:
                raise AssPipelineError('语言不能为空，例如 chs / cht / eng / chs_eng')
            mkv_lang, lang_cn = self._parse_lang_or_raise(raw)
            sub.lang_raw = raw
            sub.mkv_lang = mkv_lang
            sub.track_name = f'{sub.group} | {lang_cn}' if sub.group else lang_cn
            self.clear_mux_prompt(chat_id, owner_user_id)
            session.touch()
            return (
                '✅ 字幕语言已更新\n'
                f'轨道名: <code>{html.escape(sub.track_name)}</code>\n'
                f'语言: <code>{html.escape(sub.mkv_lang)}</code>'
            )

        raise AssPipelineError('未知输入字段，请重新发送 /ass')

    async def run_mux(self, bot: Bot, chat_id: int, owner_user_id: int) -> tuple[bool, str]:
        session = self.get_mux_session(chat_id, owner_user_id)
        if not session:
            raise AssPipelineError('当前没有进行中的字幕内封会话，请重新发送 /ass')
        if session.plan is None:
            raise AssPipelineError('当前还没有生成计划，请先生成计划')

        acquired = self.lock.acquire(blocking=False)
        if not acquired:
            return False, '⚠️ 已有 /ass 任务在执行，请稍后再试'
        self.running = True
        self.last_error = ''
        try:
            write_mux_plan(session.plan, session.settings.plan_path.expanduser().resolve())
            summary = await asyncio.to_thread(
                run_mux_plan,
                session.settings,
                session.plan,
                dry_run=session.dry_run,
                delete_external_subs=session.delete_external_subs,
            )
            text = self._format_mux_summary(summary)
            await self._notify(bot, chat_id, session.settings.notify_chat_id, text)
            self.clear_mux_session(chat_id, owner_user_id)
            return summary.failed == 0, text
        except Exception as exc:
            self.last_error = str(exc)
            logger.exception('❌ /ass 字幕内封执行失败')
            text = f'❌ <b>/ass 字幕内封执行失败</b>\n\n<code>{html.escape(str(exc))}</code>'
            await self._notify(bot, chat_id, load_ass_mux_settings_from_env().notify_chat_id, text)
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

    def _format_subset_summary(self, summary: AssRunSummary) -> str:
        prefix = '✅' if summary.failed == 0 else '⚠️'
        lines = [
            f'{prefix} <b>/ass 子集化字体执行完成</b>',
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

    def _format_mux_session(self, session: AssMuxSession) -> str:
        lines = [
            '🎞️ <b>/ass 字幕内封设置</b>',
            '',
            f'目录: <code>{html.escape(str(session.settings.target_dir))}</code>',
            f'递归扫描: <code>{session.settings.recursive}</code>',
            f'默认字幕组: <code>{html.escape(session.default_group or "-")}</code>',
            f'默认语言: <code>{html.escape(session.default_lang)}</code>',
            f'并发: <code>{session.settings.jobs}</code>',
            f'删除外挂字幕: <code>{session.delete_external_subs}</code>',
            f'DRY-RUN: <code>{session.dry_run}</code>',
            '',
            '点击下方按钮生成计划、编辑条目并执行。',
        ]
        if session.plan:
            page_size = 8
            total_pages = max(1, (len(session.plan.items) + page_size - 1) // page_size)
            lines.extend([
                '',
                f'当前计划: <code>{len(session.plan.items)}</code> 个视频 / <code>{session.plan.total_sub_tracks}</code> 条字幕轨道',
                f'编辑页: <code>{session.plan_page + 1}</code>/<code>{total_pages}</code>',
            ])
        return '\n'.join(lines)

    def _format_mux_summary(self, summary: MuxRunSummary) -> str:
        prefix = '✅' if summary.failed == 0 else '⚠️'
        lines = [
            f'{prefix} <b>/ass 字幕内封执行完成</b>',
            '',
            f'目录: <code>{html.escape(summary.target_dir)}</code>',
            f'临时目录: <code>{html.escape(summary.tmp_dir)}</code>',
            f'计划文件: <code>{html.escape(summary.plan_path)}</code>',
            f'总 MKV: <code>{summary.total_mkvs}</code>',
            f'计划视频: <code>{summary.matched_mkvs}</code>',
            f'字幕轨道: <code>{summary.total_sub_tracks}</code>',
            f'处理成功: <code>{summary.processed}</code>',
            f'失败: <code>{summary.failed}</code>',
            f'并发: <code>{summary.jobs}</code>',
            f'DRY-RUN: <code>{summary.dry_run}</code>',
            f'删除外挂字幕: <code>{summary.delete_external_subs}</code>',
            f'实际删除外挂字幕数: <code>{summary.deleted_external_subs_count}</code>',
            f'重复引用字幕文件: <code>{summary.duplicate_subtitle_refs}</code>',
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
