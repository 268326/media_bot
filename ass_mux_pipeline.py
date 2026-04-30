from __future__ import annotations

import json
import logging
import os
import queue
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from queue import Empty, Queue
from typing import Callable

from ass_mux_config import AssMuxSettings
from ass_mux_planner import MuxPlan, MuxPlanItem, infer_lang_raw_from_subtitle_name, mux_plan_from_dict
from ass_utils import AssPipelineError, ensure_dir

logger = logging.getLogger(__name__)
PRINT_LOCK = threading.Lock()
PROC_LOCK = threading.Lock()
RUNNING_PROCS: dict[str, subprocess.Popen[str]] = {}


@dataclass(slots=True)
class MuxRunSummary:
    target_dir: str
    tmp_dir: str
    plan_path: str
    total_mkvs: int
    matched_mkvs: int
    total_sub_tracks: int
    processed: int
    failed: int
    dry_run: bool
    delete_external_subs: bool
    jobs: int
    total_source_size_bytes: int
    avg_source_size_bytes: int
    max_source_size_bytes: int
    estimated_tmp_bytes: int
    tmp_free_bytes: int
    tmp_total_bytes: int
    temp_same_filesystem: bool
    duplicate_subtitle_refs: int
    deleted_external_subs_count: int
    duration_s: float
    failures: list[str]


@dataclass(slots=True)
class MuxProgressEvent:
    processed: int
    total: int
    current_file: str = ""
    ok: bool = True


@dataclass(slots=True)
class MuxProcessResult:
    ok: bool
    message: str | None = None
    deleted_subs: list[str] | None = None


def fmt_bytes(size: int) -> str:
    units = ["B", "KiB", "MiB", "GiB", "TiB"]
    value = float(size)
    for unit in units:
        if value < 1024.0 or unit == units[-1]:
            return f"{value:.2f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024.0
    return f"{value:.2f} TiB"


def safe_stat_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except Exception:
        return 0


def same_filesystem(src: Path, dst_dir: Path) -> bool:
    return os.stat(src).st_dev == os.stat(dst_dir).st_dev


def _existing_probe_path(path: Path) -> Path:
    current = path
    while not current.exists():
        parent = current.parent
        if parent == current:
            break
        current = parent
    return current


def quote(arg: str) -> str:
    if arg == "":
        return "''"
    if all(ch.isalnum() or ch in "._-/:=@+" for ch in arg):
        return arg
    return "'" + arg.replace("'", "'\"'\"'") + "'"


def _log(prefix: str, message: str, *, level: int = logging.INFO) -> None:
    with PRINT_LOCK:
        logger.log(level, "%s %s", prefix, message)


