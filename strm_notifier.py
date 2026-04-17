"""
STRM 通知聚合与 Telegram 推送
"""
from __future__ import annotations

import asyncio
import html
import logging
import threading
from dataclasses import dataclass, field
from pathlib import Path

from aiogram import Bot

from config import TGBOT_NOTIFY_CHAT_ID

FLUSH_INTERVAL_S = 12
BULK_TRIGGER_COUNT = 6
DETAIL_ALL_THRESHOLD = 8
DETAIL_PREVIEW_LIMIT = 10
OVERVIEW_LIMIT = 6
MAX_CAPTURED_DETAILS = 60
SECTION_DIVIDER = "━━━━━━━━━━━━━━━━━━━━━━"
PATH_PREVIEW_LEN = 96


@dataclass
class FolderPendingReport:
    folder_key: str
    renamed_count: int = 0
    subtitle_count: int = 0
    already_ok_count: int = 0
    failed_count: int = 0
    rename_items: list[tuple[str, str]] = field(default_factory=list)
    fail_items: list[tuple[str, str]] = field(default_factory=list)


@dataclass
class FolderFinalizedEvent:
    ok: bool
    folder_key: str
    src: str
    dst: str
    renamed_count: int
    subtitle_count: int
    already_ok_count: int
    failed_count: int
    rename_items: list[tuple[str, str]]
    fail_items: list[tuple[str, str]]
    reason: str = ""


@dataclass
class RootPendingEvent:
    source_name: str
    current_path: str
    renamed: bool = False
    already_ok: bool = False
    target_name: str = ""
    subtitle_count: int = 0
    reason: str = ""


@dataclass
class RootCompletedEvent:
    ok: bool
    src: str
    dst: str
    source_name: str
    target_name: str
    renamed: bool
    already_ok: bool
    subtitle_count: int
    reason: str = ""


