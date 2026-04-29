from __future__ import annotations

import html
from pathlib import Path
from typing import Any

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

TG_TEXT_SAFE_LIMIT = 3800


def join_lines_for_tg(
    lines: list[str],
    *,
    limit: int = TG_TEXT_SAFE_LIMIT,
    truncation_note: str = '… <i>内容过长，已自动截断。</i>',
) -> str:
    result: list[str] = []
    total = 0
    for line in lines:
        extra = len(line) + (1 if result else 0)
        if total + extra > limit:
            break
        result.append(line)
        total += extra

    if len(result) == len(lines):
        return '\n'.join(result)

    note_extra = len(truncation_note) + (1 if result else 0)
    while result and total + note_extra > limit:
        removed = result.pop()
        total -= len(removed) + (1 if result else 0)
        note_extra = len(truncation_note) + (1 if result else 0)
    if not result and len(truncation_note) > limit:
        return truncation_note[:limit]
    result.append(truncation_note)
    return '\n'.join(result)


def format_mux_menu(settings: Any) -> str:
    lines = [
        '🎬 <b>/ass 功能菜单</b>',
        '─────────────────',
        '',
        '<blockquote>',
        '🔤 <b>子集化字体</b>',
        '扫描 ASS / 字体 / 压缩包，生成 <code>.assfonts.ass</code>',
        '</blockquote>',
        '',
        '<blockquote>',
        '🎞️ <b>内封字幕</b>',
        '把同目录匹配到的 <code>.ass/.sup</code> 内封进 <code>.mkv</code>',
        '</blockquote>',
        '',
        f'📂 目录: <code>{html.escape(str(settings.target_dir))}</code>',
        f'🌐 默认语言: <code>{html.escape(settings.default_lang)}</code>',
        f'⚙️ 并发: <code>{settings.jobs}</code>',
    ]
    return '\n'.join(lines)


def build_mux_menu_keyboard(menu_prefix: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text='🔤 子集化字体', callback_data=f'{menu_prefix}subset'),
            InlineKeyboardButton(text='🎞️ 内封字幕', callback_data=f'{menu_prefix}mux_start'),
        ]
    ])


