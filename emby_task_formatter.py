from __future__ import annotations

import html
from collections import defaultdict

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

EMBY_TASK_CALLBACK_PREFIX = "emby_task"
EMBY_TASK_PAGE_SIZE = 6


FILTER_LABELS = {
    "all": "全部",
    "running": "运行中",
    "pro": "神医PRO",
    "library": "媒体库",
    "maintenance": "维护",
    "app": "系统",
}


def _cb(action: str, value: str = "") -> str:
    return f"{EMBY_TASK_CALLBACK_PREFIX}:{action}:{value}"


def _truncate(text: str, limit: int) -> str:
    value = str(text or "")
    if len(value) <= limit:
        return value
    return value[: max(1, limit - 1)] + "…"


def _category_text(task: dict) -> str:
    return str(task.get("display_category") or task.get("category") or "")


def _is_running(task: dict) -> bool:
    return bool(task.get("is_running")) or str(task.get("state") or "").lower() == "running"


def describe_filter_mode(filter_mode: str) -> str:
    return FILTER_LABELS.get(filter_mode, FILTER_LABELS["all"])


def filter_tasks_for_view(tasks: list[dict], filter_mode: str) -> list[dict]:
    mode = str(filter_mode or "all")
    if mode == "all":
        return list(tasks)
    if mode == "running":
        return [task for task in tasks if _is_running(task)]
    if mode == "pro":
        return [task for task in tasks if "神医助手" in _category_text(task) or "PRO" in _category_text(task)]
    if mode == "library":
        return [task for task in tasks if "媒体库" in _category_text(task) or _category_text(task) == "Library"]
    if mode == "maintenance":
        return [task for task in tasks if "维护" in _category_text(task) or _category_text(task) == "Maintenance"]
    if mode == "app":
        return [task for task in tasks if "系统" in _category_text(task) or _category_text(task) == "Application"]
    return list(tasks)


