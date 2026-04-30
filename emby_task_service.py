from __future__ import annotations

import asyncio
import datetime as dt
import html
import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests
from aiogram import Bot

from config import BOT_CHAT_IDS, BOT_USER_IDS

COMMON_TASK_DICT = {
    "Scan media library": "扫描媒体库",
    "Refresh Guide": "刷新电视指南",
    "Refresh channels": "刷新直播频道",
    "Clean up collections and playlists": "清理合集与播放列表",
    "Refresh people": "刷新演员与人物信息",
    "Refresh network shows": "刷新网络剧集",
    "Clean up image cache": "清理图像缓存",
    "Download missing subtitles": "下载缺失的字幕",
    "Extract chapter images": "提取视频章节图片",
    "Refresh local IP addresses": "刷新本地 IP 地址",
    "Check for application updates": "检查系统更新",
    "Check for plugin updates": "检查插件更新",
    "Optimize database": "优化数据库结构",
    "Vacuum database": "压缩与清理数据库 (Vacuum)",
    "Remove old watch history": "移除陈旧的播放历史",
    "Sync Playstate": "同步播放状态",
    "Update Plugins": "自动更新插件",
    "Update server": "自动更新服务器",
    "Cache images": "缓存图像",
    "Backup database": "备份服务器数据库",
    "Auto Organize": "自动整理媒体",
    "Generate Intro Video": "生成片头视频",
    "Rotate log file": "轮转并清理日志文件",
    "Clean up sync directories": "清理同步目录",
    "Convert media": "转换媒体格式",
    "Refresh library metadata": "刷新媒体库元数据",
    "Scan local network": "扫描本地局域网设备",
    "Download missing plugin updates": "下载缺失的插件更新",
    "Remove old sync jobs": "移除陈旧的同步任务",
    "Scrape Jav": "JavScraper 搜刮器同步",
    "Update JavScraper Index": "更新 JavScraper 索引",
    "MetaTube: Update Subscriptions": "MetaTube: 更新订阅",
    "MetaTube: Auto Update Metadata": "MetaTube: 自动更新元数据",
    "TMDb: Refresh metadata": "TMDb: 刷新元数据",
    "TheMovieDb: Refresh metadata": "TheMovieDb: 刷新元数据",
    "OMDb: Refresh metadata": "OMDb: 刷新元数据",
    "TVDb: Refresh metadata": "TVDb: 刷新元数据",
    "Douban: Refresh metadata": "豆瓣(Douban): 刷新元数据",
    "Bgm.tv: Refresh metadata": "Bgm.tv: 刷新动漫元数据",
    "Bangumi: Refresh metadata": "Bangumi: 刷新动漫元数据",
    "AniDB: Refresh metadata": "AniDB: 刷新动漫元数据",
    "Kitsu: Refresh metadata": "Kitsu: 刷新动漫元数据",
    "Open Subtitles: Download missing subtitles": "Open Subtitles: 下载缺失字幕",
    "Subscene: Download missing subtitles": "Subscene: 下载缺失字幕",
    "Shooter: Download missing subtitles": "伪射手(Shooter): 下载缺失字幕",
    "Thunder: Download missing subtitles": "迅雷(Thunder): 下载缺失字幕",
    "Fanart.tv: Download missing images": "Fanart.tv: 下载缺失的海报与艺术图",
    "Screen Grabber: Extract chapter images": "截屏器: 提取视频章节预览图",
    "Trakt.tv: Sync Library": "Trakt.tv: 同步媒体库",
    "Trakt.tv: Import Playstates": "Trakt.tv: 导入播放状态",
    "Trakt: Sync Library": "Trakt: 同步媒体库",
    "Trakt: Import Playstates": "Trakt: 导入播放状态",
    "Auto Box Sets: Create Collections": "Auto Box Sets: 自动创建电影合集",
    "Intro Skipper: Analyze Audio": "跳过片头(Intro Skipper): 分析音频指纹",
    "Intro Skipper: Analyze Video": "跳过片头(Intro Skipper): 分析视频画面",
    "Theme Songs: Download theme songs": "主题曲: 下载剧集主题曲",
    "Theme Videos: Download theme videos": "主题视频: 下载剧集主题背景视频",
    "Playback Reporting: Backup database": "播放统计: 备份统计数据库",
    "Playback Reporting: Aggregate Data": "播放统计: 聚合计算历史数据",
    "EmbyStat: Refresh data": "EmbyStat: 刷新统计数据",
    "Statistics: Calculate statistics": "数据看板: 计算全站数据",
    "Statistics: Clean up old data": "数据看板: 清理过期数据",
    "Webhooks: Send test webhook": "Webhooks: 发送测试通知",
    "Slack: Send test notification": "Slack: 发送测试通知",
    "Telegram: Send test notification": "Telegram: 发送测试通知",
    "Discord: Send test notification": "Discord: 发送测试通知",
    "M3U: Refresh guide": "M3U: 刷新直播节目单",
    "XmlTV: Refresh guide": "XmlTV: 刷新直播节目单",
    "HDHomeRun: Refresh guide": "HDHomeRun: 刷新直播节目单",
}