class StrmNotifier:
    def __init__(self):
        self.chat_id = str(TGBOT_NOTIFY_CHAT_ID or "").strip()
        self.bot: Bot | None = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self.flush_task: asyncio.Task | None = None
        self.flush_event: asyncio.Event | None = None
        self.lock = threading.Lock()
        self.started = False
        self.folder_reports: dict[str, FolderPendingReport] = {}
        self.root_pending: dict[str, RootPendingEvent] = {}
        self.folder_events: list[FolderFinalizedEvent] = []
        self.root_completed: list[RootCompletedEvent] = []

    async def start(self, bot: Bot):
        if not self.chat_id:
            logging.info("ℹ️ STRM 通知未启用（TGBOT_NOTIFY_CHAT_ID 为空）")
            return
        if self.started:
            return

        self.bot = bot
        self.loop = asyncio.get_running_loop()
        self.flush_event = asyncio.Event()
        self.flush_task = asyncio.create_task(self._flush_loop())
        self.started = True
        logging.info("✅ STRM 通知已启用: chat_id=%s", self.chat_id)

    async def stop(self):
        if not self.started:
            return
        await self._flush_once()
        if self.flush_task:
            self.flush_task.cancel()
            try:
                await self.flush_task
            except asyncio.CancelledError:
                pass
        self.flush_task = None
        self.flush_event = None
        self.bot = None
        self.loop = None
        self.started = False
        logging.info("🛑 STRM 通知已停止")

    def _capture_detail(self, items: list[tuple[str, str]], left: str, right: str):
        if len(items) < MAX_CAPTURED_DETAILS:
            items.append((left, right))

    def _trigger_flush(self, urgent: bool = False):
        if not self.loop or not self.flush_event:
            return
        if urgent:
            self.loop.call_soon_threadsafe(self.flush_event.set)
            return
        with self.lock:
            total = len(self.folder_events) + len(self.root_completed)
        if total >= BULK_TRIGGER_COUNT:
            self.loop.call_soon_threadsafe(self.flush_event.set)

    def record_process_result(
        self,
        folder_key: str | None,
        current_path: Path,
        *,
        source_name: str,
        target_name: str,
        ok: bool,
        renamed: bool,
        already_ok: bool,
        subtitle_count: int,
        reason: str,
    ):
        with self.lock:
            if folder_key is not None:
                report = self.folder_reports.get(folder_key)
                if not report:
                    report = FolderPendingReport(folder_key=folder_key)
                    self.folder_reports[folder_key] = report

                if ok:
                    if already_ok:
                        report.already_ok_count += 1
                    else:
                        report.renamed_count += 1
                        report.subtitle_count += subtitle_count
                        self._capture_detail(report.rename_items, source_name, target_name)
                else:
                    report.failed_count += 1
                    self._capture_detail(report.fail_items, source_name, reason or "unknown")
            else:
                self.root_pending[str(current_path)] = RootPendingEvent(
                    source_name=source_name,
                    current_path=str(current_path),
                    renamed=renamed,
                    already_ok=already_ok,
                    target_name=target_name or source_name,
                    subtitle_count=subtitle_count,
                    reason=reason or "",
                )

    def record_folder_completed(self, folder_key: str, src: Path, dst: Path):
        with self.lock:
            report = self.folder_reports.pop(folder_key, None) or FolderPendingReport(folder_key=folder_key)
            self.folder_events.append(
                FolderFinalizedEvent(
                    ok=True,
                    folder_key=folder_key,
                    src=str(src),
                    dst=str(dst),
                    renamed_count=report.renamed_count,
                    subtitle_count=report.subtitle_count,
                    already_ok_count=report.already_ok_count,
                    failed_count=report.failed_count,
                    rename_items=report.rename_items[:],
                    fail_items=report.fail_items[:],
                )
            )
        self._trigger_flush()

    def record_folder_failed(self, folder_key: str, src: Path, dst: Path, *, reason: str = ""):
        with self.lock:
            report = self.folder_reports.get(folder_key) or FolderPendingReport(folder_key=folder_key)
            self.folder_events.append(
                FolderFinalizedEvent(
                    ok=False,
                    folder_key=folder_key,
                    src=str(src),
                    dst=str(dst),
                    renamed_count=report.renamed_count,
                    subtitle_count=report.subtitle_count,
                    already_ok_count=report.already_ok_count,
                    failed_count=report.failed_count,
                    rename_items=report.rename_items[:],
                    fail_items=report.fail_items[:],
                    reason=reason or "unknown",
                )
            )
        self._trigger_flush(urgent=True)

    def record_root_completed(self, src: Path, dst: Path, *, ok: bool, subtitle_count: int = 0, reason: str = ""):
        with self.lock:
            pending = self.root_pending.pop(str(src), None)
            if pending:
                source_name = pending.source_name
                target_name = pending.target_name
                renamed = pending.renamed
                already_ok = pending.already_ok
                subtitle_total = max(subtitle_count, pending.subtitle_count)
                root_reason = reason or pending.reason
            else:
                source_name = src.name
                target_name = dst.name
                renamed = src.name != dst.name
                already_ok = False
                subtitle_total = subtitle_count
                root_reason = reason

            self.root_completed.append(
                RootCompletedEvent(
                    ok=ok,
                    src=str(src),
                    dst=str(dst),
                    source_name=source_name,
                    target_name=target_name,
                    renamed=renamed,
                    already_ok=already_ok,
                    subtitle_count=subtitle_total,
                    reason=root_reason or "",
                )
            )
        self._trigger_flush(urgent=not ok)

    async def _flush_loop(self):
        while True:
            try:
                try:
                    assert self.flush_event is not None
                    await asyncio.wait_for(self.flush_event.wait(), timeout=FLUSH_INTERVAL_S)
                    self.flush_event.clear()
                except asyncio.TimeoutError:
                    pass
                await self._flush_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logging.exception("❌ STRM 通知循环异常: %s", exc)

    async def _flush_once(self):
        if not self.bot or not self.chat_id:
            return

        with self.lock:
            folder_events = self.folder_events[:]
            root_events = self.root_completed[:]
            self.folder_events.clear()
            self.root_completed.clear()

        messages: list[str] = []
        if folder_events:
            messages.extend(self._format_folder_messages(folder_events))
        if root_events:
            messages.extend(self._format_root_messages(root_events))

        for text in messages:
            try:
                await self.bot.send_message(self.chat_id, text, parse_mode="HTML")
            except Exception as exc:
                logging.error("❌ STRM 通知发送失败: %s", exc)

    def _build_message(self, title: str, sections: list[str]) -> str:
        parts = [title, SECTION_DIVIDER]
        parts.extend(section for section in sections if section)
        return "\n\n".join(parts)

    def _blockquote(self, lines: list[str]) -> str:
        body = "\n".join(line for line in lines if line)
        if not body:
            return ""
        return f"<blockquote>\n{body}\n</blockquote>"

    def _short_path(self, path: str, max_len: int = PATH_PREVIEW_LEN) -> str:
        text = str(path or "")
        if len(text) <= max_len:
            return text
        head = max_len // 2 - 1
        tail = max_len - head - 1
        return f"{text[:head]}…{text[-tail:]}"

    def _detail_limit(self, total: int) -> int:
        return total if total <= DETAIL_ALL_THRESHOLD else DETAIL_PREVIEW_LIMIT

    def _format_pair_section(
        self,
        *,
        title_icon: str,
        title_text: str,
        items: list[tuple[str, str]],
        total: int,
        right_as_code: bool,
    ) -> str:
        if total <= 0 or not items:
            return ""

        limit = min(len(items), self._detail_limit(total))
        detail_label = "明细" if total <= DETAIL_ALL_THRESHOLD else "示例"
        lines: list[str] = []
        for idx, (left, right) in enumerate(items[:limit], 1):
            lines.append(f"{idx}. <code>{html.escape(left)}</code>")
            if right_as_code:
                lines.append(f"   → <code>{html.escape(right)}</code>")
            else:
                lines.append(f"   → {html.escape(right)}")

        hidden = max(total - limit, 0)
        if hidden > 0:
            lines.append(f"… 其余 <b>{hidden}</b> 项未展开")

        heading = f"{title_icon} <b>{title_text}{detail_label}</b>"
        return f"{heading}\n{self._blockquote(lines)}"

    def _format_folder_overview(self, events: list[FolderFinalizedEvent]) -> str:
        limit = len(events) if len(events) <= DETAIL_ALL_THRESHOLD else min(len(events), OVERVIEW_LIMIT)
        lines: list[str] = []
        for idx, event in enumerate(events[:limit], 1):
            status_icon = "✅" if event.ok else "❌"
            lines.append(f"{idx}. {status_icon} <code>{html.escape(event.folder_key)}</code>")
            lines.append(f"   📂 <code>{html.escape(self._short_path(event.dst))}</code>")
            lines.append(
                f"   📊 🎬<b>{event.renamed_count}</b> / 📎<b>{event.subtitle_count}</b> / ⏭️<b>{event.already_ok_count}</b> / ❌<b>{event.failed_count}</b>"
            )
            if not event.ok and event.reason:
                lines.append(f"   原因: {html.escape(event.reason)}")

        hidden = len(events) - limit
        if hidden > 0:
            lines.append(f"… 另有 <b>{hidden}</b> 个目录未展开")

        return f"🗂 <b>目录概览</b>\n{self._blockquote(lines)}"

    def _format_root_detail_section(self, heading: str, events: list[RootCompletedEvent], *, limit: int) -> str:
        if not events or limit <= 0:
            return ""

        lines: list[str] = []
        for idx, event in enumerate(events[:limit], 1):
            status_icon = "✅" if event.ok else "❌"
            lines.append(f"{idx}. {status_icon} <code>{html.escape(event.source_name)}</code>")

            if event.renamed and event.target_name and event.target_name != event.source_name:
                lines.append(f"   → <code>{html.escape(event.target_name)}</code>")
            elif event.already_ok:
                lines.append("   → <b>命名已就绪</b>")
            elif event.target_name and event.target_name != event.source_name:
                lines.append(f"   → <code>{html.escape(event.target_name)}</code>")

            if event.subtitle_count:
                lines.append(f"   📎 字幕联动: <b>{event.subtitle_count}</b>")

            path_label = "归档路径" if event.ok else "当前路径"
            lines.append(f"   📂 {path_label}: <code>{html.escape(self._short_path(event.dst))}</code>")

            if not event.ok:
                lines.append(f"   原因: {html.escape(event.reason or 'unknown')}")

        hidden = len(events) - limit
        if hidden > 0:
            lines.append(f"… 其余 <b>{hidden}</b> 项未展开")

        return f"{heading}\n{self._blockquote(lines)}"

    def _format_folder_messages(self, events: list[FolderFinalizedEvent]) -> list[str]:
        if len(events) == 1:
            event = events[0]
            title = "📦 <b>STRM 批次已归档</b>" if event.ok else "❌ <b>STRM 批次归档失败</b>"
            summary_lines = [
                f"批次目录: <code>{html.escape(event.folder_key)}</code>",
                f"源目录: <code>{html.escape(event.src)}</code>",
                f"目标目录: <code>{html.escape(event.dst)}</code>",
            ]
            if not event.ok and event.reason:
                summary_lines.append(f"失败原因: {html.escape(event.reason)}")

            stats_lines = [
                f"🎬 重命名: <b>{event.renamed_count}</b>",
                f"📎 字幕联动: <b>{event.subtitle_count}</b>",
                f"⏭️ 已就绪: <b>{event.already_ok_count}</b>",
                f"❌ 失败转移: <b>{event.failed_count}</b>",
            ]

            sections = [
                self._blockquote(summary_lines),
                f"📊 <b>处理统计</b>\n{self._blockquote(stats_lines)}",
                self._format_pair_section(
                    title_icon="📝",
                    title_text="重命名",
                    items=event.rename_items,
                    total=event.renamed_count,
                    right_as_code=True,
                ),
                self._format_pair_section(
                    title_icon="❌",
                    title_text="失败",
                    items=event.fail_items,
                    total=event.failed_count,
                    right_as_code=False,
                ),
            ]
            return [self._build_message(title, sections)]

        success_dirs = sum(1 for event in events if event.ok)
        failed_dirs = len(events) - success_dirs
        total_renamed = sum(event.renamed_count for event in events)
        total_subtitles = sum(event.subtitle_count for event in events)
        total_already_ok = sum(event.already_ok_count for event in events)
        total_failed = sum(event.failed_count for event in events)

        title = "📦 <b>STRM 批次处理汇总</b>" if failed_dirs else "📦 <b>STRM 批次归档汇总</b>"
        sections = [
            self._blockquote(
                [
                    f"✅ 归档成功目录: <b>{success_dirs}</b>",
                    f"❌ 归档失败目录: <b>{failed_dirs}</b>",
                    f"🎬 重命名: <b>{total_renamed}</b>",
                    f"📎 字幕联动: <b>{total_subtitles}</b>",
                    f"⏭️ 已就绪: <b>{total_already_ok}</b>",
                    f"❌ 失败转移: <b>{total_failed}</b>",
                ]
            ),
            self._format_folder_overview(events),
        ]
        return [self._build_message(title, sections)]

    def _format_root_messages(self, events: list[RootCompletedEvent]) -> list[str]:
        if len(events) == 1:
            event = events[0]
            title = "✅ <b>STRM 单文件已归档</b>" if event.ok else "❌ <b>STRM 单文件处理失败</b>"
            info_lines = [
                f"源文件: <code>{html.escape(event.source_name)}</code>",
                f"结果文件: <code>{html.escape(event.target_name)}</code>",
                f"{'归档路径' if event.ok else '当前路径'}: <code>{html.escape(event.dst)}</code>",
            ]

            status_lines = [f"📎 字幕联动: <b>{event.subtitle_count}</b>"]
            if event.renamed:
                status_lines.append("📝 已执行重命名")
            elif event.already_ok:
                status_lines.append("⏭️ 原文件已符合命名规则")

            sections = [
                self._blockquote(info_lines),
                self._blockquote(status_lines),
            ]
            if event.reason:
                sections.append(self._blockquote([f"失败原因: {html.escape(event.reason)}"]))
            return [self._build_message(title, sections)]

        ok_events = [event for event in events if event.ok]
        fail_events = [event for event in events if not event.ok]
        subtitle_total = sum(event.subtitle_count for event in events)

        title = "📨 <b>STRM 单文件处理汇总</b>" if fail_events else "📨 <b>STRM 单文件归档汇总</b>"
        sections = [
            self._blockquote(
                [
                    f"✅ 成功归档: <b>{len(ok_events)}</b>",
                    f"❌ 失败转移: <b>{len(fail_events)}</b>",
                    f"📎 字幕联动: <b>{subtitle_total}</b>",
                ]
            )
        ]

        if len(events) <= DETAIL_ALL_THRESHOLD:
            sections.append(self._format_root_detail_section("📄 <b>全部明细</b>", events, limit=len(events)))
        else:
            if ok_events:
                ok_limit = min(len(ok_events), OVERVIEW_LIMIT)
                sections.append(self._format_root_detail_section("✅ <b>成功示例</b>", ok_events, limit=ok_limit))
            if fail_events:
                fail_limit = min(len(fail_events), self._detail_limit(len(fail_events)))
                fail_heading = "❌ <b>失败明细</b>" if len(fail_events) <= DETAIL_ALL_THRESHOLD else "❌ <b>失败示例</b>"
                sections.append(self._format_root_detail_section(fail_heading, fail_events, limit=fail_limit))

        return [self._build_message(title, sections)]


strm_notifier = StrmNotifier()