def normalize_tasks_page(tasks: list[dict], page: int, filter_mode: str) -> int:
    visible_tasks = filter_tasks_for_view(tasks, filter_mode)
    total = len(visible_tasks)
    total_pages = max(1, (total + EMBY_TASK_PAGE_SIZE - 1) // EMBY_TASK_PAGE_SIZE)
    return min(max(int(page), 0), total_pages - 1)


def _running_count(tasks: list[dict]) -> int:
    return sum(1 for task in tasks if _is_running(task))


def _task_status_text(task: dict) -> str:
    status_text = str(task.get("status_text") or "-")
    progress = task.get("progress")
    if progress is not None and _is_running(task) and "%" not in status_text:
        try:
            status_text = f"{status_text} {int(float(progress))}%"
        except (TypeError, ValueError):
            status_text = f"{status_text} {progress}%"
    return status_text


def _filter_button_text(current_mode: str, target_mode: str, label: str) -> str:
    return f"✅{label}" if current_mode == target_mode else label


def build_tasks_panel(
    tasks: list[dict],
    *,
    page: int = 0,
    notify_enabled: bool = True,
    status: dict | None = None,
    filter_mode: str = "all",
    quick_actions: list[tuple[str, str]] | None = None,
) -> tuple[str, InlineKeyboardMarkup]:
    current_page = normalize_tasks_page(tasks, page, filter_mode)
    visible_tasks = filter_tasks_for_view(tasks, filter_mode)
    total_all = len(tasks)
    running_total = _running_count(tasks)
    total = len(visible_tasks)
    total_pages = max(1, (total + EMBY_TASK_PAGE_SIZE - 1) // EMBY_TASK_PAGE_SIZE)
    start = current_page * EMBY_TASK_PAGE_SIZE
    items = visible_tasks[start:start + EMBY_TASK_PAGE_SIZE]

    poll_interval = ""
    if status and status.get("poll_interval"):
        poll_interval = f" · 轮询<code>{html.escape(str(status['poll_interval']))}s</code>"

    lines = [
        "🧩 <b>Emby 任务</b>",
        f"运行 <code>{running_total}</code>/<code>{total_all}</code> · 通知 <code>{'ON' if notify_enabled else 'OFF'}</code>{poll_interval}",
        f"视图 <code>{describe_filter_mode(filter_mode)}</code> · <code>{total}</code>项 · 第 <code>{current_page + 1}</code>/<code>{total_pages}</code> 页",
        "━━━━━━━━━━━━━━━━━━━━━━",
    ]

    if not items:
        lines.append("当前视图下暂无任务")
    else:
        for idx, task in enumerate(items, start=1):
            display_index = start + idx
            icon = "🟢" if _is_running(task) else "⚪️"
            lines.append(f"{icon} <b>{display_index}. {_truncate(str(task.get('display_name') or task.get('name') or '-'), 24)}</b>")
            lines.append(f"   {_truncate(_category_text(task), 12)} · {_truncate(_task_status_text(task), 18)}")

    text = "\n".join(lines)

    keyboard: list[list[InlineKeyboardButton]] = []
    if items:
        row: list[InlineKeyboardButton] = []
        for idx, task in enumerate(items, start=1):
            task_id = str(task.get("id") or "")
            row.append(InlineKeyboardButton(text=str(idx), callback_data=_cb("detail", task_id)))
            if len(row) == 3:
                keyboard.append(row)
                row = []
        if row:
            keyboard.append(row)

    if quick_actions:
        rows: list[list[InlineKeyboardButton]] = []
        row: list[InlineKeyboardButton] = []
        for label, task_id in quick_actions[:6]:
            row.append(InlineKeyboardButton(text=label, callback_data=_cb("quick_start", task_id)))
            if len(row) == 2:
                rows.append(row)
                row = []
        if row:
            rows.append(row)
        keyboard.extend(rows)

    prev_page = current_page - 1 if current_page > 0 else current_page
    next_page = current_page + 1 if current_page < total_pages - 1 else current_page
    keyboard.append([
        InlineKeyboardButton(text="⬅️", callback_data=_cb("page", str(prev_page))),
        InlineKeyboardButton(text=f"{current_page + 1}/{total_pages}", callback_data=_cb("noop", str(current_page))),
        InlineKeyboardButton(text="➡️", callback_data=_cb("page", str(next_page))),
    ])
    keyboard.append([
        InlineKeyboardButton(text=_filter_button_text(filter_mode, "pro", "💎PRO"), callback_data=_cb("filter", "pro")),
        InlineKeyboardButton(text=_filter_button_text(filter_mode, "library", "📚库"), callback_data=_cb("filter", "library")),
        InlineKeyboardButton(text=_filter_button_text(filter_mode, "maintenance", "🛠维护"), callback_data=_cb("filter", "maintenance")),
    ])
    keyboard.append([
        InlineKeyboardButton(text=_filter_button_text(filter_mode, "app", "⚙️系统"), callback_data=_cb("filter", "app")),
        InlineKeyboardButton(text=_filter_button_text(filter_mode, "running", "🟢运行"), callback_data=_cb("filter", "running")),
        InlineKeyboardButton(text=_filter_button_text(filter_mode, "all", "📋全部"), callback_data=_cb("filter", "all")),
    ])
    keyboard.append([
        InlineKeyboardButton(text="📊统计", callback_data=_cb("summary", filter_mode)),
        InlineKeyboardButton(text="🔄刷新", callback_data=_cb("refresh", str(current_page))),
    ])
    keyboard.append([
        InlineKeyboardButton(
            text=(f"🔕通知 {'ON' if notify_enabled else 'OFF'}" if notify_enabled else f"🔔通知 {'ON' if notify_enabled else 'OFF'}"),
            callback_data=_cb("toggle_notify", str(current_page)),
        )
    ])

    return text, InlineKeyboardMarkup(inline_keyboard=keyboard)


def build_task_detail(
    tasks: list[dict],
    task_id: str,
    *,
    page: int = 0,
    filter_mode: str = "all",
) -> tuple[str, InlineKeyboardMarkup]:
    visible_tasks = filter_tasks_for_view(tasks, filter_mode)
    index = -1
    for idx, task in enumerate(visible_tasks):
        if str(task.get("id") or "") == str(task_id or ""):
            index = idx
            break
    if index < 0:
        raise IndexError("task not found in current view")

    detail_page = index // EMBY_TASK_PAGE_SIZE
    task = visible_tasks[index]
    current_task_id = str(task.get("id") or "")
    icon = "🟢" if _is_running(task) else "⚪️"

    display_name = str(task.get("display_name") or task.get("name") or "-")
    lines = [
        f"{icon} <b>{html.escape(_truncate(display_name, 30))}</b>",
        f"分类 <code>{html.escape(_category_text(task))}</code>",
        f"状态 <code>{html.escape(_task_status_text(task))}</code>",
        f"上次 <code>{html.escape(str(task.get('last_result_text') or '-'))}</code>",
        f"下次 <code>{html.escape(str(task.get('next_run_text') or '-'))}</code>",
    ]

    keyboard: list[list[InlineKeyboardButton]] = []
    keyboard.append([
        InlineKeyboardButton(
            text=("🛑停止" if _is_running(task) else "▶️启动"),
            callback_data=_cb("stop" if _is_running(task) else "start", current_task_id),
        ),
        InlineKeyboardButton(text="🔄刷新", callback_data=_cb("detail", current_task_id)),
    ])

    nav_row: list[InlineKeyboardButton] = []
    if index > 0:
        prev_id = str(visible_tasks[index - 1].get("id") or "")
        nav_row.append(InlineKeyboardButton(text="⬅️上个", callback_data=_cb("detail", prev_id)))
    nav_row.append(InlineKeyboardButton(text="📋列表", callback_data=_cb("page", str(detail_page))))
    if index < len(visible_tasks) - 1:
        next_id = str(visible_tasks[index + 1].get("id") or "")
        nav_row.append(InlineKeyboardButton(text="下个➡️", callback_data=_cb("detail", next_id)))
    keyboard.append(nav_row)

    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=keyboard)


def build_category_summary(tasks: list[dict], *, title: str = "任务统计") -> str:
    grouped: dict[str, int] = defaultdict(int)
    running = 0
    for task in tasks:
        grouped[_category_text(task) or "未分类"] += 1
        if _is_running(task):
            running += 1

    lines = [
        f"📚 <b>{html.escape(title)}</b>",
        f"总数 <code>{len(tasks)}</code> · 运行中 <code>{running}</code>",
        "━━━━━━━━━━━━━━━━━━━━━━",
    ]
    for category in sorted(grouped.keys()):
        lines.append(f"• {html.escape(_truncate(category, 18))}: <code>{grouped[category]}</code>")
    return "\n".join(lines)
