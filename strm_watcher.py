"""
STRM 监控核心模块
"""
from __future__ import annotations

import logging
import os
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

    def folder_key_for(self, path: Path) -> str | None:
        try:
            rel = path.relative_to(self.watch_dir)
        except Exception:
            return None

        if self.settings.only_first_level_dir:
            if len(rel.parts) < 2:
                return None
            return rel.parts[0]
        return rel.parts[0] if rel.parts else ""

    def touch(self, folder_key: str):
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

    def mark_finished(self, p: Path):
        key = str(p)
        now = time.time()
        with self.lock:
            self.inflight_paths.discard(key)
            self.recent_done[key] = now

    def can_finalize(self, st: FolderState, now: float) -> bool:
        if st.active_jobs != 0:
            return False
        if (now - st.last_activity) < self.settings.idle_seconds:
            return False
        if (now - st.first_seen) < self.settings.min_folder_age_seconds:
            return False
        return True

    def job_started(self, folder_key: str):
        with self.lock:
            st = self.folders.get(folder_key)
            if not st:
                st = FolderState(rel_folder=folder_key)
                self.folders[folder_key] = st
            st.active_jobs += 1
            st.last_activity = time.time()

    def job_finished(self, folder_key: str, ok: bool):
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

    def move_failed_strm(self, p: Path) -> None:
        if not p.exists():
            return
        rel = p.relative_to(self.watch_dir)
        dst = self.failed_dir / rel
        moved_to = safe_move(p, dst)
        logging.info("MOVED_FAILED\n  SRC: %s\n  DST: %s", p, moved_to)

    def move_done_folder(self, folder_key: str) -> None:
        src = self.watch_dir / folder_key
        if not src.exists():
            return
        if not src.is_dir():
            logging.warning("SKIP move_done_not_dir: %s", src)
            return

        # 二次确认：移动前再次扫描目录，若仍有 .strm 留存则暂不移动
        remain = list(src.rglob("*.strm"))
        if remain:
            self.coord.touch(folder_key)
            logging.info("DEFER_DONE_FOLDER remaining_strm=%s folder=%s", len(remain), src)
            return

        dst = self.done_dir / folder_key
        moved_to = safe_move(src, dst)
        logging.info("MOVED_DONE_FOLDER\n  SRC: %s\n  DST: %s", src, moved_to)

    def process_strm_file(self, p: Path) -> bool:
        if not p.exists():
            return True

        url = read_strm_url(p)
        if not url:
            logging.warning("FAIL invalid_strm_url: %s", p)
            return False

        data = run_ffprobe(url, self.settings)
        if not data:
            logging.warning("FAIL ffprobe_failed: %s", p)
            return False

        info = parse_media_info(data)
        new_name = generate_new_name(p.name, info)

        if new_name == p.name:
            logging.info("SKIP already_ok: %s", p)
            return True

        dst = p.parent / new_name
        if dst.exists():
            logging.warning("FAIL name_conflict: %s -> %s", p, dst)
            return False

        try:
            os.rename(p, dst)
            logging.info("RENAMED\n  OLD: %s\n  NEW: %s", p.name, new_name)
            return True
        except Exception as exc:
            logging.warning("FAIL rename_error: %s (%s)", p, exc)
            return False

    def submit_one(self, p: Path, folder_key: str):
        if not self.executor:
            raise RuntimeError("executor not started")
        if not self.coord.mark_submitted(p):
            logging.debug("SKIP duplicate_submit: %s", p)
            return

        self.coord.job_started(folder_key)

        def _run():
            ok = False
            try:
                ok = self.process_strm_file(p)
                if not ok:
                    try:
                        self.move_failed_strm(p)
                    except Exception as exc:
                        logging.warning("MOVE_FAILED_STRM error: %s (%s)", p, exc)
                return ok
            finally:
                self.coord.mark_finished(p)
                self.coord.job_finished(folder_key, ok=ok)

        self.executor.submit(_run)

    def scan_existing_and_submit(self):
        for p in self.watch_dir.rglob("*.strm"):
            folder_key = self.coord.folder_key_for(p)
            if folder_key is None:
                continue
            self.coord.touch(folder_key)
            self.submit_one(p, folder_key)

    def finalize_loop(self):
        while not self.stop_evt.is_set():
            now = time.time()
            for st in self.coord.snapshot():
                if self.coord.can_finalize(st, now):
                    try:
                        self.move_done_folder(st.rel_folder)
                    except Exception as exc:
                        logging.warning("MOVE_DONE_FOLDER failed for %s: %s", st.rel_folder, exc)
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
            if folder_key is None:
                continue

            self.coord.touch(folder_key)
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