def run_mkvmerge_stream(
    cmd: list[str],
    prefix: str,
    stop_event: threading.Event,
    *,
    dry_run: bool,
    tmp_path: Path,
    settings: AssMuxSettings,
) -> int:
    cmd_str = " ".join(quote(item) for item in cmd)
    _log(prefix, f"CMD: {cmd_str}")

    if dry_run:
        _log(prefix, "DRY: skip execute/replace")
        return 0

    if stop_event.is_set():
        _log(prefix, "SKIP: stop already triggered")
        return 99

    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    with PROC_LOCK:
        RUNNING_PROCS[prefix] = proc

    line_queue: queue.Queue[str | None] = queue.Queue()

    def _reader() -> None:
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                line_queue.put(line.rstrip("\n"))
        finally:
            line_queue.put(None)

    reader = threading.Thread(target=_reader, daemon=True)
    reader.start()

    started_at = time.monotonic()
    last_progress_at = started_at
    last_size = -1
    last_mtime_ns = -1
    warned = False
    timeout_reason: str | None = None
    terminate_sent_at: float | None = None

    try:
        while True:
            now = time.monotonic()

            while True:
                try:
                    line = line_queue.get_nowait()
                except queue.Empty:
                    break
                if line is None:
                    break
                last_progress_at = now
                _log(prefix, line)

            try:
                stat = tmp_path.stat()
                if stat.st_size != last_size or stat.st_mtime_ns != last_mtime_ns:
                    last_size = stat.st_size
                    last_mtime_ns = stat.st_mtime_ns
                    last_progress_at = now
                current_size = stat.st_size
            except Exception:
                current_size = 0

            elapsed = now - started_at
            idle_for = now - last_progress_at

            if settings.soft_warn_after_s > 0 and not warned and elapsed >= settings.soft_warn_after_s:
                warned = True
                _log(
                    prefix,
                    f"⚠️ mkvmerge 已运行 {int(elapsed)} 秒，仍检测到任务存活则继续等待（标准模式仅告警不清理）",
                    level=logging.WARNING,
                )

            if timeout_reason is None and settings.hard_cap_s > 0 and elapsed >= settings.hard_cap_s:
                timeout_reason = 'hard_cap'
                terminate_sent_at = now
                _log(
                    prefix,
                    f"⚠️ mkvmerge 达到极限保护时间 {settings.hard_cap_s}s，开始终止进程",
                    level=logging.WARNING,
                )
                try:
                    proc.terminate()
                except Exception:
                    pass

            if timeout_reason is None and settings.idle_timeout_s > 0 and idle_for >= settings.idle_timeout_s:
                timeout_reason = 'idle'
                terminate_sent_at = now
                _log(
                    prefix,
                    f"⚠️ mkvmerge 已连续 {int(idle_for)} 秒无输出且临时文件无增长，当前大小 {fmt_bytes(current_size)}，开始终止进程",
                    level=logging.WARNING,
                )
                try:
                    proc.terminate()
                except Exception:
                    pass

            if timeout_reason is None and stop_event.is_set():
                timeout_reason = 'stop_event'
                terminate_sent_at = now
                try:
                    proc.terminate()
                except Exception:
                    pass

            if timeout_reason and terminate_sent_at and proc.poll() is None and now - terminate_sent_at >= settings.terminate_grace_s:
                _log(prefix, f"⚠️ 进程在 {settings.terminate_grace_s}s 宽限期后仍未退出，执行 kill", level=logging.WARNING)
                try:
                    proc.kill()
                except Exception:
                    pass

            rc = proc.poll()
            if rc is not None:
                if timeout_reason == 'idle':
                    return 124
                if timeout_reason == 'hard_cap':
                    return 137
                if timeout_reason == 'stop_event':
                    return 99
                return rc

            time.sleep(settings.progress_poll_interval_s)
    finally:
        reader.join(timeout=2)
        with PROC_LOCK:
            RUNNING_PROCS.pop(prefix, None)


def terminate_other_jobs() -> None:
    with PROC_LOCK:
        procs = list(RUNNING_PROCS.items())

    for prefix, proc in procs:
        try:
            _log(prefix, "⚠️ 终止中（因为有任务失败）…", level=logging.WARNING)
            proc.terminate()
        except Exception:
            pass

    time.sleep(1.0)

    with PROC_LOCK:
        procs = list(RUNNING_PROCS.items())

    for prefix, proc in procs:
        try:
            if proc.poll() is None:
                _log(prefix, "⚠️ 强制 kill…", level=logging.WARNING)
                proc.kill()
        except Exception:
            pass


def _normalize_plan(plan: MuxPlan | dict) -> MuxPlan:
    if isinstance(plan, MuxPlan):
        return plan
    return mux_plan_from_dict(plan)


def identify_mkv_subtitle_tracks(mkv_path: Path, mkvmerge_bin: str) -> list[dict[str, object]]:
    try:
        result = subprocess.run(
            [mkvmerge_bin, '-J', str(mkv_path)],
            text=True,
            capture_output=True,
            check=False,
        )
    except Exception as exc:
        logger.warning('⚠️ 识别 MKV 内置字幕失败: %s (%s)', mkv_path, exc)
        return []

    if result.returncode != 0:
        logger.warning('⚠️ mkvmerge -J 失败: %s rc=%s stderr=%s', mkv_path, result.returncode, (result.stderr or '').strip())
        return []

    try:
        data = json.loads(result.stdout or '{}')
    except Exception as exc:
        logger.warning('⚠️ 解析 MKV 轨道 JSON 失败: %s (%s)', mkv_path, exc)
        return []

    tracks: list[dict[str, object]] = []
    for track in data.get('tracks') or []:
        if str(track.get('type') or '') != 'subtitles':
            continue
        props = track.get('properties') or {}
        track_name = str(props.get('track_name') or '')
        language = str(props.get('language_ietf') or props.get('language') or '')
        lang_raw = infer_lang_raw_from_subtitle_name(f'{track_name} {language}', '')
        tracks.append({
            'id': int(track.get('id') or 0),
            'track_name': track_name,
            'language': language,
            'lang_raw': lang_raw,
            'default_track': bool(props.get('default_track')),
        })
    return tracks


