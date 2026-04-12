"""
STRM 监控功能配置模型
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class StrmSettings:
    enabled: bool = False
    ffprobe_path: str = "/usr/local/bin/ffprobe"
    watch_dir: str = ""
    done_dir: str = ""
    failed_dir: str = ""
    max_workers: int = 3
    timeout_s: int = 60
    max_retries: int = 2
    rw_timeout_us: int = 15000000
    probesize: str = "12M"
    analyzeduration: str = "3000000"
    recent_event_ttl: int = 10
    idle_seconds: int = 30
    min_folder_age_seconds: int = 60
    only_first_level_dir: bool = True