CATEGORY_TRANSLATIONS = {
    "Library": "媒体库扫描",
    "Application": "系统与应用",
    "Maintenance": "日常维护",
    "Live TV": "电视直播",
    "Sync": "状态同步",
    "Plugins": "插件自动化",
}

STATUS_TRANSLATIONS = {
    "Completed": "成功",
    "Failed": "失败",
    "Cancelled": "已取消",
    "Canceled": "已取消",
    "Aborted": "已中止",
    "Running": "运行中",
    "Idle": "空闲",
    "Ready": "就绪",
    "Queued": "排队中",
}


@dataclass(slots=True)
class EmbyTaskSettings:
    enabled: bool
    url: str
    api_key: str
    server_type: str
    notify_enabled: bool
    poll_interval: int
    request_timeout: int
    http_retries: int
    http_backoff: float
    state_path: str


class EmbyTaskService:
    def __init__(self):
        self.settings = self._load_settings()
        self.bot: Bot | None = None
        self.started = False
        self.poller_task: asyncio.Task | None = None
        self.notify_enabled = self.settings.notify_enabled
        self.last_end_times: dict[str, str | None] = {}
        self._poller_initialized = False
        self._snapshot_poll_state()
        self._load_state()

    @staticmethod
    def _pick_first_nonempty(*values: str | None) -> str:
        for value in values:
            text = str(value or "").strip()
            if text:
                return text
        return ""

    @staticmethod
    def _parse_bool(value: str | None, default: bool = False) -> bool:
        if value is None:
            return default
        text = str(value).strip().lower()
        if not text:
            return default
        return text in ("1", "true", "yes", "on")

    @staticmethod
    def _parse_int(value: str | None, default: int, *, minimum: int | None = None) -> int:
        try:
            parsed = int(str(value).strip()) if value is not None and str(value).strip() else default
        except ValueError:
            parsed = default
        if minimum is not None and parsed < minimum:
            return minimum
        return parsed

    @staticmethod
    def _parse_float(value: str | None, default: float, *, minimum: float | None = None) -> float:
        try:
            parsed = float(str(value).strip()) if value is not None and str(value).strip() else default
        except ValueError:
            parsed = default
        if minimum is not None and parsed < minimum:
            return minimum
        return parsed

    def _load_settings(self) -> EmbyTaskSettings:
        url = self._pick_first_nonempty(
            os.getenv("EMBY_TASKS_URL"),
            os.getenv("STRM_PRUNE_EMBY_URL"),
            os.getenv("EMBY_URL"),
            "http://172.17.0.1:8096",
        )
        api_key = self._pick_first_nonempty(
            os.getenv("EMBY_TASKS_API_KEY"),
            os.getenv("STRM_PRUNE_EMBY_API_KEY"),
            os.getenv("EMBY_API_KEY"),
            os.getenv("EMBYAPIKEY"),
        )
        return EmbyTaskSettings(
            enabled=self._parse_bool(os.getenv("EMBY_TASKS_ENABLED"), False),
            url=url.rstrip("/"),
            api_key=api_key,
            server_type=(self._pick_first_nonempty(os.getenv("EMBY_TASKS_SERVER_TYPE"), os.getenv("EMBY_SERVER_TYPE"), "emby") or "emby").lower(),
            notify_enabled=self._parse_bool(os.getenv("EMBY_TASKS_NOTIFY_ENABLED"), True),
            poll_interval=self._parse_int(os.getenv("EMBY_TASKS_POLL_INTERVAL"), 5, minimum=2),
            request_timeout=self._parse_int(os.getenv("EMBY_TASKS_REQUEST_TIMEOUT"), 8, minimum=3),
            http_retries=self._parse_int(os.getenv("EMBY_TASKS_HTTP_RETRIES"), 3, minimum=1),
            http_backoff=self._parse_float(os.getenv("EMBY_TASKS_HTTP_BACKOFF"), 2.0, minimum=1.0),
            state_path=self._pick_first_nonempty(os.getenv("EMBY_TASKS_STATE_PATH"), "/app/data/emby_tasks_state.json"),
        )

    def _snapshot_poll_state(self) -> None:
        self.last_end_times = {}
        self._poller_initialized = False

    def _load_state(self) -> None:
        path = Path(self.settings.state_path)
        try:
            if not path.exists():
                return
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict) and isinstance(raw.get("notify_enabled"), bool):
                self.notify_enabled = raw["notify_enabled"]
        except Exception as exc:
            logging.warning("⚠️ 读取 Emby 任务状态文件失败，已回退环境配置: %s", exc)

    def _save_state(self) -> None:
        path = Path(self.settings.state_path)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = {"notify_enabled": bool(self.notify_enabled)}
            tmp_path = path.with_suffix(path.suffix + ".tmp")
            tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp_path.replace(path)
        except Exception as exc:
            logging.error("❌ 写入 Emby 任务状态文件失败: %s", exc)

    def _build_url(self, path: str) -> str:
        final_path = path if path.startswith("/") else f"/{path}"
        if self.settings.server_type == "jellyfin":
            if final_path.startswith("/emby/"):
                final_path = final_path.replace("/emby/", "/", 1)
        else:
            if not final_path.startswith("/emby/"):
                final_path = f"/emby{final_path}"
        return f"{self.settings.url}{final_path}"

    def _build_headers(self) -> dict[str, str]:
        if self.settings.server_type == "jellyfin":
            return {"Authorization": f'MediaBrowser Token="{self.settings.api_key}"'}
        return {"X-Emby-Token": self.settings.api_key}

    def _request(self, method: str, path: str, *, json_body: dict[str, Any] | None = None) -> requests.Response:
        url = self._build_url(path)
        last_error: Exception | None = None
        last_response: requests.Response | None = None

        for attempt in range(1, self.settings.http_retries + 1):
            try:
                response = requests.request(
                    method=method,
                    url=url,
                    headers=self._build_headers(),
                    json=json_body,
                    timeout=self.settings.request_timeout,
                )
                if 200 <= response.status_code < 300:
                    return response
                last_response = response
                if 400 <= response.status_code < 500:
                    break
            except requests.RequestException as exc:
                last_error = exc

            if attempt < self.settings.http_retries:
                wait_seconds = self.settings.http_backoff ** (attempt - 1)
                logging.warning(
                    "⚠️ Emby 任务请求失败，%.1fs 后重试（%s/%s） method=%s path=%s",
                    wait_seconds,
                    attempt,
                    self.settings.http_retries,
                    method,
                    path,
                )
                time.sleep(wait_seconds)

        if last_response is not None:
            text = (last_response.text or "").strip()
            if len(text) > 300:
                text = text[:300] + "..."
            raise RuntimeError(f"HTTP {last_response.status_code}: {text or 'empty response'}")
        if last_error is not None:
            raise RuntimeError(str(last_error))
        raise RuntimeError("unknown error")

    def _request_json(self, method: str, path: str) -> Any:
        response = self._request(method, path)
        try:
            return response.json()
        except Exception as exc:
            raise RuntimeError(f"响应不是合法 JSON: {exc}") from exc

    def _parse_time(self, raw: str | None) -> dt.datetime | None:
        text = str(raw or "").strip()
        if not text:
            return None
        text = text.replace("Z", "+00:00")
        try:
            value = dt.datetime.fromisoformat(text)
        except ValueError:
            try:
                trimmed = text.split(".", 1)[0]
                if trimmed.endswith("+00:00"):
                    value = dt.datetime.fromisoformat(trimmed)
                else:
                    value = dt.datetime.strptime(trimmed, "%Y-%m-%dT%H:%M:%S")
                    value = value.replace(tzinfo=dt.timezone.utc)
            except Exception:
                return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=dt.timezone.utc)
        return value.astimezone(dt.timezone(dt.timedelta(hours=8)))

    def _format_time(self, raw: str | None) -> str:
        value = self._parse_time(raw)
        if not value:
            return "-"
        return value.strftime("%Y-%m-%d %H:%M:%S")

    def _translate_task_name(self, name: str) -> str:
        return COMMON_TASK_DICT.get(name, name)

    def _translate_category(self, category: str) -> str:
        return CATEGORY_TRANSLATIONS.get(category, category or "未分类")

    def _translate_status(self, status: str) -> str:
        return STATUS_TRANSLATIONS.get(status, status or "未知")

    def _normalize_task(self, task: dict[str, Any]) -> dict[str, Any]:
        original_name = str(task.get("Name") or "未命名任务")
        category = str(task.get("Category") or "未分类")
        state = str(task.get("State") or "")
        progress = task.get("CurrentProgressPercentage")
        try:
            progress_value = int(float(progress)) if progress not in (None, "") else None
        except (TypeError, ValueError):
            progress_value = None
        is_running = bool(task.get("IsRunning")) or state.lower() == "running"

        last_result = task.get("LastExecutionResult") or {}
        last_status = str(last_result.get("Status") or "").strip()
        end_time = last_result.get("EndTimeUtc") or last_result.get("EndTime") or ""
        start_time = last_result.get("StartTimeUtc") or last_result.get("StartTime") or ""
        next_time = task.get("NextExecutionTimeUtc") or task.get("NextExecutionTime") or ""

        if is_running:
            status_text = f"运行中 {progress_value}%" if progress_value is not None else "运行中"
        elif state and state.lower() not in ("idle", "ready"):
            status_text = self._translate_status(state)
        else:
            status_text = "空闲"

        last_result_text = "-"
        if last_status or end_time:
            parts = []
            if last_status:
                parts.append(self._translate_status(last_status))
            if end_time:
                parts.append(self._format_time(end_time))
            last_result_text = " | ".join(parts)

        return {
            "id": str(task.get("Id") or ""),
            "name": original_name,
            "display_name": self._translate_task_name(original_name),
            "category": category,
            "display_category": self._translate_category(category),
            "state": state,
            "is_running": is_running,
            "progress": progress_value,
            "status_text": status_text,
            "last_status": last_status,
            "last_result_text": last_result_text,
            "last_end_time": end_time,
            "last_start_time": start_time,
            "last_start_text": self._format_time(start_time) if start_time else "-",
            "next_run_text": self._format_time(next_time) if next_time else "-",
            "is_hidden": bool(task.get("IsHidden")),
            "is_enabled": task.get("IsEnabled"),
        }

    def _sort_tasks(self, tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return sorted(
            tasks,
            key=lambda item: (
                0 if item.get("is_running") else 1,
                str(item.get("display_category") or ""),
                str(item.get("display_name") or ""),
            ),
        )

    def _targets(self) -> list[int]:
        raw_targets = BOT_CHAT_IDS or BOT_USER_IDS
        result: list[int] = []
        for item in raw_targets:
            try:
                result.append(int(str(item).strip()))
            except ValueError:
                continue
        return result

    def status(self) -> dict[str, Any]:
        configured = bool(self.settings.url and self.settings.api_key)
        return {
            "enabled": self.settings.enabled,
            "configured": configured,
            "notify_enabled": self.notify_enabled,
            "poller_running": bool(self.poller_task and not self.poller_task.done()),
            "server_type": self.settings.server_type,
            "url": self.settings.url,
            "state_path": self.settings.state_path,
            "poll_interval": self.settings.poll_interval,
        }

    def validate(self) -> tuple[bool, str]:
        if not self.settings.enabled:
            return False, "Emby 任务管理未启用（EMBY_TASKS_ENABLED=0）"
        if not self.settings.url:
            return False, "未配置 EMBY_TASKS_URL（可回退 STRM_PRUNE_EMBY_URL / EMBY_URL）"
        if not self.settings.api_key:
            return False, "未配置 EMBY_TASKS_API_KEY（可回退 STRM_PRUNE_EMBY_API_KEY / EMBY_API_KEY）"
        return True, "ok"

    async def start(self, bot: Bot | None = None) -> None:
        self.bot = bot
        self.started = True
        ok, reason = self.validate()
        if not ok:
            logging.info("ℹ️ Emby 任务服务未启动轮询: %s", reason)
            return
        await self._sync_poller()
        logging.info(
            "✅ Emby 任务服务已就绪: url=%s notify_enabled=%s poll_interval=%ss",
            self.settings.url,
            self.notify_enabled,
            self.settings.poll_interval,
        )

    async def stop(self) -> None:
        self.started = False
        await self._stop_poller()
        self.bot = None
        logging.info("🛑 Emby 任务服务已停止")

    async def _stop_poller(self) -> None:
        task = self.poller_task
        self.poller_task = None
        self._snapshot_poll_state()
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def _sync_poller(self) -> None:
        should_run = bool(self.started and self.notify_enabled and self.bot)
        ok, _ = self.validate()
        should_run = should_run and ok
        if should_run and not self.poller_task:
            self.poller_task = asyncio.create_task(self._poll_loop())
            return
        if not should_run and self.poller_task:
            await self._stop_poller()

    async def set_notify_enabled(self, enabled: bool) -> bool:
        self.notify_enabled = bool(enabled)
        self._save_state()
        await self._sync_poller()
        return self.notify_enabled

    async def toggle_notify(self) -> bool:
        return await self.set_notify_enabled(not self.notify_enabled)

    async def list_tasks(self) -> dict[str, Any]:
        ok, reason = self.validate()
        if not ok:
            return {"ok": False, "message": reason, "tasks": []}
        try:
            raw = await asyncio.to_thread(self._request_json, "GET", "/ScheduledTasks")
            if isinstance(raw, dict):
                raw_items = raw.get("Items") or raw.get("items") or []
            else:
                raw_items = raw or []
            tasks = [self._normalize_task(item) for item in raw_items if isinstance(item, dict)]
            return {"ok": True, "message": "ok", "tasks": self._sort_tasks(tasks), "status": self.status()}
        except Exception as exc:
            logging.error("❌ 获取 Emby 计划任务失败: %s", exc)
            return {"ok": False, "message": f"获取 Emby 计划任务失败: {exc}", "tasks": [], "status": self.status()}

    async def start_task(self, task_id: str) -> dict[str, Any]:
        ok, reason = self.validate()
        if not ok:
            return {"ok": False, "message": reason}
        try:
            await asyncio.to_thread(self._request, "POST", f"/ScheduledTasks/Running/{task_id}")
            return {"ok": True, "message": "任务已启动"}
        except Exception as exc:
            logging.error("❌ 启动 Emby 任务失败: task_id=%s error=%s", task_id, exc)
            return {"ok": False, "message": f"启动任务失败: {exc}"}

    async def stop_task(self, task_id: str) -> dict[str, Any]:
        ok, reason = self.validate()
        if not ok:
            return {"ok": False, "message": reason}
        try:
            await asyncio.to_thread(self._request, "DELETE", f"/ScheduledTasks/Running/{task_id}")
            return {"ok": True, "message": "任务已停止"}
        except Exception as exc:
            logging.error("❌ 停止 Emby 任务失败: task_id=%s error=%s", task_id, exc)
            return {"ok": False, "message": f"停止任务失败: {exc}"}

    async def _send_notification(self, task: dict[str, Any], status: str) -> None:
        if not self.bot:
            return
        targets = self._targets()
        if not targets:
            logging.warning("⚠️ Emby 任务通知已启用，但未配置 bot_chat_id / bot_user_id")
            return

        title = "✅ Emby 任务完成" if status == "Completed" else "❌ Emby 任务失败"
        status_cn = self._translate_status(status)
        text = (
            f"{title}\n\n"
            f"📌 <b>任务</b>: {html.escape(str(task.get('display_name') or task.get('name') or '-'))}\n"
            f"🗂️ <b>分类</b>: {html.escape(str(task.get('display_category') or task.get('category') or '-'))}\n"
            f"📊 <b>状态</b>: {html.escape(status_cn)}\n"
            f"🕒 <b>完成时间</b>: <code>{html.escape(str(task.get('last_result_text') or '-'))}</code>"
        )
        for target in targets:
            try:
                await self.bot.send_message(target, text, parse_mode="HTML")
            except Exception as exc:
                logging.error("❌ Emby 任务通知发送失败: target=%s error=%s", target, exc)

    async def _poll_loop(self) -> None:
        logging.info("🔁 Emby 任务轮询已启动")
        try:
            while True:
                try:
                    result = await self.list_tasks()
                    if result.get("ok"):
                        tasks = result.get("tasks") or []
                        for task in tasks:
                            task_id = str(task.get("id") or "")
                            end_time = str(task.get("last_end_time") or "") or None
                            last_status = str(task.get("last_status") or "")
                            had_previous_snapshot = task_id in self.last_end_times
                            previous_end_time = self.last_end_times.get(task_id)
                            if (
                                self._poller_initialized
                                and had_previous_snapshot
                                and end_time
                                and end_time != previous_end_time
                                and last_status in ("Completed", "Failed")
                            ):
                                await self._send_notification(task, last_status)
                            self.last_end_times[task_id] = end_time
                        self._poller_initialized = True
                except Exception as exc:
                    logging.error("❌ Emby 任务轮询失败: %s", exc)
                await asyncio.sleep(self.settings.poll_interval)
        except asyncio.CancelledError:
            logging.info("🛑 Emby 任务轮询已停止")
            raise


emby_task_service = EmbyTaskService()
