"""
HDHive 解锁队列服务
统一封装 /resources/unlock 调用，提供排队与速率限制。
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from threading import Lock
from typing import Any, Awaitable, Callable

# 注意：这里假设你在 config 中把变量名也改为了 PER_MINUTE
from config import HDHIVE_UNLOCK_RATE_LIMIT_PER_MINUTE
from hdhive_auth import build_authenticated_session, request_open_api_json

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class UnlockQueueNotice:
    resource_id: str
    queue_position: int
    ahead_count: int
    wait_seconds: float
    queued_seconds: float
    rate_limit_per_minute: int  # 变量名改为 per_minute
    user_id: int | None = None


UnlockWaitCallback = Callable[[UnlockQueueNotice], Awaitable[None]]


@dataclass(slots=True)
class UnlockJob:
    resource_id: str
    future: asyncio.Future
    sequence: int
    user_id: int | None = None
    wait_callback: UnlockWaitCallback | None = None
    created_at: float = field(default_factory=time.monotonic)
    queue_position: int = 1
    wait_notified: bool = False
    wait_logged: bool = False
    last_notice_at: float = 0.0


def _request_unlock_sync(resource_id: str) -> dict[str, Any]:
    with build_authenticated_session() as session:
        payload = request_open_api_json(
            session,
            "POST",
            "/resources/unlock",
            json={"slug": resource_id},
        )

    data = payload.get("data") or {}
    if not isinstance(data, dict):
        raise RuntimeError("解锁接口返回异常")
    return data


class HDHiveUnlockService:
    def __init__(self):
        self.rate_limit_per_minute = HDHIVE_UNLOCK_RATE_LIMIT_PER_MINUTE  # 改为 per_minute
        # worker 数量可以保持原逻辑，或者你觉得 60/分 不需要太多 worker，也可以手动限制，比如 max(1, min(10, self.rate_limit_per_minute))
        self.worker_count = max(1, self.rate_limit_per_minute)
        self.queue: asyncio.Queue[UnlockJob] | None = None
        self.worker_tasks: list[asyncio.Task] = []
        self.slot_lock: asyncio.Lock | None = None
        self.timestamps: deque[float] = deque()
        self.started = False
        self.processing_count = 0
        self.state_lock = Lock()
        self.sequence_counter = 0
        self.active_sequences: set[int] = set()

    async def start(self):
        with self.state_lock:
            if self.started:
                return
            self.queue = asyncio.Queue()
            self.slot_lock = asyncio.Lock()
            self.timestamps.clear()
            self.sequence_counter = 0
            self.active_sequences.clear()
            self.worker_tasks = [
                asyncio.create_task(self._worker_loop(index + 1))
                for index in range(self.worker_count)
            ]
            self.processing_count = 0
            self.started = True

        logger.info(
            "✅ HDHive 解锁队列已启动: rate_limit=%s 次/分 workers=%s",  # 日志更新
            self.rate_limit_per_minute,
            self.worker_count,
        )

    async def stop(self):
        with self.state_lock:
            if not self.started:
                return
            queue = self.queue
            worker_tasks = list(self.worker_tasks)
            self.queue = None
            self.worker_tasks = []
            self.slot_lock = None
            self.started = False
            self.processing_count = 0

        for task in worker_tasks:
            task.cancel()
        if worker_tasks:
            results = await asyncio.gather(*worker_tasks, return_exceptions=True)
            for result in results:
                if isinstance(result, Exception) and not isinstance(result, asyncio.CancelledError):
                    logger.debug("HDHive 解锁 worker 停止时返回异常: %s", result)

        if queue:
            while True:
                try:
                    job = queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                if not job.future.done():
                    job.future.set_exception(RuntimeError("HDHive 解锁队列已停止"))
                queue.task_done()

        self.timestamps.clear()
        self.active_sequences.clear()
        self.sequence_counter = 0
        logger.info("🛑 HDHive 解锁队列已停止")

    async def unlock(
        self,
        resource_id: str,
        *,
        user_id: int | None = None,
        wait_callback: UnlockWaitCallback | None = None,
    ) -> dict[str, Any]:
        await self.start()

        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()

        with self.state_lock:
            queue = self.queue
            if queue is None:
                raise RuntimeError("HDHive 解锁队列未初始化")

            self.sequence_counter += 1
            sequence = self.sequence_counter
            self.active_sequences.add(sequence)
            queue_position = len(self.active_sequences)

            job = UnlockJob(
                resource_id=resource_id,
                future=future,
                sequence=sequence,
                user_id=user_id,
                wait_callback=wait_callback,
                queue_position=queue_position,
            )
            queue.put_nowait(job)

        if queue_position > 1:
            logger.info(
                "📥 HDHive 解锁请求已入队: resource=%s position=%s user=%s",
                resource_id,
                queue_position,
                user_id or "-",
            )

        return await future

    def _compute_live_position_unlocked(self, sequence: int) -> int:
        if sequence not in self.active_sequences:
            return 1
        ahead_count = sum(1 for seq in self.active_sequences if seq < sequence)
        return ahead_count + 1

    def _compute_live_position(self, sequence: int) -> int:
        with self.state_lock:
            return self._compute_live_position_unlocked(sequence)

    async def _worker_loop(self, worker_id: int):
        while True:
            queue = self.queue
            if queue is None:
                return

            job = await queue.get()
            with self.state_lock:
                self.processing_count += 1

            try:
                await self._acquire_slot(job)
                queued_seconds = max(0.0, time.monotonic() - job.created_at)
                logger.info(
                    "🔓 HDHive 解锁开始: worker=%s resource=%s user=%s queued=%.2fs",
                    worker_id,
                    job.resource_id,
                    job.user_id or "-",
                    queued_seconds,
                )
                result = await asyncio.to_thread(_request_unlock_sync, job.resource_id)
                if not job.future.done():
                    job.future.set_result(result)
                logger.info(
                    "✅ HDHive 解锁完成: worker=%s resource=%s user=%s",
                    worker_id,
                    job.resource_id,
                    job.user_id or "-",
                )
            except asyncio.CancelledError:
                if not job.future.done():
                    job.future.set_exception(RuntimeError("HDHive 解锁队列已停止"))
                raise
            except Exception as exc:
                logger.warning(
                    "❌ HDHive 解锁失败: worker=%s resource=%s user=%s error=%s",
                    worker_id,
                    job.resource_id,
                    job.user_id or "-",
                    exc,
                )
                if not job.future.done():
                    job.future.set_exception(exc)
            finally:
                with self.state_lock:
                    self.processing_count = max(0, self.processing_count - 1)
                    self.active_sequences.discard(job.sequence)
                queue.task_done()

    async def _acquire_slot(self, job: UnlockJob):
        while True:
            lock = self.slot_lock
            if lock is None:
                raise RuntimeError("HDHive 解锁队列未运行")

            async with lock:
                now = time.monotonic()
                # 核心修改：时间窗口改为 60.0 秒
                while self.timestamps and now - self.timestamps[0] >= 60.0:
                    self.timestamps.popleft()

                # 判断一分钟内的请求数
                if len(self.timestamps) < self.rate_limit_per_minute:
                    self.timestamps.append(now)
                    return

                # 核心修改：等待时间基于 60 秒计算
                wait_seconds = max(0.0, 60.0 - (now - self.timestamps[0]))
                queued_seconds = max(0.0, now - job.created_at)
                with self.state_lock:
                    live_position = self._compute_live_position_unlocked(job.sequence)
                ahead_count = max(0, live_position - 1)

            if not job.wait_logged:
                logger.warning(
                    "⏳ HDHive 解锁触发限速，排队等待: resource=%s position=%s ahead=%s wait=%.2fs queued=%.2fs rate_limit=%s/分 user=%s", # 日志更新
                    job.resource_id,
                    live_position,
                    ahead_count,
                    wait_seconds,
                    queued_seconds,
                    self.rate_limit_per_minute,
                    job.user_id or "-",
                )
                job.wait_logged = True

            should_notify = (
                job.wait_callback is not None
                and (
                    not job.wait_notified
                    or (now - job.last_notice_at) >= 1.0  # 仍然每秒触发一次通知，方便前端更新倒计时
                )
            )
            if should_notify:
                notice = UnlockQueueNotice(
                    resource_id=job.resource_id,
                    queue_position=live_position,
                    ahead_count=ahead_count,
                    wait_seconds=wait_seconds,
                    queued_seconds=queued_seconds,
                    rate_limit_per_minute=self.rate_limit_per_minute,
                    user_id=job.user_id,
                )
                try:
                    await job.wait_callback(notice)
                except Exception as exc:
                    logger.debug("更新 HDHive 解锁排队提示失败: %s", exc)
                job.wait_notified = True
                job.last_notice_at = time.monotonic()

            # 每次最多只睡 1 秒，醒来后循环检查。这保证了排队期间每秒都能正确调用 wait_callback 进行进度通知
            await asyncio.sleep(min(wait_seconds, 1.0))


hdhive_unlock_service = HDHiveUnlockService()
