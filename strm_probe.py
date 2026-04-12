"""
STRM 文件读取与 ffprobe 探测
"""
from __future__ import annotations

import json
import logging
import subprocess
import time
from pathlib import Path

from strm_config import StrmSettings


def read_strm_url(p: Path) -> str | None:
    try:
        url = p.read_text(encoding="utf-8").strip()
    except Exception:
        return None
    return url if url.startswith("http") else None


def run_ffprobe(url: str, settings: StrmSettings) -> dict | None:
    cmd = [
        settings.ffprobe_path,
        "-threads",
        "1",
        "-v",
        "quiet",
        "-print_format",
        "json",
        "-show_streams",
        "-show_format",
        "-probesize",
        settings.probesize,
        "-analyzeduration",
        settings.analyzeduration,
    ]
    if settings.rw_timeout_us and int(settings.rw_timeout_us) > 0:
        cmd += ["-rw_timeout", str(int(settings.rw_timeout_us))]
    cmd.append(url)

    last_err = None
    for i in range(settings.max_retries + 1):
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=settings.timeout_s)
            if result.returncode != 0:
                last_err = (result.stderr or "").strip()[:300]
                raise RuntimeError(f"ffprobe rc={result.returncode}: {last_err}")
            if not result.stdout.strip():
                last_err = (result.stderr or "").strip()[:300]
                raise RuntimeError(f"empty ffprobe output: {last_err}")
            return json.loads(result.stdout)
        except subprocess.TimeoutExpired:
            last_err = "timeout"
        except json.JSONDecodeError:
            last_err = "json decode error"
        except Exception as exc:
            last_err = str(exc)[:300]

        if i < settings.max_retries:
            time.sleep(2 * (2**i))

    logging.debug("ffprobe failed: %s", last_err)
    return None
