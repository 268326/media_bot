"""
STRM 后台服务封装
"""
from __future__ import annotations

import asyncio
import logging
import threading

from config import STRM_SETTINGS
from strm_watcher import StrmWatcher


class StrmService:
    def __init__(self):
        self.settings = STRM_SETTINGS
        self.watcher: StrmWatcher | None = None
        self.started = False
        self.last_error: str = ""
        self.lock = threading.Lock()
        self.scan_lock = threading.Lock()

    async def start(self):
        if not self.settings.enabled:
            logging.info("ℹ️ STRM watcher 未启用（STRM_WATCH_ENABLED=0）")
            return

        with self.lock:
            if self.started:
                return
            self.watcher = StrmWatcher(self.settings)
            watcher = self.watcher

        try:
            await asyncio.to_thread(watcher.start)
        except Exception as exc:
            with self.lock:
                self.last_error = str(exc)
                self.watcher = None
                self.started = False
            logging.exception("❌ STRM watcher 启动失败，已跳过，不影响 Bot 主功能")
            return

        with self.lock:
            self.last_error = ""
            self.started = True
        logging.info("✅ STRM watcher 服务已启动")

    async def stop(self):
        with self.lock:
            if not self.started:
                return
            watcher = self.watcher
            self.watcher = None
            self.started = False

        if watcher:
            await asyncio.to_thread(watcher.stop)

    async def scan(self) -> dict:
        with self.lock:
            watcher = self.watcher
            enabled = self.settings.enabled
            started = self.started

        if not enabled:
            return {"ok": False, "message": "STRM watcher 未启用（STRM_WATCH_ENABLED=0）"}
        if not started or not watcher:
            return {"ok": False, "message": "STRM watcher 未启动，无法执行重扫"}

        acquired = self.scan_lock.acquire(blocking=False)
        if not acquired:
            return {"ok": False, "message": "已有 STRM 重扫任务在执行，请稍后再试"}

        try:
            await asyncio.to_thread(watcher.scan_existing_and_submit)
            return {"ok": True, "message": f"已触发 STRM 重扫: {self.settings.watch_dir}"}
        except Exception as exc:
            with self.lock:
                self.last_error = str(exc)
            logging.exception("❌ STRM 手动重扫失败")
            return {"ok": False, "message": f"STRM 重扫失败: {exc}"}
        finally:
            self.scan_lock.release()

    def status(self) -> dict:
        with self.lock:
            watcher = self.watcher
            return {
                "enabled": self.settings.enabled,
                "started": self.started,
                "running": bool(watcher and watcher.is_running()),
                "watch_dir": self.settings.watch_dir,
                "done_dir": self.settings.done_dir,
                "failed_dir": self.settings.failed_dir,
                "last_error": self.last_error,
            }


strm_service = StrmService()