def subtitle_preference_score(lang_raw: str) -> int:
    text = str(lang_raw or '').strip().lower()
    if not text:
        return 0

    normalized = re.sub(r"[\s&+\-/|／＋＆｜]+", "_", text)
    parts = [part for part in normalized.split('_') if part]
    aliases = {
        'chs': 'chs', 'sc': 'chs', 'zh': 'chs', 'cn': 'chs', 'gb': 'chs', 'gbk': 'chs', 'gb2312': 'chs', 'zhhans': 'chs', 'zhcn': 'chs', 'zhchs': 'chs',
        'cht': 'cht', 'tc': 'cht', 'tw': 'cht', 'hk': 'cht', 'big5': 'cht', 'zhhant': 'cht', 'zhtw': 'cht', 'zhhk': 'cht',
        'jpn': 'jpn', 'ja': 'jpn', 'jp': 'jpn',
        'eng': 'eng', 'en': 'eng',
    }
    canonical_parts: list[str] = []
    for part in parts:
        canonical = aliases.get(part, part)
        if canonical not in canonical_parts:
            canonical_parts.append(canonical)

    if not canonical_parts:
        return 0
    if 'chs' in canonical_parts and len(canonical_parts) >= 2:
        return 300
    if canonical_parts == ['chs']:
        return 200
    return 0


def choose_default_subtitle_candidate(mkv_tracks: list[dict[str, object]], item: MuxPlanItem) -> dict[str, object] | None:
    candidates: list[dict[str, object]] = []

    for track in mkv_tracks:
        score = subtitle_preference_score(str(track.get('lang_raw') or ''))
        if score <= 0:
            continue
        candidates.append({
            'source': 'internal',
            'score': score,
            'is_current_default': bool(track.get('default_track')),
            'track_id': int(track.get('id') or 0),
            'lang_raw': str(track.get('lang_raw') or ''),
            'track_name': str(track.get('track_name') or ''),
        })

    for sub_index, sub in enumerate(item.subs):
        score = subtitle_preference_score(sub.lang_raw)
        if score <= 0:
            continue
        candidates.append({
            'source': 'external',
            'score': score,
            'is_current_default': False,
            'sub_index': sub_index,
            'lang_raw': sub.lang_raw,
            'track_name': sub.track_name,
        })

    if not candidates:
        return None

    candidates.sort(
        key=lambda item: (
            int(item['score']),
            int(bool(item['is_current_default'])),
            int(item['source'] == 'external'),
        ),
        reverse=True,
    )
    return candidates[0]