def format_mux_session(session: Any) -> str:
    lines = [
        '🎛️ <b>/ass · 字幕内封控制面板</b>',
        '─────────────────',
        f'📂 目录: <code>{html.escape(str(session.settings.target_dir))}</code>',
        f'🔁 扫描: <code>{session.settings.recursive}</code> · ⚙️ 并发: <code>{session.settings.jobs}</code>',
        f'🌐 默认语言: <code>{html.escape(session.default_lang)}</code>',
        f'🏷️ 默认字幕组: <code>{html.escape(session.default_group or "-")}</code>',
        f'🗑️ 外挂字幕: <code>{session.delete_external_subs}</code> · 🧪 DRY: <code>{session.dry_run}</code>',
    ]
    if session.plan:
        page_size = 8
        total_pages = max(1, (len(session.plan.items) + page_size - 1) // page_size)
        preview_mode = '总览' if session.preview_mode == 'summary' else '列表'
        lines.extend([
            '',
            f'📊 计划: <code>{len(session.plan.items)}</code> 集 / <code>{session.plan.total_sub_tracks}</code> 条字幕轨',
            f'🧭 编辑页: <code>{session.plan_page + 1}</code>/<code>{total_pages}</code>',
            f'👀 预览: <code>{preview_mode}</code> · 同面板显示',
            '👆 轻触数字进入单集编辑。',
        ])
    else:
        lines.extend(['', '📭 还没有计划，先点“重新扫描”。'])
    return join_lines_for_tg(lines)


def format_mux_preview_summary(session: Any) -> str:
    total_items = session.plan.total_items if hasattr(session.plan, 'total_items') else len(session.plan.items)
    total_tracks = session.plan.total_sub_tracks
    lines = [
        '🎞️ <b>计划预览 · 总览</b>',
        '─────────────────',
        f'📂 目录: <code>{html.escape(str(session.settings.target_dir))}</code>',
        f'🎬 视频: <code>{total_items}</code> · 🎞️ 字幕轨: <code>{total_tracks}</code>',
        f'🌐 默认语言: <code>{html.escape(session.default_lang)}</code>',
        f'🏷️ 默认字幕组: <code>{html.escape(session.default_group or "-")}</code>',
        f'⚙️ 并发: <code>{session.settings.jobs}</code> · 🧪 DRY: <code>{session.dry_run}</code>',
        f'🗑️ 删除外挂字幕: <code>{session.delete_external_subs}</code>',
    ]
    return join_lines_for_tg(lines)


def format_mux_preview_list(
    session: Any,
    current_items: list[Any],
    *,
    current_page: int,
    total_pages: int,
    total_items: int,
    start_no: int,
    end_no: int,
) -> str:
    total_tracks = session.plan.total_sub_tracks
    lines = [
        '🎞️ <b>计划预览 · 列表</b>',
        '─────────────────',
        f'第 <code>{current_page}</code>/<code>{total_pages}</code> 页 · 当前 <code>{len(current_items)}</code> 条 · 总共 <code>{total_items}</code> 条',
        f'范围: <code>{start_no}</code>-<code>{end_no}</code> · 总字幕轨 <code>{total_tracks}</code>',
        '',
    ]
    for entry in current_items:
        if isinstance(entry, dict):
            index = int(entry.get('index', 0) or 0)
            item_obj = entry.get('item')
            title = entry.get('display_title')
            ep = entry.get('display_ep')
        else:
            index, item_obj = entry
            title = None
            ep = ''

        if item_obj is None:
            continue

        title = str(title or Path(item_obj.mkv).stem)
        ep = str(ep or '')
        lines.append(f'<blockquote>\n<b>{index + 1}. {html.escape(title)}</b> {html.escape(ep)}\n<code>{html.escape(Path(item_obj.mkv).name)}</code>')
        for sub_idx, sub in enumerate(item_obj.subs, 1):
            lines.append(
                f'#{sub_idx} <code>{html.escape(Path(sub.file).name)}</code>\n'
                f'<code>{html.escape(sub.track_name)}</code> · <code>{html.escape(sub.mkv_lang)}</code>'
            )
        lines.append('</blockquote>')
    if not current_items:
        lines.append('<blockquote>当前页没有条目</blockquote>')
    return join_lines_for_tg(
        lines,
        truncation_note='… <i>当前页列表过长，已自动截断；请翻页查看更多。</i>',
    )


def build_mux_plan_keyboard(session: Any, current_items: list[tuple[int, Any]], total_pages: int, mux_prefix: str) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if session.plan:
        button_row: list[InlineKeyboardButton] = []
        for index, _item in current_items:
            button_row.append(InlineKeyboardButton(text=f'{index + 1}', callback_data=f'{mux_prefix}edit_item:{index}'))
            if len(button_row) == 4:
                rows.append(button_row)
                button_row = []
        if button_row:
            rows.append(button_row)
        if total_pages > 1:
            nav_row: list[InlineKeyboardButton] = []
            if session.plan_page > 0:
                nav_row.append(InlineKeyboardButton(text='⬅️', callback_data=f'{mux_prefix}page:{session.plan_page - 1}'))
            nav_row.append(InlineKeyboardButton(text=f'编辑页 {session.plan_page + 1}/{total_pages}', callback_data=f'{mux_prefix}page:{session.plan_page}'))
            if session.plan_page < total_pages - 1:
                nav_row.append(InlineKeyboardButton(text='➡️', callback_data=f'{mux_prefix}page:{session.plan_page + 1}'))
            rows.append(nav_row)
    rows.append([
        InlineKeyboardButton(text='📋 预览总览', callback_data=f'{mux_prefix}preview:summary'),
        InlineKeyboardButton(text='📄 展开列表', callback_data=f'{mux_prefix}preview:list'),
    ])
    if session.preview_mode == 'list' and session.plan and max(1, (len(session.plan.items) + 4 - 1) // 4) > 1:
        preview_total_pages = max(1, (len(session.plan.items) + 4 - 1) // 4)
        nav_row: list[InlineKeyboardButton] = []
        if session.preview_page > 0:
            nav_row.append(InlineKeyboardButton(text='⬅️ 列表', callback_data=f'{mux_prefix}preview_page:{session.preview_page - 1}'))
        nav_row.append(InlineKeyboardButton(text=f'列表 {session.preview_page + 1}/{preview_total_pages}', callback_data=f'{mux_prefix}preview_page:{session.preview_page}'))
        if session.preview_page < preview_total_pages - 1:
            nav_row.append(InlineKeyboardButton(text='列表 ➡️', callback_data=f'{mux_prefix}preview_page:{session.preview_page + 1}'))
        rows.append(nav_row)
    rows.append([
        InlineKeyboardButton(text=f'🗑️ 外挂字幕: {"开" if session.delete_external_subs else "关"}', callback_data=f'{mux_prefix}toggle_delete'),
        InlineKeyboardButton(text=f'🧪 DRY: {"开" if session.dry_run else "关"}', callback_data=f'{mux_prefix}toggle_dry'),
    ])
    rows.append([
        InlineKeyboardButton(text='✏️ 默认字幕组', callback_data=f'{mux_prefix}prompt_group'),
        InlineKeyboardButton(text='🌐 默认语言', callback_data=f'{mux_prefix}prompt_lang'),
    ])
    rows.append([
        InlineKeyboardButton(text=f'⚙️ 并发数: {session.settings.jobs}', callback_data=f'{mux_prefix}prompt_jobs'),
    ])
    if session.awaiting_field:
        rows.append([
            InlineKeyboardButton(text='❎ 取消当前输入', callback_data=f'{mux_prefix}cancel_prompt'),
        ])
    action_row = [InlineKeyboardButton(text='🔄 重新扫描', callback_data=f'{mux_prefix}refresh')]
    if session.plan:
        action_row.append(InlineKeyboardButton(text='▶️ 开始执行', callback_data=f'{mux_prefix}run_confirm'))
    rows.append(action_row)
    rows.append([InlineKeyboardButton(text='❎ 结束会话', callback_data=f'{mux_prefix}cancel')])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_mux_preview_keyboard(session: Any, total_pages: int, mux_prefix: str) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = [[
        InlineKeyboardButton(text='📋 总览', callback_data=f'{mux_prefix}preview:summary'),
        InlineKeyboardButton(text='📄 列表', callback_data=f'{mux_prefix}preview:list'),
    ]]
    if session.awaiting_field:
        rows.append([
            InlineKeyboardButton(text='❎ 取消当前输入', callback_data=f'{mux_prefix}cancel_prompt'),
        ])
    if session.plan and session.preview_mode == 'list' and total_pages > 1:
        nav_row: list[InlineKeyboardButton] = []
        if session.preview_page > 0:
            nav_row.append(InlineKeyboardButton(text='⬅️', callback_data=f'{mux_prefix}preview_page:{session.preview_page - 1}'))
        nav_row.append(InlineKeyboardButton(text=f'列表 {session.preview_page + 1}/{total_pages}', callback_data=f'{mux_prefix}preview_page:{session.preview_page}'))
        if session.preview_page < total_pages - 1:
            nav_row.append(InlineKeyboardButton(text='➡️', callback_data=f'{mux_prefix}preview_page:{session.preview_page + 1}'))
        rows.append(nav_row)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_mux_item_keyboard(item: Any, item_index: int, mux_prefix: str) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for sub_index, _sub in enumerate(item.subs, 1):
        rows.append([
            InlineKeyboardButton(text=f'改字幕 {sub_index} 文件', callback_data=f'{mux_prefix}prompt_subfile:{item_index}:{sub_index - 1}'),
        ])
        rows.append([
            InlineKeyboardButton(text=f'改字幕 {sub_index} 字幕组', callback_data=f'{mux_prefix}prompt_subgroup:{item_index}:{sub_index - 1}'),
            InlineKeyboardButton(text=f'改字幕 {sub_index} 语言', callback_data=f'{mux_prefix}prompt_sublang:{item_index}:{sub_index - 1}'),
        ])
    rows.append([InlineKeyboardButton(text='⬅️ 返回计划列表', callback_data=f'{mux_prefix}back_plan')])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_mux_run_confirm_keyboard(mux_prefix: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text='▶️ 确认执行', callback_data=f'{mux_prefix}run_now'),
        InlineKeyboardButton(text='⬅️ 返回', callback_data=f'{mux_prefix}back_plan'),
    ]])


