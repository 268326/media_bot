from __future__ import annotations

import asyncio
import html
import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from aiogram import Bot
from aiogram.types import Message, InlineKeyboardMarkup

from ass_formatter import (
    build_mux_add_sub_picker_keyboard,
    build_mux_item_keyboard,
    build_mux_menu_keyboard,
    build_mux_plan_keyboard,
    build_mux_preview_keyboard,
    build_mux_run_confirm_keyboard,
    format_default_group_updated,
    format_default_lang_updated,
    format_jobs_updated,
    format_mux_add_sub_picker,
    format_mux_error,
    format_mux_item_detail,
    format_mux_menu,
    format_mux_preview_list,
    format_mux_preview_summary,
    format_mux_run_confirm,
    format_mux_session,
    format_mux_summary,
    format_sub_file_updated,
    format_subset_error,
    format_subset_summary,
    format_track_group_updated,
    format_track_lang_updated,
    join_lines_for_tg,
)

from ass_config import load_ass_settings_from_env
from ass_mux_config import AssMuxSettings, load_ass_mux_settings_from_env
from ass_mux_pipeline import collect_mux_plan_stats, fmt_bytes, run_mux_plan, MuxProgressEvent
from ass_mux_planner import (
    MuxPlan,
    MuxPlanItem,
    SubtitleTrackPlan,
    build_manual_mux_plan,
    build_mux_plan,
    build_track_name,
    format_mux_plan_preview,
    infer_lang_raw_from_subtitle_name,
    parse_lang,
    recount_mux_plan,
    short_ep_display,
    short_title_from_mkv,
    write_mux_plan,
)
from ass_pipeline import run_ass_pipeline
from ass_utils import AssPipelineError

logger = logging.getLogger(__name__)

ASS_MENU_PREFIX = "ass_menu:"
ASS_MUX_PREFIX = "ass_mux:"
ASS_MUX_SESSION_TTL = 1800
TG_TEXT_SAFE_LIMIT = 3800