def process_one_item(
    item: MuxPlanItem,
    settings: AssMuxSettings,
    target_dir: Path,
    stop_event: threading.Event,
    failures: list[str],
    failures_lock: threading.Lock,
    processed_counter: list[int],
    processed_lock: threading.Lock,
    *,
    total_items: int,
    progress_callback: Callable[[MuxProgressEvent], None] | None,
    dry_run: bool,
    delete_external_subs: bool,
) -> int:
    mkv_path = target_dir / item.mkv
    prefix = f"[{mkv_path.stem[-8:]}]"

    if stop_event.is_set():
        _log(prefix, f"SKIP: stop_event 已触发，跳过 {mkv_path.name}")
        return 99

    if not mkv_path.exists():
        msg = f"{mkv_path.name}: missing mkv"
        _log(prefix, msg, level=logging.ERROR)
        with failures_lock:
            failures.append(msg)
        return 2

    ensure_dir(settings.tmp_dir.expanduser().resolve())
    tmp_dir = settings.tmp_dir.expanduser().resolve()
    if not settings.allow_cross_fs and not same_filesystem(mkv_path, tmp_dir):
        msg = f"{mkv_path.name}: cross filesystem not allowed"
        _log(prefix, msg, level=logging.ERROR)
        with failures_lock:
            failures.append(msg)
        return 3

    tmp_path = tmp_dir / f"{mkv_path.stem[:80]}.{os.getpid()}.{time.time_ns()}.tmp{mkv_path.suffix}"
    mkv_subtitle_tracks = identify_mkv_subtitle_tracks(mkv_path, settings.mkvmerge_bin) if settings.set_default_subtitle else []
    default_candidate = choose_default_subtitle_candidate(mkv_subtitle_tracks, item) if settings.set_default_subtitle else None

    cmd = [settings.mkvmerge_bin, "-o", str(tmp_path)]
    if default_candidate is not None:
        for track in mkv_subtitle_tracks:
            track_id = int(track.get('id') if track.get('id') is not None else 0)
            candidate_track_id = default_candidate.get('track_id')
            default_value = '1' if default_candidate['source'] == 'internal' and candidate_track_id is not None and track_id == int(candidate_track_id) else '0'
            cmd.extend(["--default-track-flag", f"{track_id}:{default_value}"])
        _log(prefix, f"Auto default subtitle => {default_candidate['source']} / {default_candidate.get('lang_raw')} / {default_candidate.get('track_name')}")
    cmd.append(str(mkv_path))

    for sub_index, sub in enumerate(item.subs):
        sub_path = target_dir / sub.file
        if not sub_path.exists():
            msg = f"{mkv_path.name}: missing sub {sub_path.name}"
            _log(prefix, msg, level=logging.ERROR)
            with failures_lock:
                failures.append(msg)
            return 4
        if default_candidate is not None:
            candidate_sub_index = default_candidate.get('sub_index')
            default_value = '1' if default_candidate['source'] == 'external' and candidate_sub_index is not None and int(candidate_sub_index) == sub_index else '0'
            cmd.extend(["--default-track-flag", f"0:{default_value}"])
        cmd.extend([
            "--language", f"0:{sub.mkv_lang}",
            "--track-name", f"0:{sub.track_name}",
            str(sub_path),
        ])

    rc = run_mkvmerge_stream(
        cmd,
        prefix,
        stop_event,
        dry_run=dry_run,
        tmp_path=tmp_path,
        settings=settings,
    )
    if rc == 1:
        _log(prefix, f"⚠️ mkvmerge 以 warning 退出（exit=1），继续后续替换: {mkv_path.name}", level=logging.WARNING)
        rc = 0
    if rc not in (0, 99):
        if rc == 124:
            msg = f"{mkv_path.name}: idle timeout"
        elif rc == 137:
            msg = f"{mkv_path.name}: hard cap exceeded"
        else:
            msg = f"{mkv_path.name}: mkvmerge exit={rc}"
        _log(prefix, msg, level=logging.ERROR)
        with failures_lock:
            failures.append(msg)
        stop_event.set()
        terminate_other_jobs()
        return rc

    if dry_run:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except Exception:
                pass
        _log(prefix, f"✅ DRY OK: {mkv_path.name}")
        with processed_lock:
            processed_counter[0] += 1
            current_processed = processed_counter[0]
        if progress_callback is not None:
            try:
                progress_callback(MuxProgressEvent(
                    processed=current_processed,
                    total=total_items,
                    current_file=mkv_path.name,
                    ok=True,
                ))
            except Exception:
                logger.debug("更新 /ass DRY-RUN 进度回调失败", exc_info=True)
        return 0

    if stop_event.is_set():
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except Exception:
                pass
        _log(prefix, f"SKIP: stop_event 已触发，跳过替换 {mkv_path.name}", level=logging.WARNING)
        return 99

    try:
        os.replace(tmp_path, mkv_path)
        _log(prefix, f"✅ replaced: {mkv_path.name}")
    except OSError as exc:
        msg = f"{mkv_path.name}: replace failed ({exc})"
        _log(prefix, msg, level=logging.ERROR)
        with failures_lock:
            failures.append(msg)
        stop_event.set()
        terminate_other_jobs()
        return 5

    with processed_lock:
        processed_counter[0] += 1
        current_processed = processed_counter[0]
    if progress_callback is not None:
        try:
            progress_callback(MuxProgressEvent(
                processed=current_processed,
                total=total_items,
                current_file=mkv_path.name,
                ok=True,
            ))
        except Exception:
            logger.debug("更新 /ass 字幕内封进度回调失败", exc_info=True)
    return 0


def cleanup_external_subs_after_success(settings: AssMuxSettings, plan: MuxPlan | dict) -> int:
    normalized = _normalize_plan(plan)
    target_dir = settings.target_dir.expanduser().resolve()
    deleted = 0
    seen: set[str] = set()
    for item in normalized.items:
        for sub in item.subs:
            if sub.file in seen:
                continue
            seen.add(sub.file)
            sub_path = target_dir / sub.file
            if not sub_path.exists():
                continue
            try:
                sub_path.unlink()
                deleted += 1
                logger.info("🗑️ /ass 字幕内封批次完成后删除外挂字幕: %s", sub_path)
            except Exception as exc:
                logger.warning("⚠️ /ass 删除外挂字幕失败: %s (%s)", sub_path, exc)
    return deleted


