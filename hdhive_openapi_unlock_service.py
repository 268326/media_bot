"""
HDHive 官方 OpenAPI 串行解锁队列服务
统一封装官方 SDK 的 `/api/open/resources/unlock` 调用。

设计原则：
- 不在客户端写死任何官方 QPS / 配额。
- 仅做串行化提交，真正的限流与退避完全以官方 `429` / `Retry-After`
  为准，由 `hdhive_openapi_adapter.py` 适配层处理。
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from threading import Lock
from typing import Any, Awaitable, Callable

from hdhive_openapi_adapter import build_authenticated_client_context

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class UnlockQueueNotice:
    resource_id: str
    queue_position: int
    ahead_count: int
    wait_seconds: float
    queued_seconds: float
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


def _request_unlock_sync(resource_id: str) -> dict[str, Any]:
    with build_authenticated_client_context() as client:
        payload = client.unlock_resource(resource_id)

    data = payload.get("data") or {}
    if not isinstance(data, dict):
        raise RuntimeError("解锁接口返回异常")
    return data


class HDHiveUnlockService:
    def __init__(self):
        self.worker_count = 1
        self.queue: asyncio.Queue[UnlockJob] | None = None
        self.worker_tasks: list[asyncio.Task] = []
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
            self.sequence_counter = 0
            self.active_sequences.clear()
            self.worker_tasks = [asyncio.create_task(self._worker_loop(1))]
            self.processing_count = 0
            self.started = True

        logger.info("✅ HDHive 解锁队列已启动: mode=serial workers=1")

    async def stop(self):
        with self.state_lock:
            if not self.started:
                return
            queue = self.queue
            worker_tasks = list(self.worker_tasks)
            self.queue = None
            self.worker_tasks = []
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

    async def _worker_loop(self, worker_id: int):
        while True:
            queue = self.queue
            if queue is None:
                return

            job = await queue.get()
            with self.state_lock:
                self.processing_count += 1

            try:
                now = time.monotonic()
                queued_seconds = max(0.0, now - job.created_at)
                with self.state_lock:
                    live_position = self._compute_live_position_unlocked(job.sequence)
                ahead_count = max(0, live_position - 1)

                if job.wait_callback is not None and ahead_count > 0:
                    notice = UnlockQueueNotice(
                        resource_id=job.resource_id,
                        queue_position=live_position,
                        ahead_count=ahead_count,
                        wait_seconds=0.0,
                        queued_seconds=queued_seconds,
                        user_id=job.user_id,
                    )
                    try:
                        await job.wait_callback(notice)
                    except Exception as exc:
                        logger.debug("更新 HDHive 解锁排队提示失败: %s", exc)

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


hdhive_openapi_unlock_service = HDHiveUnlockService()
