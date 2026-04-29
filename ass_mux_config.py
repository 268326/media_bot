from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from config import ALLOWED_USER_ID, TGBOT_NOTIFY_CHAT_ID

TRUTHY = {"1", "true", "yes", "on"}


@dataclass(slots=True)
class AssMuxSettings:
    target_dir: Path
    tmp_dir: Path
    plan_path: Path
    recursive: bool
    jobs: int
    default_lang: str
    default_group: str
    delete_external_subs_default: bool
    allow_cross_fs: bool
    notify_chat_id: str
    mkvmerge_bin: str
    idle_timeout_s: int = 1800
    soft_warn_after_s: int = 7200
    hard_cap_s: int = 43200
    progress_poll_interval_s: int = 5
    terminate_grace_s: int = 15


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in TRUTHY


def _env_int(name: str, default: int, *, minimum: int | None = None) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        value = default
    else:
        try:
            value = int(raw.strip())
        except ValueError:
            value = default
    if minimum is not None:
        value = max(minimum, value)
    return value


def load_ass_mux_settings_from_env() -> AssMuxSettings:
    target_dir = Path(os.getenv("ASS_MUX_TARGET_DIR", "").strip() or "/ass_mux_target")

    tmp_dir_raw = os.getenv("ASS_MUX_TMP_DIR", "").strip()
    tmp_dir = Path(tmp_dir_raw) if tmp_dir_raw else target_dir / ".ass_mux_tmp"

    plan_path_raw = os.getenv("ASS_MUX_PLAN_PATH", "").strip()
    plan_path = Path(plan_path_raw) if plan_path_raw else target_dir / ".ass_mux_plan.json"

    notify_chat_id = os.getenv("ASS_MUX_NOTIFY_CHAT_ID", "").strip()
    if not notify_chat_id:
        notify_chat_id = str(TGBOT_NOTIFY_CHAT_ID or "").strip()
    if not notify_chat_id and ALLOWED_USER_ID:
        notify_chat_id = str(ALLOWED_USER_ID)

    jobs_raw = os.getenv("ASS_MUX_JOBS", "2").strip() or "2"
    try:
        jobs = max(1, int(jobs_raw))
    except ValueError:
        jobs = 2

    return AssMuxSettings(
        target_dir=target_dir,
        tmp_dir=tmp_dir,
        plan_path=plan_path,
        recursive=_env_bool("ASS_MUX_RECURSIVE", False),
        jobs=jobs,
        default_lang=os.getenv("ASS_MUX_DEFAULT_LANG", "chs").strip() or "chs",
        default_group=os.getenv("ASS_MUX_DEFAULT_GROUP", "").strip(),
        delete_external_subs_default=_env_bool("ASS_MUX_DELETE_EXTERNAL_SUBS", False),
        allow_cross_fs=_env_bool("ASS_MUX_ALLOW_CROSS_FS", False),
        notify_chat_id=notify_chat_id,
        mkvmerge_bin=os.getenv("ASS_MKVMERGE_BIN", "mkvmerge").strip() or "mkvmerge",
        idle_timeout_s=_env_int("ASS_MUX_IDLE_TIMEOUT_SECONDS", 1800, minimum=60),
        soft_warn_after_s=_env_int("ASS_MUX_SOFT_WARN_AFTER_SECONDS", 7200, minimum=0),
        hard_cap_s=_env_int("ASS_MUX_HARD_CAP_SECONDS", 43200, minimum=0),
        progress_poll_interval_s=_env_int("ASS_MUX_PROGRESS_POLL_INTERVAL_SECONDS", 5, minimum=1),
        terminate_grace_s=_env_int("ASS_MUX_TERMINATE_GRACE_SECONDS", 15, minimum=1),
    )