def format_mux_run_confirm(session: Any, stats: dict[str, Any], fmt_bytes_func) -> str:
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
        '⚠️ <b>/ass · 执行确认</b>',
        '─────────────────',
        f'📂 目录: <code>{html.escape(str(session.settings.target_dir))}</code>',
        f'🎬 计划视频: <code>{len(session.plan.items)}</code> · 🎞️ 字幕轨: <code>{session.plan.total_sub_tracks}</code>',
        f'⚙️ 并发: <code>{session.settings.jobs}</code> · 🧪 DRY: <code>{session.dry_run}</code>',
        f'🗑️ 外挂字幕: <code>{session.delete_external_subs}</code>',
        f'🎯 默认字幕轨: <code>{session.settings.set_default_subtitle}</code>（简体双语优先，简体回退，含 MKV 内置字幕）',
        f'⏳ 超时保护: <code>标准模式</code>（空闲 <code>{session.settings.idle_timeout_s}s</code> / 软告警 <code>{session.settings.soft_warn_after_s}s</code> / 极限 <code>{session.settings.hard_cap_s}s</code>）',
        '',
        '<b>执行前资源评估</b>',
        f'📦 源视频总大小: <code>{fmt_bytes_func(total_source)}</code>',
        f'📏 单集大小: 平均 <code>{fmt_bytes_func(avg_source)}</code> / 最大 <code>{fmt_bytes_func(max_source)}</code>',
        f'💾 预计临时占用: <code>{fmt_bytes_func(est_tmp)}</code>',
        f'🗄️ 临时目录剩余: <code>{fmt_bytes_func(free_bytes)}</code>' + (f' / <code>{fmt_bytes_func(total_bytes)}</code>' if total_bytes > 0 else ''),
        f'📍 临时目录与视频同分区: <code>{temp_same_fs}</code> (<code>{same_fs_count}</code>/<code>{len(session.plan.items)}</code>)',
    ]
    if duplicate_subtitle_refs > 0:
        lines.append(f'♻️ 重复引用字幕文件: <code>{duplicate_subtitle_refs}</code>')
    if missing_count > 0:
        lines.append(f'❌ 缺失 MKV: <code>{missing_count}</code>')
    if free_bytes > 0 and est_tmp > free_bytes:
        lines.append('⚠️ <b>警告：</b> 临时目录剩余空间可能不足，建议先调小并发或修改临时目录。')
    lines.extend(['', '确认后将开始调用 <code>mkvmerge</code> 写回视频文件。'])
    return '\n'.join(lines)


