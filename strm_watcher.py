"""
STRM 监控核心模块
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from strm_config import StrmSettings
from strm_naming import generate_new_name, parse_media_info
from strm_probe import read_strm_url, run_ffprobe
from strm_notifier import strm_notifier

SUBTITLE_EXTENSIONS = (".ass", ".srt", ".sup")


@dataclass
class FolderState:
    rel_folder: str
    first_seen: float = field(default_factory=lambda: time.time())
    last_activity: float = field(default_factory=lambda: time.time())
    active_jobs: int = 0
    fail_count: int = 0


class Coordinator:
    def __init__(self, settings: StrmSettings):
        self.settings = settings
        self.lock = threading.Lock()
        self.folders: dict[str, FolderState] = {}
        self.inflight_paths: set[str] = set()
        self.recent_done: dict[str, float] = {}
        self.watch_dir = Path(settings.watch_dir)

    def mark_alias_inflight(self, p: Path):
        with self.lock:
            self.inflight_paths.add(str(p))

    def folder_key_for(self, path: Path) -> str | None:
        try:
            rel = path.relative_to(self.watch_dir)
        except Exception:
            return None

        if self.settings.only_first_level_dir:
            if len(rel.parts) < 2:
                return None
            return rel.parts[0]

        # only_first_level_dir=0 时：
        # - 根目录下直接出现的 .strm 允许处理，但不作为“可移动目录批次”纳入 finalize
        # - 子目录中的 .strm 仍按一级目录批次归组
        if len(rel.parts) < 2:
            return None
        return rel.parts[0]

    def touch(self, folder_key: str | None):
        if folder_key is None:
            return
        with self.lock:
            st = self.folders.get(folder_key)
            if not st:
                st = FolderState(rel_folder=folder_key)
                self.folders[folder_key] = st
            st.last_activity = time.time()

    def mark_submitted(self, p: Path) -> bool:
        key = str(p)
        now = time.time()
        with self.lock:
            expired = [k for k, ts in self.recent_done.items() if (now - ts) >= self.settings.recent_event_ttl]
            for k in expired:
                self.recent_done.pop(k, None)

            if key in self.inflight_paths:
                return False

            last_done = self.recent_done.get(key)
            if last_done is not None and (now - last_done) < self.settings.recent_event_ttl:
                return False

            self.inflight_paths.add(key)
            return True

    def mark_finished(self, p: Path, alias_paths: list[Path] | None = None):
        now = time.time()
        with self.lock:
            self.inflight_paths.discard(str(p))
            self.recent_done[str(p)] = now
            for ap in alias_paths or []:
                self.inflight_paths.discard(str(ap))
                self.recent_done[str(ap)] = now

    def can_finalize(self, st: FolderState, now: float) -> bool:
        if st.active_jobs != 0:
            return False
        if (now - st.last_activity) < self.settings.idle_seconds:
            return False
        if (now - st.first_seen) < self.settings.min_folder_age_seconds:
            return False
        return True

    def job_started(self, folder_key: str | None):
        if folder_key is None:
            return
        with self.lock:
            st = self.folders.get(folder_key)
            if not st:
                st = FolderState(rel_folder=folder_key)
                self.folders[folder_key] = st
            st.active_jobs += 1
            st.last_activity = time.time()

    def job_finished(self, folder_key: str | None, ok: bool):
        if folder_key is None:
            return
        with self.lock:
            st = self.folders.get(folder_key)
            if not st:
                return
            st.active_jobs = max(0, st.active_jobs - 1)
            st.last_activity = time.time()
            if not ok:
                st.fail_count += 1

    def snapshot(self) -> list[FolderState]:
        with self.lock:
            return [FolderState(**st.__dict__) for st in self.folders.values()]

    def remove(self, folder_key: str):
        with self.lock:
            self.folders.pop(folder_key, None)


def ensure_parent(p: Path):
    p.parent.mkdir(parents=True, exist_ok=True)


def is_subpath(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except Exception:
        return False


def safe_move(src: Path, dst: Path) -> Path:
    if dst.exists():
        ts = time.strftime("%Y%m%d-%H%M%S")
        if dst.is_dir() or src.is_dir():
            dst = dst.with_name(dst.name + f"__{ts}")
        else:
            dst = dst.with_name(f"{dst.stem}__{ts}{dst.suffix}")
    ensure_parent(dst)
    shutil.move(str(src), str(dst))
    return dst


@dataclass
class ProcessOutcome:
    ok: bool
    final_path: Path | None
    subtitle_aliases: list[Path] = field(default_factory=list)
    renamed: bool = False
    already_ok: bool = False
    reason: str = ""


class StrmWatcher:
    def __init__(self, settings: StrmSettings):
        self.settings = settings
        self.watch_dir = Path(settings.watch_dir)
        self.done_dir = Path(settings.done_dir)
        self.failed_dir = Path(settings.failed_dir)
        self.coord = Coordinator(settings)
        self.stop_evt = threading.Event()
        self.executor: ThreadPoolExecutor | None = None
        self.finalizer_thread: threading.Thread | None = None
        self.watcher_thread: threading.Thread | None = None

    def is_running(self) -> bool:
        watcher_alive = bool(self.watcher_thread and self.watcher_thread.is_alive())
        finalizer_alive = bool(self.finalizer_thread and self.finalizer_thread.is_alive())
        return watcher_alive and finalizer_alive and not self.stop_evt.is_set()

    def validate(self):
        if not os.path.exists(self.settings.ffprobe_path):
            raise RuntimeError(f"ffprobe not found: {self.settings.ffprobe_path}")
        if not self.watch_dir.is_dir():
            raise RuntimeError(f"WATCH_DIR not found: {self.watch_dir}")
        if is_subpath(self.done_dir, self.watch_dir) or is_subpath(self.failed_dir, self.watch_dir):
            raise RuntimeError("DONE_DIR/FAILED_DIR must NOT be inside WATCH_DIR (to avoid infinite loop)")
        self.done_dir.mkdir(parents=True, exist_ok=True)
        self.failed_dir.mkdir(parents=True, exist_ok=True)

    def iter_sidecar_subtitles(self, strm_path: Path) -> list[Path]:
        """查找同目录下与 strm 严格对应的字幕文件。

        允许：
        - xxx.ass
        - xxx.zh.ass
        - xxx.chs.default.srt

        不匹配仅“前缀相似”但不真正属于该 strm 的文件。
        """
        results: list[Path] = []
        stem = strm_path.stem
        pattern = re.compile(
            rf"^{re.escape(stem)}(?:\.[^.]+)*\.(?:ass|srt|sup)$",
            re.IGNORECASE,
        )

        for child in strm_path.parent.iterdir():
            if not child.is_file():
                continue
            if child.suffix.lower() not in SUBTITLE_EXTENSIONS:
                continue
            if pattern.match(child.name):
                results.append(child)

        results.sort(key=lambda x: x.name)
        return results

    def move_sidecar_subtitles(self, old_strm: Path, new_strm: Path) -> list[Path]:
        """同步移动同目录下与 strm 同基名的字幕文件。"""
        moved: list[Path] = []
        old_stem = old_strm.stem
        new_stem = new_strm.stem

        for src in self.iter_sidecar_subtitles(old_strm):
            suffix_part = src.name[len(old_stem):]
            dst = new_strm.with_name(new_stem + suffix_part)
            try:
                moved_to = safe_move(src, dst)
                self.coord.mark_alias_inflight(moved_to)
                moved.append(moved_to)
                logging.info("📎 字幕已移动\n   ├─ 源文件: %s\n   └─ 目标:   %s", src, moved_to)
            except Exception as exc:
                logging.warning("FAIL subtitle_move_error: %s -> %s (%s)", src, dst, exc)

        return moved

    def move_failed_strm(self, p: Path) -> tuple[Path | None, list[Path]]:
        if not p.exists():
            return None, []
        rel = p.relative_to(self.watch_dir)
        dst = self.failed_dir / rel
        moved_to = safe_move(p, dst)
        moved_subtitles = self.move_sidecar_subtitles(p, moved_to)
        logging.warning("❌ STRM 处理失败并已转移\n   ├─ 源文件: %s\n   ├─ 目标:   %s\n   └─ 字幕数: %s", p, moved_to, len(moved_subtitles))
        return moved_to, moved_subtitles

    def move_done_file(self, p: Path) -> tuple[Path | None, list[Path]]:
        if not p.exists():
            return None, []
        rel = p.relative_to(self.watch_dir)
        dst = self.done_dir / rel.name
        moved_to = safe_move(p, dst)
        moved_subtitles = self.move_sidecar_subtitles(p, moved_to)
        logging.info("✅ STRM 文件已归档\n   ├─ 源文件: %s\n   ├─ 目标:   %s\n   └─ 字幕数: %s", p, moved_to, len(moved_subtitles))
        return moved_to, moved_subtitles

    def move_done_folder(self, folder_key: str) -> None:
        src = self.watch_dir / folder_key
        if not src.exists():
            logging.warning("⚠️ STRM 目录归档跳过\n   ├─ 目录:   %s\n   └─ 原因:   source_missing", src)
            strm_notifier.record_folder_failed(folder_key, src, self.done_dir / folder_key, reason="source_missing")
            return
        if not src.is_dir():
            logging.warning("⚠️ STRM 目录归档跳过\n   ├─ 路径:   %s\n   └─ 原因:   not_a_directory", src)
            strm_notifier.record_folder_failed(folder_key, src, self.done_dir / folder_key, reason="not_a_directory")
            return

        dst = self.done_dir / folder_key
        moved_to = safe_move(src, dst)
        strm_count = sum(1 for _ in moved_to.rglob("*.strm"))
        subtitle_count = sum(1 for _ in moved_to.rglob("*.ass"))
        subtitle_count += sum(1 for _ in moved_to.rglob("*.srt"))
        subtitle_count += sum(1 for _ in moved_to.rglob("*.sup"))
        logging.info(
            "📦 STRM 目录已归档\n"
            "   ├─ 批次:   %s\n"
            "   ├─ 目录:   %s\n"
            "   ├─ 目标:   %s\n"
            "   ├─ STRM:  %s\n"
            "   └─ 字幕:  %s",
            folder_key,
            src,
            moved_to,
            strm_count,
            subtitle_count,
        )
        strm_notifier.record_folder_completed(folder_key, src, moved_to)

    def rename_sidecar_subtitles(self, old_strm: Path, new_strm: Path) -> list[Path]:
        """同步重命名同目录下与旧 strm 同基名的字幕文件。"""
        renamed: list[Path] = []
        old_stem = old_strm.stem
        new_stem = new_strm.stem

        for src in self.iter_sidecar_subtitles(old_strm):
            suffix_part = src.name[len(old_stem):]
            dst = new_strm.with_name(new_stem + suffix_part)
            if dst.exists():
                logging.warning("SKIP subtitle_name_conflict: %s -> %s", src, dst)
                continue
            try:
                os.rename(src, dst)
                self.coord.mark_alias_inflight(dst)
                renamed.append(dst)
                logging.info("📝 字幕已重命名\n   ├─ 旧名: %s\n   └─ 新名: %s", src.name, dst.name)
            except Exception as exc:
                logging.warning("FAIL subtitle_rename_error: %s -> %s (%s)", src, dst, exc)

        return renamed

    def process_strm_file(self, p: Path) -> ProcessOutcome:
        if not p.exists():
            return ProcessOutcome(ok=True, final_path=p, already_ok=True)

        url = read_strm_url(p)
        if not url:
            logging.warning("FAIL invalid_strm_url: %s", p)
            return ProcessOutcome(ok=False, final_path=p, reason="invalid_strm_url")

        data = run_ffprobe(url, self.settings)
        if not data:
            logging.warning("FAIL ffprobe_failed: %s", p)
            return ProcessOutcome(ok=False, final_path=p, reason="ffprobe_failed")

        info = parse_media_info(data)
        new_name = generate_new_name(p.name, info)

        if new_name == p.name:
            logging.debug("SKIP already_ok: %s", p)
            return ProcessOutcome(ok=True, final_path=p, already_ok=True)

        dst = p.parent / new_name
        if dst.exists():
            logging.warning("FAIL name_conflict: %s -> %s", p, dst)
            return ProcessOutcome(ok=False, final_path=p, reason="name_conflict")

        try:
            os.rename(p, dst)
            self.coord.mark_alias_inflight(dst)
            subtitle_aliases = self.rename_sidecar_subtitles(p, dst)
            logging.info("🎬 STRM 已重命名\n   ├─ 旧名: %s\n   ├─ 新名: %s\n   └─ 字幕: %s", p.name, new_name, len(subtitle_aliases))
            return ProcessOutcome(
                ok=True,
                final_path=dst,
                subtitle_aliases=subtitle_aliases,
                renamed=True,
            )
        except Exception as exc:
            logging.warning("FAIL rename_error: %s (%s)", p, exc)
            return ProcessOutcome(ok=False, final_path=p, reason=f"rename_error: {exc}")

    def submit_one(self, p: Path, folder_key: str | None) -> bool:
        if not self.executor:
            raise RuntimeError("executor not started")
        if not self.coord.mark_submitted(p):
            logging.debug("SKIP duplicate_submit: %s", p)
            return False

        self.coord.touch(folder_key)
        self.coord.job_started(folder_key)

        def _run():
            ok = False
            final_path: Path | None = p
            subtitle_aliases: list[Path] = []
            final_move_paths: list[Path] = []
            renamed = False
            already_ok = False
            reason = ""
            try:
                outcome = self.process_strm_file(p)
                ok = outcome.ok
                final_path = outcome.final_path
                subtitle_aliases = outcome.subtitle_aliases
                renamed = outcome.renamed
                already_ok = outcome.already_ok
                reason = outcome.reason
                strm_notifier.record_process_result(
                    folder_key,
                    final_path or p,
                    source_name=p.name,
                    target_name=(final_path.name if final_path else p.name),
                    ok=ok,
                    renamed=renamed,
                    already_ok=already_ok,
                    subtitle_count=len(subtitle_aliases),
                    reason=reason,
                )

                if ok and folder_key is None and final_path and final_path.exists():
                    try:
                        moved_strm, moved_subtitles = self.move_done_file(final_path)
                        if moved_strm:
                            final_move_paths.append(moved_strm)
                            strm_notifier.record_root_completed(
                                final_path,
                                moved_strm,
                                ok=True,
                                subtitle_count=len(moved_subtitles) + len(subtitle_aliases),
                            )
                        final_move_paths.extend(moved_subtitles)
                    except Exception as exc:
                        reason = f"move_done_file_error: {exc}"
                        logging.warning(
                            "❌ STRM 单文件归档失败\n"
                            "   ├─ 文件:   %s\n"
                            "   ├─ 目标:   %s\n"
                            "   └─ 原因:   %s",
                            final_path,
                            self.done_dir / final_path.name,
                            exc,
                        )
                        strm_notifier.record_root_completed(
                            final_path,
                            final_path,
                            ok=False,
                            subtitle_count=len(subtitle_aliases),
                            reason=reason,
                        )
                elif not ok:
                    try:
                        moved_strm, moved_subtitles = self.move_failed_strm(final_path or p)
                        if moved_strm:
                            final_move_paths.append(moved_strm)
                            strm_notifier.record_root_completed(
                                final_path or p,
                                moved_strm,
                                ok=False,
                                subtitle_count=len(moved_subtitles) + len(subtitle_aliases),
                                reason=reason,
                            )
                        final_move_paths.extend(moved_subtitles)
                    except Exception as exc:
                        move_reason = f"move_failed_strm_error: {exc}"
                        failed_src = final_path or p
                        logging.warning(
                            "❌ STRM 失败文件转移失败\n"
                            "   ├─ 文件:   %s\n"
                            "   ├─ 目标:   %s\n"
                            "   └─ 原因:   %s",
                            failed_src,
                            self.failed_dir / failed_src.relative_to(self.watch_dir),
                            exc,
                        )
                        strm_notifier.record_root_completed(
                            failed_src,
                            failed_src,
                            ok=False,
                            subtitle_count=len(subtitle_aliases),
                            reason=(reason + " | " + move_reason).strip(" |"),
                        )
                return ok
            finally:
                alias_paths = []
                if final_path and final_path != p:
                    alias_paths.append(final_path)
                alias_paths.extend(subtitle_aliases)
                alias_paths.extend(final_move_paths)
                self.coord.mark_finished(p, alias_paths=alias_paths)
                self.coord.job_finished(folder_key, ok=ok)

        self.executor.submit(_run)
        return True

    def scan_existing_and_submit(self):
        for p in self.watch_dir.rglob("*.strm"):
            folder_key = self.coord.folder_key_for(p)
            self.submit_one(p, folder_key)

    def finalize_loop(self):
        while not self.stop_evt.is_set():
            now = time.time()
            for st in self.coord.snapshot():
                if self.coord.can_finalize(st, now):
                    try:
                        self.move_done_folder(st.rel_folder)
                    except Exception as exc:
                        src = self.watch_dir / st.rel_folder
                        dst = self.done_dir / st.rel_folder
                        reason = f"move_done_folder_error: {exc}"
                        logging.warning(
                            "❌ STRM 目录归档失败\n"
                            "   ├─ 批次:   %s\n"
                            "   ├─ 目录:   %s\n"
                            "   ├─ 目标:   %s\n"
                            "   └─ 原因:   %s",
                            st.rel_folder,
                            src,
                            dst,
                            exc,
                        )
                        strm_notifier.record_folder_failed(st.rel_folder, src, dst, reason=reason)
                    finally:
                        self.coord.remove(st.rel_folder)
            self.stop_evt.wait(2)

    def run_inotify_once(self) -> int:
        cmd = [
            "inotifywait",
            "-m",
            "-r",
            "-e",
            "close_write,moved_to",
            "--format",
            "%w%f",
            str(self.watch_dir),
        ]
        logging.info("Starting inotify: %s", " ".join(cmd))

        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

        def _stderr_drain():
            assert proc.stderr is not None
            for line in proc.stderr:
                line = line.strip()
                if line:
                    logging.debug("inotify: %s", line)

        t = threading.Thread(target=_stderr_drain, daemon=True)
        t.start()

        assert proc.stdout is not None
        for line in proc.stdout:
            if self.stop_evt.is_set():
                proc.terminate()
                break

            path_s = line.strip()
            if not path_s.endswith(".strm"):
                continue

            fp = Path(path_s)
            folder_key = self.coord.folder_key_for(fp)
            self.submit_one(fp, folder_key)

        return proc.wait()

    def watch_loop(self):
        backoff_s = 2
        while not self.stop_evt.is_set():
            rc = self.run_inotify_once()
            if self.stop_evt.is_set():
                break
            if rc == 0:
                logging.warning("inotify exited normally, restarting in %ss", backoff_s)
            else:
                logging.warning("inotify exited with rc=%s, restarting in %ss", rc, backoff_s)
            if self.stop_evt.wait(backoff_s):
                break
            backoff_s = min(backoff_s * 2, 30)

    def start(self):
        self.validate()
        self.executor = ThreadPoolExecutor(max_workers=self.settings.max_workers, thread_name_prefix="strm")
        self.scan_existing_and_submit()
        self.finalizer_thread = threading.Thread(target=self.finalize_loop, daemon=True, name="strm-finalizer")
        self.finalizer_thread.start()
        self.watcher_thread = threading.Thread(target=self.watch_loop, daemon=True, name="strm-watcher")
        self.watcher_thread.start()
        logging.info("✅ STRM watcher started: watch_dir=%s", self.watch_dir)

    def stop(self):
        self.stop_evt.set()
        if self.executor:
            self.executor.shutdown(wait=False, cancel_futures=False)
            self.executor = None
        logging.info("🛑 STRM watcher stop requested")