def collect_mux_plan_stats(
    settings: AssMuxSettings,
    plan: MuxPlan | dict,
) -> dict[str, int | bool | str]:
    normalized = _normalize_plan(plan)
    target_dir = settings.target_dir.expanduser().resolve()
    tmp_dir = settings.tmp_dir.expanduser().resolve()
    probe_tmp_dir = _existing_probe_path(tmp_dir)

    total_size = 0
    max_size = 0
    missing_count = 0
    same_fs_count = 0
    sub_ref_counts: dict[str, int] = {}
    for item in normalized.items:
        mkv_path = target_dir / item.mkv
        size = safe_stat_size(mkv_path)
        total_size += size
        max_size = max(max_size, size)
        for sub in item.subs:
            sub_ref_counts[sub.file] = sub_ref_counts.get(sub.file, 0) + 1
        if mkv_path.exists():
            try:
                if same_filesystem(mkv_path, probe_tmp_dir):
                    same_fs_count += 1
            except Exception:
                pass
        else:
            missing_count += 1

    avg_size = total_size // max(1, len(normalized.items))
    est_tmp = int(max_size * max(1, settings.jobs) * 1.10)

    try:
        du = shutil.disk_usage(probe_tmp_dir)
        free_bytes = du.free
        total_bytes = du.total
    except Exception:
        free_bytes = 0
        total_bytes = 0

    return {
        "target_dir": str(target_dir),
        "tmp_dir": str(tmp_dir),
        "plan_path": str(settings.plan_path.expanduser().resolve()),
        "items": len(normalized.items),
        "tracks": normalized.total_sub_tracks,
        "total_source_size_bytes": total_size,
        "avg_source_size_bytes": avg_size,
        "max_source_size_bytes": max_size,
        "estimated_tmp_bytes": est_tmp,
        "tmp_free_bytes": free_bytes,
        "tmp_total_bytes": total_bytes,
        "temp_same_filesystem": same_fs_count == len(normalized.items) and len(normalized.items) > 0,
        "same_fs_count": same_fs_count,
        "missing_count": missing_count,
        "duplicate_subtitle_refs": sum(1 for count in sub_ref_counts.values() if count > 1),
    }