def format_mux_item_detail(item: Any, item_index: int) -> str:
    lines = [
        f'📝 <b>单集编辑 · 第 {item_index + 1} 项</b>',
        '─────────────────',
        f'🎬 MKV: <code>{html.escape(item.mkv)}</code>',
    ]
    for idx, sub in enumerate(item.subs, 1):
        lines.extend([
            '',
            f'<b>字幕 {idx}</b>',
            f'📄 文件: <code>{html.escape(sub.file)}</code>',
            f'🏷️ 字幕组: <code>{html.escape(sub.group or "-")}</code>',
            f'🌐 语言输入: <code>{html.escape(sub.lang_raw)}</code>',
            f'🧭 轨道语言: <code>{html.escape(sub.mkv_lang)}</code>',
            f'🪪 轨道名: <code>{html.escape(sub.track_name)}</code>',
        ])
    return join_lines_for_tg(lines, truncation_note='… <i>该条目详情过长，已自动截断；请继续修改单条字幕项查看。</i>')


def format_default_group_updated(group: str) -> str:
    return (
        '✅ <b>默认字幕组已更新</b> '
        f'<code>{html.escape(group or "-")}</code>\n'
        '💡 重新扫描后生效。'
    )


def format_default_lang_updated(lang: str) -> str:
    return (
        '✅ <b>默认语言已更新</b> '
        f'<code>{html.escape(lang)}</code>\n'
        '💡 重新扫描后生效。'
    )


