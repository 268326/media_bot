"""
STRM 空目录清理服务封装
"""
from __future__ import annotations

import asyncio
import html
import logging
import threading

from strm_prune import load_settings_from_env, run_prune


class StrmPruneService:
    def __init__(self):
        self.lock = threading.Lock()
        self.run_lock = threading.Lock()
        self.last_result: dict[str, object] | None = None
        self.last_error: str = ""

    def status(self) -> dict[str, object]:
        settings = load_settings_from_env()
        with self.lock:
            return {
                "enabled": settings.enabled,
                "roots": list(settings.roots),
                "notify_emby": settings.notify_emby,
                "allow_delete_first_level": settings.allow_delete_first_level,
                "include_roots": settings.include_roots,
                "last_error": self.last_error,
                "last_result": self.last_result,
            }

    async def run(self, apply_changes: bool) -> dict[str, object]:
        settings = load_settings_from_env()
        if not settings.enabled:
            return {"ok": False, "message": "STRM 空目录清理未启用（STRM_PRUNE_ENABLED=0）"}

        acquired = self.run_lock.acquire(blocking=False)
        if not acquired:
            return {"ok": False, "message": "已有 STRM 空目录清理任务在执行，请稍后再试"}

        try:
            result = await asyncio.to_thread(run_prune, settings, apply_changes)
            summary = self._to_summary(result)
            with self.lock:
                self.last_error = ""
                self.last_result = summary
            return {"ok": True, "message": self._format_message(summary), "summary": summary}
        except Exception as exc:
            logging.exception("❌ STRM 空目录清理执行失败")
            with self.lock:
                self.last_error = str(exc)
            return {"ok": False, "message": f"STRM 空目录清理失败: {exc}"}
        finally:
            self.run_lock.release()

    def _to_summary(self, result) -> dict[str, object]:
        payload: dict[str, object] = {
            "mode": result.mode,
            "roots": list(result.settings.roots),
            "notify_emby": result.settings.notify_emby,
            "allow_delete_first_level": result.settings.allow_delete_first_level,
            "include_roots": result.settings.include_roots,
            "root_total": len(result.scan.roots),
            "total_dirs": result.scan.total_dirs,
            "scanned_dirs": result.scan.scanned_dirs,
            "deletable_total": len(result.scan.deletable_dirs),
            "deletable_dirs": list(result.scan.deletable_dirs),
            "errors": list(result.scan.errors),
        }
        if result.apply is not None:
            payload.update(
                {
                    "deleted_total": len(result.apply.deleted_paths),
                    "deleted_paths": list(result.apply.deleted_paths),
                    "parent_dirs": list(result.apply.parent_dirs),
                    "emby_notified_dirs": list(result.apply.emby_notified_dirs),
                    "emby_refreshed_item_ids": list(result.apply.emby_refreshed_item_ids),
                    "errors": list(result.apply.errors),
                }
            )
        return payload

    def _format_message(self, summary: dict[str, object]) -> str:
        mode = "实际删除" if summary.get("mode") == "apply" else "预览"
        lines = [
            f"🧹 <b>STRM 空目录清理完成（{mode}）</b>",
            "",
            f"根目录数: <code>{summary.get('root_total', 0)}</code>",
            f"扫描目录数: <code>{summary.get('scanned_dirs', 0)}</code>",
            f"待删除目录数: <code>{summary.get('deletable_total', 0)}</code>",
        ]
        if summary.get("mode") == "apply":
            lines.append(f"已删除目录数: <code>{summary.get('deleted_total', 0)}</code>")
            lines.append(f"Emby 通知目录数: <code>{len(summary.get('emby_notified_dirs', []))}</code>")
            lines.append(f"Emby 递归刷新库数: <code>{len(summary.get('emby_refreshed_item_ids', []))}</code>")

        deletable = list(summary.get("deletable_dirs", []))
        if deletable:
            lines.append("")
            lines.append("<b>目录示例：</b>")
            for path in deletable[:8]:
                lines.append(f"• <code>{html.escape(str(path))}</code>")
            remain = len(deletable) - min(len(deletable), 8)
            if remain > 0:
                lines.append(f"• … 其余 <code>{remain}</code> 个未展开")

        errors = list(summary.get("errors", []))
        if errors:
            lines.append("")
            lines.append("<b>提示：</b>")
            for item in errors[:6]:
                lines.append(f"• {html.escape(str(item))}")
            remain = len(errors) - min(len(errors), 6)
            if remain > 0:
                lines.append(f"• 其余 <code>{remain}</code> 条请查看日志")

        return "\n".join(lines)


strm_prune_service = StrmPruneService()