def run_mux_plan(
    settings: AssMuxSettings,
    plan: MuxPlan | dict,
    *,
    dry_run: bool,
    delete_external_subs: bool | None = None,
    progress_callback: Callable[[MuxProgressEvent], None] | None = None,
) -> MuxRunSummary:
    if not shutil.which(settings.mkvmerge_bin):
        raise AssPipelineError(f"未找到 mkvmerge: {settings.mkvmerge_bin}")

    normalized = _normalize_plan(plan)
    if not normalized.items:
        raise AssPipelineError("计划中没有可执行项")

    executable_items = [item for item in normalized.items if item.subs]
    if not executable_items:
        raise AssPipelineError("计划中没有选择任何字幕文件，请先为至少一个视频添加字幕")

    normalized = MuxPlan(
        generated_at=normalized.generated_at,
        target_dir=normalized.target_dir,
        defaults=normalized.defaults,
        items=executable_items,
        total_mkvs=normalized.total_mkvs,
        matched_mkvs=len(executable_items),
        total_sub_tracks=sum(len(item.subs) for item in executable_items),
    )

    target_dir = settings.target_dir.expanduser().resolve()
    if not target_dir.is_dir():
        raise AssPipelineError(f"ASS_MUX_TARGET_DIR 不存在: {target_dir}")

    if not normalized.items:
        raise AssPipelineError("计划中没有可执行项")

    delete_subs = settings.delete_external_subs_default if delete_external_subs is None else delete_external_subs
    ensure_dir(settings.tmp_dir.expanduser().resolve())
    ensure_dir(settings.plan_path.expanduser().resolve().parent)

    stats = collect_mux_plan_stats(settings, normalized)
    total_size = int(stats["total_source_size_bytes"])
    avg_size = int(stats["avg_source_size_bytes"])
    max_size = int(stats["max_source_size_bytes"])
    est_tmp = int(stats["estimated_tmp_bytes"])
    free_bytes = int(stats["tmp_free_bytes"])
    total_bytes = int(stats["tmp_total_bytes"])
    temp_same_fs = bool(stats["temp_same_filesystem"])
    same_fs_count = int(stats["same_fs_count"])
    missing_count = int(stats["missing_count"])
    duplicate_subtitle_refs = int(stats["duplicate_subtitle_refs"])

    logger.info("=== /ass 字幕内封执行摘要（开跑前）===")
    logger.info("计划文件: %s", settings.plan_path.expanduser().resolve())
    logger.info("工作目录: %s", target_dir)
    logger.info("临时目录: %s (jobs=%s)", settings.tmp_dir.expanduser().resolve(), settings.jobs)
    logger.info("将处理视频: %s", len(normalized.items))
    logger.info("总字幕轨道: %s", normalized.total_sub_tracks)
    logger.info("源视频总大小: %s", fmt_bytes(total_size))
    logger.info("单集大小: 平均 %s / 最大 %s", fmt_bytes(avg_size), fmt_bytes(max_size))
    logger.info("预计临时占用(粗估): %s", fmt_bytes(est_tmp))
    logger.info("临时目录与视频同分区: %s (%s/%s)", temp_same_fs, same_fs_count, len(normalized.items))
    if duplicate_subtitle_refs > 0:
        logger.warning("⚠️ 计划中有重复引用的字幕文件: %s 个", duplicate_subtitle_refs)
    if missing_count > 0:
        logger.warning("⚠️ 计划中缺失 MKV 数量: %s", missing_count)
    if total_bytes > 0:
        logger.info("临时分区剩余: %s / 总 %s", fmt_bytes(free_bytes), fmt_bytes(total_bytes))
        if free_bytes and est_tmp and free_bytes < est_tmp:
            logger.warning("⚠️ 剩余空间可能不足以并发输出，建议调小 ASS_MUX_JOBS 或修改 ASS_MUX_TMP_DIR")
    logger.info("dry-run: %s", "y" if dry_run else "n")
    logger.info("delete_external_subs: %s", delete_subs)
    logger.info("====================================")

    stop_event = threading.Event()
    failures: list[str] = []
    failures_lock = threading.Lock()
    processed_counter = [0]
    processed_lock = threading.Lock()
    queue: Queue[MuxPlanItem] = Queue()
    for item in normalized.items:
        queue.put(item)

    def worker() -> None:
        while not stop_event.is_set():
            try:
                item = queue.get_nowait()
            except Empty:
                return
            rc = process_one_item(
                item,
                settings,
                target_dir,
                stop_event,
                failures,
                failures_lock,
                processed_counter,
                processed_lock,
                total_items=len(normalized.items),
                progress_callback=progress_callback,
                dry_run=dry_run,
                delete_external_subs=delete_subs,
            )
            queue.task_done()
            if rc not in (0, 99):
                return

    started = time.monotonic()
    threads = [threading.Thread(target=worker, daemon=True) for _ in range(max(1, settings.jobs))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    duration = time.monotonic() - started
    failed = len(failures)
    processed = processed_counter[0]

    deleted_external_subs_count = 0
    if not failures and delete_subs and not dry_run:
        deleted_external_subs_count = cleanup_external_subs_after_success(settings, normalized)

    logger.info("/ass 字幕内封结束: processed=%s failed=%s duration=%.1fs dry_run=%s", processed, failed, duration, dry_run)
    if failures:
        for item in failures:
            logger.error("/ass 字幕内封失败项: %s", item)

    return MuxRunSummary(
        target_dir=str(target_dir),
        tmp_dir=str(settings.tmp_dir.expanduser().resolve()),
        plan_path=str(settings.plan_path.expanduser().resolve()),
        total_mkvs=normalized.total_mkvs,
        matched_mkvs=normalized.matched_mkvs,
        total_sub_tracks=normalized.total_sub_tracks,
        processed=processed,
        failed=failed,
        dry_run=dry_run,
        delete_external_subs=delete_subs,
        jobs=max(1, settings.jobs),
        total_source_size_bytes=total_size,
        avg_source_size_bytes=avg_size,
        max_source_size_bytes=max_size,
        estimated_tmp_bytes=est_tmp,
        tmp_free_bytes=free_bytes,
        tmp_total_bytes=total_bytes,
        temp_same_filesystem=temp_same_fs,
        duplicate_subtitle_refs=duplicate_subtitle_refs,
        deleted_external_subs_count=deleted_external_subs_count,
        duration_s=duration,
        failures=failures,
    )