def format_jobs_updated(jobs: int) -> str:
    return (
        '✅ <b>并发数已更新</b> '
        f'<code>{jobs}</code>\n'
        '💡 新并发数将用于本次会话后续执行。'
    )


def format_sub_file_updated(path_text: str) -> str:
    return '✅ <b>字幕文件已更新</b> ' f'<code>{html.escape(path_text)}</code>'


def format_track_group_updated(track_name: str, mkv_lang: str) -> str:
    return (
        '✅ <b>字幕组已更新</b> '
        f'<code>{html.escape(track_name)}</code> · <code>{html.escape(mkv_lang)}</code>'
    )


def format_subset_summary(summary: Any) -> str:
    prefix = '✅' if summary.failed == 0 else '⚠️'
    lines = [
        f'{prefix} <b>/ass · 子集化字体完成</b>',
        '─────────────────',
        f'📂 目录: <code>{html.escape(summary.target_dir)}</code>',
        f'📝 ASS: <code>{summary.total_ass}</code> · ✅ 成功: <code>{summary.processed}</code> · ⏭️ 跳过: <code>{summary.skipped}</code> · ❌ 失败: <code>{summary.failed}</code>',
        f'🧰 字体目录: <code>{summary.font_dirs}</code> · 字体包: <code>{summary.archives}</code>',
        f'🔤 OTF→TTF: <code>{summary.converted_otf}</code> · 跳过: <code>{summary.skipped_otf}</code>',
        f'🗑️ 删除原 ASS: <code>{summary.deleted_source_ass}</code> · 🧹 工作目录已清理: <code>{summary.cleaned_work_dir}</code>',
        f'⏱️ 耗时: <code>{summary.duration_s:.1f}s</code>',
    ]
    if summary.failures:
        lines.extend(['', '<b>失败明细：</b>'])
        for item in summary.failures[:10]:
            lines.append(f'• <code>{html.escape(item)}</code>')
        if len(summary.failures) > 10:
            lines.append(f'• ... 其余 <code>{len(summary.failures) - 10}</code> 项已省略')
    return '\n'.join(lines)


def format_mux_summary(summary: Any) -> str:
    prefix = '✅' if summary.failed == 0 else '⚠️'
    lines = [
        f'{prefix} <b>/ass · 字幕内封完成</b>',
        '─────────────────',
        f'📂 目录: <code>{html.escape(summary.target_dir)}</code>',
        f'🎬 MKV: <code>{summary.total_mkvs}</code> · 📋 计划: <code>{summary.matched_mkvs}</code> · 🎞️ 字幕轨: <code>{summary.total_sub_tracks}</code>',
        f'✅ 成功: <code>{summary.processed}</code> · ❌ 失败: <code>{summary.failed}</code> · ⚙️ 并发: <code>{summary.jobs}</code>',
        f'🧪 DRY: <code>{summary.dry_run}</code> · 🗑️ 外挂字幕: <code>{summary.delete_external_subs}</code> · 实删: <code>{summary.deleted_external_subs_count}</code>',
        f'♻️ 重复引用字幕: <code>{summary.duplicate_subtitle_refs}</code>',
        f'📄 计划文件: <code>{html.escape(summary.plan_path)}</code>',
        f'📦 临时目录: <code>{html.escape(summary.tmp_dir)}</code>',
        f'⏱️ 耗时: <code>{summary.duration_s:.1f}s</code>',
    ]
    if summary.failures:
        lines.extend(['', '<b>失败明细：</b>'])
        for item in summary.failures[:10]:
            lines.append(f'• <code>{html.escape(item)}</code>')
        if len(summary.failures) > 10:
            lines.append(f'• ... 其余 <code>{len(summary.failures) - 10}</code> 项已省略')
    return '\n'.join(lines)