@dataclass(slots=True)
class AssMuxSession:
    chat_id: int
    owner_user_id: int
    settings: AssMuxSettings
    plan: MuxPlan | None = None
    mode: str = "auto"
    default_group: str = ""
    default_lang: str = "chs"
    delete_external_subs: bool = False
    dry_run: bool = False
    plan_page: int = 0
    preview_page: int = 0
    preview_mode: str = "summary"
    selected_item_index: int | None = None
    selected_sub_index: int | None = None
    awaiting_field: str | None = None
    awaiting_message_id: int | None = None
    preview_message_id: int | None = None
    inline_notice: str = ""
    add_sub_candidates: list[str] = field(default_factory=list)
    add_sub_selected_indexes: set[int] = field(default_factory=set)
    add_sub_page: int = 0
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
            text = format_subset_summary(summary)
            await self._notify(bot, trigger_chat_id, settings.notify_chat_id, text)
            return summary.failed == 0, text
        except Exception as exc:
            self.last_error = str(exc)
            logger.exception('❌ /ass 字体子集化执行失败')
            text = format_subset_error(str(exc))
            await self._notify(bot, trigger_chat_id, load_ass_settings_from_env().notify_chat_id, text)
            return False, text
        finally:
            self.running = False
            self.lock.release()

    async def build_mux_menu(self, message: Message) -> tuple[str, InlineKeyboardMarkup]:
        settings = load_ass_mux_settings_from_env()
        return format_mux_menu(settings), build_mux_menu_keyboard(ASS_MENU_PREFIX)

    async def start_mux_session(self, *, chat_id: int, owner_user_id: int, mode: str = "auto") -> AssMuxSession:
        self.cleanup_mux_sessions()
        settings = load_ass_mux_settings_from_env()
        session = AssMuxSession(
            chat_id=chat_id,
            owner_user_id=owner_user_id,
            settings=settings,
            mode=mode if mode in ("auto", "manual") else "auto",
            default_group=settings.default_group,
            default_lang=settings.default_lang,
            delete_external_subs=settings.delete_external_subs_default,
            preview_mode="summary",
            preview_page=0,
        )
        self.mux_sessions[self._session_key(chat_id, owner_user_id)] = session
        logger.info('🎬 创建 /ass 字幕内封会话: chat_id=%s user_id=%s mode=%s target=%s', chat_id, owner_user_id, session.mode, settings.target_dir)
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

    def bind_mux_message_ids(self, chat_id: int, owner_user_id: int, *, panel_message_id: int | None = None, preview_message_id: int | None = None) -> None:
        session = self.get_mux_session(chat_id, owner_user_id)
        if not session:
            return
        if panel_message_id is not None:
            session.awaiting_message_id = panel_message_id
        if preview_message_id is not None:
            session.preview_message_id = preview_message_id
        session.touch()

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
        return format_mux_session(session)

    async def build_mux_panel_text(self, chat_id: int, owner_user_id: int) -> str:
        session = self.get_mux_session(chat_id, owner_user_id)
        if not session:
            raise AssPipelineError('当前没有进行中的字幕内封会话，请重新发送 /ass')
        session.touch()
        lines = [format_mux_session(session)]

        prompt_text = self.get_mux_inline_prompt_text(chat_id, owner_user_id)
        if prompt_text:
            lines.extend(['', prompt_text])

        if session.inline_notice:
            lines.extend(['', session.inline_notice])

        if session.plan:
            lines.extend(['', await self.build_mux_preview_text(chat_id, owner_user_id)])

        return join_lines_for_tg(lines)


    async def build_mux_preview_text(self, chat_id: int, owner_user_id: int) -> str:
        session = self.get_mux_session(chat_id, owner_user_id)
        if not session:
            raise AssPipelineError('当前没有进行中的字幕内封会话，请重新发送 /ass')
        session.touch()
        if not session.plan:
            return '🎞️ <b>计划预览</b>\n\n尚未生成计划。'

        total_items = len(session.plan.items)
        if session.preview_mode == 'summary':
            return format_mux_preview_summary(session)

        current_items: list[dict[str, object]] = []
        for index, item in self._current_preview_page_items(session):
            current_items.append({
                'index': index,
                'item': item,
                'display_title': short_title_from_mkv(Path(item.mkv).name) or Path(item.mkv).stem,
                'display_ep': short_ep_display(Path(item.mkv).name),
            })
        page_size = 4
        total_pages = max(1, (total_items + page_size - 1) // page_size)
        start_no = session.preview_page * page_size + 1 if current_items else 0
        end_no = session.preview_page * page_size + len(current_items)
        return format_mux_preview_list(
            session,
            current_items,
            current_page=session.preview_page + 1,
            total_pages=total_pages,
            total_items=total_items,
            start_no=start_no,
            end_no=end_no,
        )

    def get_mux_inline_prompt_text(self, chat_id: int, owner_user_id: int) -> str:
        session = self.get_mux_session(chat_id, owner_user_id)
        if not session or not session.awaiting_field:
            return ''

        field = session.awaiting_field
        if field == 'default_group':
            return '✏️ <b>当前等待输入：默认字幕组</b>\n• 直接发送文字 = 设置字幕组\n• 发送 <code>-</code> = 清空字幕组\n• 输入后请点“重新扫描”生效'
        if field == 'default_lang':
            return '🌐 <b>当前等待输入：默认语言</b>\n请输入例如：<code>chs</code> / <code>cht</code> / <code>eng</code> / <code>chs_eng</code>\n• 输入后请点“重新扫描”生效'
        if field == 'jobs':
            return '⚙️ <b>当前等待输入：并发数</b>\n请输入正整数，例如：<code>1</code> / <code>2</code> / <code>4</code>\n• 修改后立即用于本次会话执行'
        if field == 'add_sub_file':
            return '➕ <b>当前等待输入：要添加的字幕文件名</b>\n请输入同目录字幕文件名（只输入文件名，不要带路径）'
        if field == 'sub_file':
            return '📄 <b>当前等待输入：字幕文件名</b>\n请输入新的字幕文件名（只输入文件名，不要带路径）'
        if field == 'track_group':
            return '🏷️ <b>当前等待输入：字幕组</b>\n• 直接发送文字 = 设置字幕组\n• 发送 <code>-</code> = 清空字幕组'
        if field == 'track_lang':
            return '🌐 <b>当前等待输入：字幕语言</b>\n请输入例如：<code>chs</code> / <code>cht</code> / <code>eng</code> / <code>chs_eng</code>'
        return ''

    def set_mux_inline_notice(self, chat_id: int, owner_user_id: int, text: str) -> None:
        session = self.get_mux_session(chat_id, owner_user_id)
        if not session:
            return
        session.inline_notice = text
        session.touch()

    def clear_mux_inline_notice(self, chat_id: int, owner_user_id: int) -> None:
        session = self.get_mux_session(chat_id, owner_user_id)
        if not session:
            return
        session.inline_notice = ''
        session.touch()

    async def rebuild_mux_plan(self, chat_id: int, owner_user_id: int) -> tuple[AssMuxSession, str]:
        session = self.get_mux_session(chat_id, owner_user_id)
        if not session:
            raise AssPipelineError('当前没有进行中的字幕内封会话，请重新发送 /ass')
        session.touch()
        builder = build_manual_mux_plan if session.mode == 'manual' else build_mux_plan
        plan = await asyncio.to_thread(
            builder,
            session.settings,
            default_group=session.default_group,
            default_lang=session.default_lang,
        )
        session.plan = recount_mux_plan(plan)
        session.plan_page = 0
        session.preview_page = 0
        session.preview_mode = "summary"
        write_mux_plan(session.plan, session.settings.plan_path.expanduser().resolve())
        logger.info('🎬 /ass 生成字幕内封计划: chat_id=%s mode=%s items=%s tracks=%s plan=%s', chat_id, session.mode, len(session.plan.items), session.plan.total_sub_tracks, session.settings.plan_path)
        text = format_mux_plan_preview(session.plan)
        return session, text


    def _current_preview_page_items(self, session: AssMuxSession, *, page_size: int = 4) -> list[tuple[int, MuxPlanItem]]:
        if not session.plan:
            return []
        total_items = len(session.plan.items)
        total_pages = max(1, (total_items + page_size - 1) // page_size)
        session.preview_page = min(max(session.preview_page, 0), total_pages - 1)
        start = session.preview_page * page_size
        end = min(start + page_size, total_items)
        return [(index, session.plan.items[index]) for index in range(start, end)]

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
        current_items = self._current_plan_page_items(session) if session.plan else []
        total_pages = max(1, (len(session.plan.items) + 8 - 1) // 8) if session.plan else 1
        return build_mux_plan_keyboard(session, current_items, total_pages, ASS_MUX_PREFIX)

    def build_mux_preview_keyboard(self, chat_id: int, owner_user_id: int) -> InlineKeyboardMarkup:
        session = self.get_mux_session(chat_id, owner_user_id)
        if not session:
            raise AssPipelineError('当前没有进行中的字幕内封会话，请重新发送 /ass')
        total_pages = max(1, (len(session.plan.items) + 4 - 1) // 4) if session.plan else 1
        return build_mux_preview_keyboard(session, total_pages, ASS_MUX_PREFIX)

    def build_mux_item_keyboard(self, chat_id: int, owner_user_id: int, item_index: int) -> InlineKeyboardMarkup:
        session = self.get_mux_session(chat_id, owner_user_id)
        if not session or not session.plan:
            raise AssPipelineError('当前还没有生成计划')
        item = session.plan.items[item_index]
        return build_mux_item_keyboard(item, item_index, ASS_MUX_PREFIX, manual_mode=(session.mode == 'manual'), use_picker=(session.mode == 'manual'))

    def build_mux_add_sub_picker_keyboard(self, chat_id: int, owner_user_id: int, item_index: int) -> InlineKeyboardMarkup:
        session = self.get_mux_session(chat_id, owner_user_id)
        if not session or not session.plan:
            raise AssPipelineError('当前还没有生成计划')
        candidates = session.add_sub_candidates
        total_pages = max(1, (len(candidates) + 6 - 1) // 6)
        session.add_sub_page = min(max(session.add_sub_page, 0), total_pages - 1)
        return build_mux_add_sub_picker_keyboard(item_index, candidates, session.add_sub_selected_indexes, session.add_sub_page, total_pages, ASS_MUX_PREFIX)

    def format_mux_add_sub_picker(self, chat_id: int, owner_user_id: int, item_index: int) -> str:
        session = self.get_mux_session(chat_id, owner_user_id)
        if not session or not session.plan:
            raise AssPipelineError('当前还没有生成计划')
        item = session.plan.items[item_index]
        candidates = session.add_sub_candidates
        total_pages = max(1, (len(candidates) + 6 - 1) // 6)
        session.add_sub_page = min(max(session.add_sub_page, 0), total_pages - 1)
        return format_mux_add_sub_picker(item, item_index, candidates, session.add_sub_selected_indexes, session.add_sub_page, total_pages)

    def build_mux_run_confirm_keyboard(self) -> InlineKeyboardMarkup:
        return build_mux_run_confirm_keyboard(ASS_MUX_PREFIX)

    def format_mux_run_confirm(self, chat_id: int, owner_user_id: int) -> str:
        session = self.get_mux_session(chat_id, owner_user_id)
        if not session or not session.plan:
            raise AssPipelineError('当前还没有生成计划')
        stats = collect_mux_plan_stats(session.settings, session.plan)
        return format_mux_run_confirm(session, stats, fmt_bytes)

    def format_mux_item_detail(self, chat_id: int, owner_user_id: int, item_index: int) -> str:
        session = self.get_mux_session(chat_id, owner_user_id)
        if not session or not session.plan:
            raise AssPipelineError('当前还没有生成计划')
        item = session.plan.items[item_index]
        return format_mux_item_detail(item, item_index)

    def list_mux_candidate_subs(self, chat_id: int, owner_user_id: int, item_index: int) -> list[str]:
        session = self.get_mux_session(chat_id, owner_user_id)
        if not session or not session.plan:
            raise AssPipelineError('当前还没有生成计划')
        target_root = session.settings.target_dir.expanduser().resolve()
        item = session.plan.items[item_index]
        if session.mode == 'manual':
            candidates = [
                str(path.relative_to(target_root))
                for path in sorted(target_root.rglob('*'))
                if path.is_file() and path.suffix.lower() in ('.ass', '.sup')
            ]
            return candidates
        item_dir = (target_root / Path(item.mkv).parent).resolve()
        if not item_dir.is_dir():
            return []
        candidates: list[str] = []
        for path in sorted(item_dir.iterdir()):
            if path.is_file() and path.suffix.lower() in ('.ass', '.sup'):
                candidates.append(path.name)
        return candidates

    def list_mux_available_subs_for_item(self, chat_id: int, owner_user_id: int, item_index: int) -> list[str]:
        session = self.get_mux_session(chat_id, owner_user_id)
        if not session or not session.plan:
            raise AssPipelineError('当前还没有生成计划')
        item = session.plan.items[item_index]
        current_files = {str(Path(sub.file)) for sub in item.subs}
        return [name for name in self.list_mux_candidate_subs(chat_id, owner_user_id, item_index) if str(Path(name)) not in current_files]

    def prepare_mux_add_sub_picker(self, chat_id: int, owner_user_id: int, item_index: int) -> list[str]:
        session = self.get_mux_session(chat_id, owner_user_id)
        if not session:
            raise AssPipelineError('当前没有进行中的字幕内封会话，请重新发送 /ass')
        candidates = self.list_mux_available_subs_for_item(chat_id, owner_user_id, item_index)
        session.selected_item_index = item_index
        session.selected_sub_index = None
        session.awaiting_field = 'add_sub_pick'
        session.add_sub_candidates = candidates
        session.add_sub_selected_indexes = set()
        session.add_sub_page = 0
        session.touch()
        return candidates

    def set_mux_add_sub_picker_page(self, chat_id: int, owner_user_id: int, page: int) -> None:
        session = self.get_mux_session(chat_id, owner_user_id)
        if not session:
            raise AssPipelineError('当前没有进行中的字幕内封会话，请重新发送 /ass')
        session.add_sub_page = max(0, page)
        session.touch()

    def toggle_mux_add_sub_candidate(self, chat_id: int, owner_user_id: int, candidate_index: int) -> None:
        session = self.get_mux_session(chat_id, owner_user_id)
        if not session:
            raise AssPipelineError('当前没有进行中的字幕内封会话，请重新发送 /ass')
        if session.awaiting_field != 'add_sub_pick':
            raise AssPipelineError('当前不在按钮选字幕状态，请重新进入单集编辑')
        if candidate_index < 0 or candidate_index >= len(session.add_sub_candidates):
            raise AssPipelineError('选中的字幕候选不存在，请重新打开候选列表')
        if candidate_index in session.add_sub_selected_indexes:
            session.add_sub_selected_indexes.remove(candidate_index)
        else:
            session.add_sub_selected_indexes.add(candidate_index)
        session.touch()

    def confirm_mux_add_sub_candidates(self, chat_id: int, owner_user_id: int) -> str:
        session = self.get_mux_session(chat_id, owner_user_id)
        if not session:
            raise AssPipelineError('当前没有进行中的字幕内封会话，请重新发送 /ass')
        if session.awaiting_field != 'add_sub_pick':
            raise AssPipelineError('当前不在按钮选字幕状态，请重新进入单集编辑')
        if session.selected_item_index is None:
            raise AssPipelineError('当前未选中视频项，请重新进入单集编辑')
        if not session.add_sub_selected_indexes:
            raise AssPipelineError('请至少选择一个字幕文件')

        selected_indexes = sorted(session.add_sub_selected_indexes)
        added_names: list[str] = []
        for candidate_index in selected_indexes:
            try:
                candidate = session.add_sub_candidates[candidate_index]
            except IndexError as exc:
                raise AssPipelineError('选中的字幕候选不存在，请重新打开候选列表') from exc
            self.add_mux_subtitle_to_item(chat_id, owner_user_id, session.selected_item_index, candidate)
            added_names.append(candidate)

        self.clear_mux_prompt(chat_id, owner_user_id)
        names_preview = '、'.join(added_names[:3])
        if len(added_names) > 3:
            names_preview += f' 等 {len(added_names)} 个'
        return f'✅ <b>已批量添加字幕</b> <code>{html.escape(names_preview)}</code>'

    def pick_mux_add_sub_candidate(self, chat_id: int, owner_user_id: int, candidate_index: int) -> str:
        session = self.get_mux_session(chat_id, owner_user_id)
        if not session:
            raise AssPipelineError('当前没有进行中的字幕内封会话，请重新发送 /ass')
        if session.awaiting_field != 'add_sub_pick':
            raise AssPipelineError('当前不在按钮选字幕状态，请重新进入单集编辑')
        if session.selected_item_index is None:
            raise AssPipelineError('当前没有选中要添加字幕的视频项')
        try:
            candidate = session.add_sub_candidates[candidate_index]
        except IndexError as exc:
            raise AssPipelineError('选中的字幕候选不存在，请重新打开候选列表') from exc
        result = self.add_mux_subtitle_to_item(chat_id, owner_user_id, session.selected_item_index, candidate)
        self.clear_mux_prompt(chat_id, owner_user_id)
        return result

    def add_mux_subtitle_to_item(self, chat_id: int, owner_user_id: int, item_index: int, filename: str) -> str:
        session = self.get_mux_session(chat_id, owner_user_id)
        if not session or not session.plan:
            raise AssPipelineError('当前还没有生成计划')
        item = session.plan.items[item_index]
        raw_name = filename.strip()
        candidate_rel: Path | None = None
        target_root = session.settings.target_dir.expanduser().resolve()

        if session.mode == 'manual':
            try:
                candidate_rel = Path(raw_name)
                full_path = (target_root / candidate_rel).resolve()
                full_path.relative_to(target_root)
            except ValueError as exc:
                raise AssPipelineError('字幕文件超出工作目录范围') from exc
        else:
            candidate_name = Path(raw_name).name
            if not candidate_name:
                raise AssPipelineError('字幕文件名不能为空')
            if candidate_name != raw_name:
                raise AssPipelineError('请只输入同目录字幕文件名，不要带路径')
            candidate_rel = Path(item.mkv).parent / candidate_name
            full_path = (target_root / candidate_rel).resolve()
            try:
                full_path.relative_to(target_root)
            except ValueError as exc:
                raise AssPipelineError('字幕文件超出工作目录范围') from exc

        if not full_path.is_file():
            raise AssPipelineError(f'字幕文件不存在: {candidate_rel}')
        if full_path.suffix.lower() not in ('.ass', '.sup'):
            raise AssPipelineError('只支持 .ass 或 .sup 字幕文件')
        if any(str(Path(sub.file)) == str(candidate_rel) for sub in item.subs):
            raise AssPipelineError('该字幕文件已在当前视频计划中')

        detected_lang_raw = infer_lang_raw_from_subtitle_name(full_path.name, session.default_lang)
        detected_mkv_lang, detected_track_name = build_track_name(session.default_group, detected_lang_raw)
        item.subs.append(
            SubtitleTrackPlan(
                file=str(candidate_rel),
                group=session.default_group,
                lang_raw=detected_lang_raw,
                mkv_lang=detected_mkv_lang,
                track_name=detected_track_name,
            )
        )
        recount_mux_plan(session.plan)
        session.touch()
        return f'✅ <b>已添加字幕</b> <code>{html.escape(str(candidate_rel))}</code>'

    def remove_mux_subtitle_from_item(self, chat_id: int, owner_user_id: int, item_index: int, sub_index: int) -> str:
        session = self.get_mux_session(chat_id, owner_user_id)
        if not session or not session.plan:
            raise AssPipelineError('当前还没有生成计划')
        item = session.plan.items[item_index]
        try:
            removed = item.subs.pop(sub_index)
        except IndexError as exc:
            raise AssPipelineError('选中的字幕项不存在，请重新进入该单集') from exc
        recount_mux_plan(session.plan)
        session.touch()
        return f'🗑️ <b>已移除字幕</b> <code>{Path(removed.file).name}</code>'

    def set_mux_prompt(self, chat_id: int, owner_user_id: int, *, field: str, item_index: int | None = None, sub_index: int | None = None, message_id: int | None = None) -> None:
        session = self.get_mux_session(chat_id, owner_user_id)
        if not session:
            raise AssPipelineError('当前没有进行中的字幕内封会话，请重新发送 /ass')
        session.selected_item_index = item_index
        session.selected_sub_index = sub_index
        session.awaiting_field = field
        session.awaiting_message_id = message_id
        if field != 'add_sub_pick':
            session.add_sub_candidates = []
            session.add_sub_selected_indexes = set()
            session.add_sub_page = 0
        session.touch()

    def clear_mux_prompt(self, chat_id: int, owner_user_id: int) -> None:
        session = self.get_mux_session(chat_id, owner_user_id)
        if not session:
            return
        session.selected_item_index = None
        session.selected_sub_index = None
        session.awaiting_field = None
        session.awaiting_message_id = session.awaiting_message_id
        session.add_sub_candidates = []
        session.add_sub_selected_indexes = set()
        session.add_sub_page = 0
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
            return format_default_group_updated(session.default_group)

        if field == 'default_lang':
            if not raw:
                raise AssPipelineError('默认语言不能为空，例如 chs / cht / eng / chs_eng')
            self._parse_lang_or_raise(raw)
            session.default_lang = raw
            self.clear_mux_prompt(chat_id, owner_user_id)
            return format_default_lang_updated(session.default_lang)

        if field == 'jobs':
            if not raw:
                raise AssPipelineError('并发数不能为空，例如 1 / 2 / 4')
            try:
                jobs = int(raw)
            except ValueError as exc:
                raise AssPipelineError('并发数必须是正整数，例如 1 / 2 / 4') from exc
            if jobs < 1:
                raise AssPipelineError('并发数最小为 1')
            session.settings.jobs = jobs
            self.clear_mux_prompt(chat_id, owner_user_id)
            session.touch()
            return format_jobs_updated(session.settings.jobs)

        if field == 'add_sub_file':
            if session.selected_item_index is None:
                raise AssPipelineError('当前没有选中要添加字幕的视频项')
            result = self.add_mux_subtitle_to_item(chat_id, owner_user_id, session.selected_item_index, raw)
            self.clear_mux_prompt(chat_id, owner_user_id)
            return result

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
            return format_sub_file_updated(sub.file)

        if field == 'track_group':
            sub.group = '' if raw == '-' else raw
            mkv_lang, lang_cn = self._parse_lang_or_raise(sub.lang_raw)
            sub.mkv_lang = mkv_lang
            sub.track_name = f'{sub.group} | {lang_cn}' if sub.group else lang_cn
            self.clear_mux_prompt(chat_id, owner_user_id)
            session.touch()
            return format_track_group_updated(sub.track_name, sub.mkv_lang)

        if field == 'track_lang':
            if not raw:
                raise AssPipelineError('语言不能为空，例如 chs / cht / eng / chs_eng')
            mkv_lang, lang_cn = self._parse_lang_or_raise(raw)
            sub.lang_raw = raw
            sub.mkv_lang = mkv_lang
            sub.track_name = f'{sub.group} | {lang_cn}' if sub.group else lang_cn
            self.clear_mux_prompt(chat_id, owner_user_id)
            session.touch()
            return format_track_lang_updated(sub.track_name, sub.mkv_lang)

        raise AssPipelineError('未知输入字段，请重新发送 /ass')

    async def run_mux(
        self,
        bot: Bot,
        chat_id: int,
        owner_user_id: int,
        *,
        progress_callback: Callable[[MuxProgressEvent], None] | None = None,
    ) -> tuple[bool, str]:
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
                progress_callback=progress_callback,
            )
            text = format_mux_summary(summary)
            await self._notify(bot, chat_id, session.settings.notify_chat_id, text)
            self.clear_mux_session(chat_id, owner_user_id)
            return summary.failed == 0, text
        except Exception as exc:
            self.last_error = str(exc)
            logger.exception('❌ /ass 字幕内封执行失败')
            text = format_mux_error(str(exc))
            await self._notify(bot, chat_id, load_ass_mux_settings_from_env().notify_chat_id, text)
            return False, text
        finally:
            self.running = False
            self.lock.release()

    async def _notify(self, bot: Bot, trigger_chat_id: int, notify_chat_id: str, text: str) -> None:
        target = str(notify_chat_id or '').strip()
        if not target:
            logger.info('ℹ️ 跳过 /ass 汇总通知：未配置 notify_chat_id')
            return
        if target == str(trigger_chat_id):
            logger.info('ℹ️ 跳过 /ass 汇总通知：notify_chat_id 与触发 chat 相同 (%s)', target)
            return
        try:
            await bot.send_message(chat_id=int(target), text=text, parse_mode='HTML')
            logger.info('✅ /ass 汇总通知已发送: chat_id=%s', target)
        except Exception:
            logger.exception('⚠️ 发送 /ass 汇总通知失败: chat_id=%s', target)



ass_service = AssService()
