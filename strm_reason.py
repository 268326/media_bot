"""
STRM 原因码、条目状态、批次状态与文案映射
"""
from __future__ import annotations

UNKNOWN_REASON = "unknown"
INVALID_STRM_URL = "invalid_strm_url"
FFPROBE_FAILED = "ffprobe_failed"
NAME_CONFLICT = "name_conflict"
SOURCE_MISSING = "source_missing"
NOT_A_DIRECTORY = "not_a_directory"
PROCESSING_LEASE_EXPIRED = "processing_lease_expired"
DISAPPEARED_BEFORE_COMPLETION = "disappeared_before_completion"
RENAME_ERROR = "rename_error"
MOVE_DONE_FILE_ERROR = "move_done_file_error"
MOVE_FAILED_STRM_ERROR = "move_failed_strm_error"
MOVE_DONE_FOLDER_ERROR = "move_done_folder_error"
SUBTITLE_MOVE_ERROR = "subtitle_move_error"
SUBTITLE_RENAME_ERROR = "subtitle_rename_error"

STATUS_PENDING = "pending"
STATUS_PROCESSING = "processing"
STATUS_DONE = "done"
STATUS_ALREADY_OK = "already_ok"
STATUS_FAILED = "failed"
STATUS_MISSING = "missing"
ITEM_STATUS_ORDER = (
    STATUS_PENDING,
    STATUS_PROCESSING,
    STATUS_DONE,
    STATUS_ALREADY_OK,
    STATUS_FAILED,
    STATUS_MISSING,
)
KNOWN_ITEM_STATUSES = set(ITEM_STATUS_ORDER)
ACTIVE_ITEM_STATUSES = {STATUS_PENDING, STATUS_PROCESSING}
FINAL_ITEM_STATUSES = {STATUS_DONE, STATUS_ALREADY_OK, STATUS_FAILED}
BLOCKING_ITEM_STATUSES = {STATUS_PENDING, STATUS_PROCESSING, STATUS_MISSING}

BATCH_STATUS_ACTIVE = "active"
BATCH_STATUS_COMPLETED = "completed"
BATCH_STATUS_FAILED = "failed"
KNOWN_BATCH_STATUS_CODES = {
    BATCH_STATUS_ACTIVE,
    BATCH_STATUS_COMPLETED,
    BATCH_STATUS_FAILED,
}

REASON_LABELS = {
    UNKNOWN_REASON: "未知原因",
    INVALID_STRM_URL: "STRM 内容不是有效的 http/https 链接",
    FFPROBE_FAILED: "ffprobe 探测失败",
    NAME_CONFLICT: "目标文件名已存在",
    SOURCE_MISSING: "源目录不存在",
    NOT_A_DIRECTORY: "源路径不是目录",
    PROCESSING_LEASE_EXPIRED: "处理超时，已回退待重试",
    DISAPPEARED_BEFORE_COMPLETION: "文件在完成前消失",
    RENAME_ERROR: "重命名失败",
    MOVE_DONE_FILE_ERROR: "单文件归档失败",
    MOVE_FAILED_STRM_ERROR: "失败文件转移失败",
    MOVE_DONE_FOLDER_ERROR: "目录归档失败",
    SUBTITLE_MOVE_ERROR: "字幕移动失败",
    SUBTITLE_RENAME_ERROR: "字幕重命名失败",
}

BATCH_STATUS_LABELS = {
    BATCH_STATUS_ACTIVE: "活跃",
    BATCH_STATUS_COMPLETED: "已完成",
    BATCH_STATUS_FAILED: "失败",
}

DETAIL_REASON_CODES = {
    RENAME_ERROR,
    MOVE_DONE_FILE_ERROR,
    MOVE_FAILED_STRM_ERROR,
    MOVE_DONE_FOLDER_ERROR,
    SUBTITLE_MOVE_ERROR,
    SUBTITLE_RENAME_ERROR,
}


def normalize_item_status(status: str) -> str:
    text = str(status or "").strip()
    return text if text in KNOWN_ITEM_STATUSES else STATUS_PENDING


def split_batch_status(status: str) -> tuple[str, str]:
    text = str(status or "").strip()
    if not text:
        return BATCH_STATUS_ACTIVE, ""
    failed_prefix = f"{BATCH_STATUS_FAILED}:"
    if text.startswith(failed_prefix):
        return BATCH_STATUS_FAILED, text[len(failed_prefix):].strip()
    if text in KNOWN_BATCH_STATUS_CODES:
        return text, ""
    return BATCH_STATUS_ACTIVE, ""


def make_batch_status(code: str, detail: str = "") -> str:
    batch_code, _ = split_batch_status(code)
    detail = str(detail or "").strip()
    if batch_code == BATCH_STATUS_FAILED and detail:
        return f"{BATCH_STATUS_FAILED}:{detail}"
    return batch_code


def humanize_batch_status(status: str) -> str:
    code, detail = split_batch_status(status)
    label = BATCH_STATUS_LABELS.get(code, code)
    if code == BATCH_STATUS_FAILED and detail:
        return f"{label}: {humanize_reason(detail)}"
    return label


def make_reason(code: str, detail: str = "") -> str:
    code = str(code or UNKNOWN_REASON).strip() or UNKNOWN_REASON
    detail = str(detail or "").strip()
    if not detail:
        return code
    if code in DETAIL_REASON_CODES:
        return f"{code}: {detail}"
    return code


def split_reason(reason: str) -> tuple[str, str]:
    text = str(reason or "").strip()
    if not text:
        return UNKNOWN_REASON, ""
    for code in DETAIL_REASON_CODES:
        prefix = f"{code}:"
        if text.startswith(prefix):
            return code, text[len(prefix):].strip()
    return text, ""


def humanize_reason(reason: str) -> str:
    text = str(reason or "").strip()
    if not text:
        return REASON_LABELS[UNKNOWN_REASON]
    if " | " in text:
        return "；".join(humanize_reason(part) for part in text.split(" | ") if part.strip())
    code, detail = split_reason(text)
    label = REASON_LABELS.get(code)
    if not label:
        return text
    return f"{label}: {detail}" if detail else label