def format_subset_running() -> str:
    return (
        '🔤 <b>/ass · 子集化字体</b>\n'
        '─────────────────\n\n'
        '⏳ 正在扫描 ASS / 字体 / 压缩包并执行子集化…\n'
        '💡 详细过程请查看 Docker 日志。'
    )


def format_mux_running(*, processed: int = 0, total: int | None = None, dry_run: bool = False) -> str:
    total_text = str(total if total is not None else '?')
    return (
        '🎞️ <b>/ass · 字幕内封执行中</b>\n'
        '─────────────────\n\n'
        f'📈 进度: <code>{processed}</code>/<code>{total_text}</code>（已内封视频/总视频）\n'
        '⏳ 正在调用 <code>mkvmerge</code> 执行计划…\n'
        + ('🧪 当前为 DRY-RUN，仅模拟执行，不会写回文件。\n' if dry_run else '')
        + '💡 详细过程请查看 Docker 日志。'
    )


def format_rescan_notice() -> str:
    return '🔄 <b>重新扫描计划</b>\n\n正在重新扫描目录、匹配字幕并刷新控制面板/预览…'


def format_rescan_running() -> str:
    return '🔄 <b>/ass · 重新扫描中</b>'


def prompt_default_group_text() -> str:
    return (
        '✏️ <b>修改默认字幕组</b>\n\n'
        '• 直接发送文字 = 设置字幕组\n'
        '• 发送 <code>-</code> = 清空字幕组\n'
        '• 修改后请回到主面板点“重新扫描”\n'
    )


def prompt_default_lang_text() -> str:
    return (
        '🌐 <b>修改默认语言</b>\n\n'
        '请输入语言，例如：<code>chs</code> / <code>cht</code> / <code>eng</code> / <code>chs_eng</code>\n'
        '💡 修改后请回到主面板点“重新扫描”\n'
    )


def prompt_sub_file_text() -> str:
    return (
        '📄 <b>修改字幕文件</b>\n\n'
        '请输入新的字幕文件名（只输入文件名，不要带路径）\n'
        '💡 文件较多时请参考独立预览或单集编辑页，不再自动整页展开。'
    )


def prompt_track_group_text() -> str:
    return (
        '🏷️ <b>修改字幕组</b>\n\n'
        '• 直接发送文字 = 设置字幕组\n'
        '• 发送 <code>-</code> = 清空字幕组'
    )


def prompt_track_lang_text() -> str:
    return (
        '🌐 <b>修改字幕语言</b>\n\n'
        '请输入新的字幕语言，例如：<code>chs</code> / <code>cht</code> / <code>eng</code> / <code>chs_eng</code>'
    )


def format_mux_error(exc_text: str) -> str:
    return f'❌ <b>/ass 字幕内封执行失败</b>\n\n<code>{html.escape(exc_text)}</code>'


def format_subset_error(exc_text: str) -> str:
    return f'❌ <b>/ass 字体子集化执行失败</b>\n\n<code>{html.escape(exc_text)}</code>'


def format_track_lang_updated(track_name: str, mkv_lang: str) -> str:
    return (
        '✅ <b>字幕语言已更新</b> '
        f'<code>{html.escape(track_name)}</code> · <code>{html.escape(mkv_lang)}</code>'
    )


