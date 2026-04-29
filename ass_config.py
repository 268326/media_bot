from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from config import ALLOWED_USER_ID, TGBOT_NOTIFY_CHAT_ID


@dataclass(slots=True)
class AssSettings:
    target_dir: Path
    work_dir: Path
    recursive: bool
    include_system_fonts: bool
    notify_chat_id: str
    assfonts_bin: str
    fontforge_bin: str
    sevenz_bin: str
    unzip_bin: str


TRUTHY = {"1", "true", "yes", "on"}


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in TRUTHY


def load_ass_settings_from_env() -> AssSettings:
    target_dir = Path(os.getenv("ASS_TARGET_DIR", "").strip() or "/ass_target")
    work_dir_raw = os.getenv("ASS_WORK_DIR", "").strip()
    work_dir = Path(work_dir_raw) if work_dir_raw else target_dir / ".assfonts_pipeline_work"

    notify_chat_id = os.getenv("ASS_NOTIFY_CHAT_ID", "").strip()
    if not notify_chat_id:
        notify_chat_id = str(TGBOT_NOTIFY_CHAT_ID or "").strip()
    if not notify_chat_id and ALLOWED_USER_ID:
        notify_chat_id = str(ALLOWED_USER_ID)

    return AssSettings(
        target_dir=target_dir,
        work_dir=work_dir,
        recursive=_env_bool("ASS_RECURSIVE", False),
        include_system_fonts=_env_bool("ASS_INCLUDE_SYSTEM_FONTS", True),
        notify_chat_id=notify_chat_id,
        assfonts_bin=os.getenv("ASSFONTS_BIN", "/usr/local/bin/assfonts").strip() or "/usr/local/bin/assfonts",
        fontforge_bin=os.getenv("ASS_FONTFORGE_BIN", "fontforge").strip() or "fontforge",
        sevenz_bin=os.getenv("ASS_7Z_BIN", "7z").strip() or "7z",
        unzip_bin=os.getenv("ASS_UNZIP_BIN", "unzip").strip() or "unzip",
    )
