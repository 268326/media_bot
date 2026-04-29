"""
STRM 批次计划（manifest）持久化与闭环判定
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from pathlib import Path

from strm_reason import (
    ACTIVE_ITEM_STATUSES,
    BATCH_STATUS_ACTIVE,
    BATCH_STATUS_COMPLETED,
    BATCH_STATUS_FAILED,
    BLOCKING_ITEM_STATUSES,
    DISAPPEARED_BEFORE_COMPLETION,
    ITEM_STATUS_ORDER,
    PROCESSING_LEASE_EXPIRED,
    STATUS_ALREADY_OK,
    STATUS_DONE,
    STATUS_FAILED,
    STATUS_MISSING,
    STATUS_PENDING,
    STATUS_PROCESSING,
    UNKNOWN_REASON,
    humanize_batch_status,
    make_batch_status,
    normalize_item_status,
    split_batch_status,
)


MANIFEST_VERSION = 1


class StrmBatchState:
    def __init__(self, state_dir: str):
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()

    def _key_hash(self, folder_key: str) -> str:
        return hashlib.sha1(folder_key.encode("utf-8")).hexdigest()[:16]

    def manifest_path(self, folder_key: str) -> Path:
        return self.state_dir / f"{self._key_hash(folder_key)}.json"

    def _empty_manifest(self, folder_key: str) -> dict:
        now = time.time()
        return {
            "version": MANIFEST_VERSION,
            "folder_key": folder_key,
            "status": BATCH_STATUS_ACTIVE,
            "created_at": now,
            "updated_at": now,
            "last_scan_at": 0.0,
            "scan_revision": 0,
            "items": {},
        }

    def _normalize_manifest(self, data: dict, folder_key: str) -> dict:
        manifest = dict(data or {})
        now = time.time()
        manifest["version"] = int(manifest.get("version") or MANIFEST_VERSION)
        manifest["folder_key"] = str(manifest.get("folder_key") or folder_key)
        manifest["status"] = make_batch_status(manifest.get("status") or BATCH_STATUS_ACTIVE)
        manifest.setdefault("created_at", now)
        manifest.setdefault("updated_at", now)
        manifest.setdefault("last_scan_at", 0.0)
        manifest.setdefault("scan_revision", 0)

        raw_items = manifest.get("items") or {}
        if not isinstance(raw_items, dict):
            raw_items = {}

        items: dict[str, dict] = {}
        for rel_path, raw_item in raw_items.items():
            item = dict(raw_item or {})
            item["source_name"] = str(item.get("source_name") or Path(rel_path).name)
            item["target_name"] = str(item.get("target_name") or "")
            item["status"] = normalize_item_status(item.get("status"))
            item["last_error"] = str(item.get("last_error") or "")
            item["updated_at"] = float(item.get("updated_at") or now)
            item["attempts"] = int(item.get("attempts") or 0)
            item["lease_until"] = float(item.get("lease_until") or 0.0)
            item["last_seen_scan_revision"] = int(item.get("last_seen_scan_revision") or 0)
            items[str(rel_path)] = item
        manifest["items"] = items
        return manifest

    def load(self, folder_key: str) -> dict:
        path = self.manifest_path(folder_key)
        if not path.exists():
            return self._empty_manifest(folder_key)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            logging.warning("⚠️ STRM manifest 读取失败，已重建: %s (%s)", path, exc)
            return self._empty_manifest(folder_key)
        if not isinstance(data, dict):
            return self._empty_manifest(folder_key)
        return self._normalize_manifest(data, folder_key)

    def save(self, folder_key: str, manifest: dict):
        path = self.manifest_path(folder_key)
        manifest["updated_at"] = time.time()
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp, path)

    def ensure_item(self, manifest: dict, rel_path: str, *, source_name: str = "") -> dict:
        item = manifest["items"].get(rel_path)
        now = time.time()
        if item:
            item.setdefault("source_name", source_name or Path(rel_path).name)
            item.setdefault("status", STATUS_PENDING)
            item.setdefault("target_name", "")
            item.setdefault("last_error", "")
            item.setdefault("updated_at", now)
            item.setdefault("attempts", 0)
            item.setdefault("lease_until", 0.0)
            item.setdefault("last_seen_scan_revision", manifest.get("scan_revision", 0))
            return item
        item = {
            "source_name": source_name or Path(rel_path).name,
            "target_name": "",
            "status": STATUS_PENDING,
            "last_error": "",
            "updated_at": now,
            "attempts": 0,
            "lease_until": 0.0,
            "last_seen_scan_revision": manifest.get("scan_revision", 0),
        }
        manifest["items"][rel_path] = item
        return item

    def reconcile(self, folder_key: str, rel_paths: list[str]) -> dict:
        with self.lock:
            manifest = self.load(folder_key)
            now = time.time()
            manifest["scan_revision"] = int(manifest.get("scan_revision", 0)) + 1
            manifest["last_scan_at"] = now
            current_rev = manifest["scan_revision"]
            current_set = set(rel_paths)

            new_items = 0
            missing_active = 0
            reset_processing = 0

            for rel_path in rel_paths:
                existed = rel_path in manifest["items"]
                item = self.ensure_item(manifest, rel_path)
                item["last_seen_scan_revision"] = current_rev
                if not existed:
                    new_items += 1
                if item.get("status") == STATUS_PROCESSING and float(item.get("lease_until") or 0) <= now:
                    item["status"] = STATUS_PENDING
                    item["last_error"] = PROCESSING_LEASE_EXPIRED
                    item["updated_at"] = now
                    reset_processing += 1
                if item.get("status") == STATUS_MISSING:
                    item["status"] = STATUS_PENDING
                    item["updated_at"] = now

            for rel_path, item in list(manifest["items"].items()):
                if rel_path in current_set:
                    continue
                status = str(item.get("status") or STATUS_PENDING)
                if status in ACTIVE_ITEM_STATUSES:
                    item["status"] = STATUS_MISSING
                    item["last_error"] = DISAPPEARED_BEFORE_COMPLETION
                    item["updated_at"] = now
                    missing_active += 1

            self.save(folder_key, manifest)
            return {
                "manifest": manifest,
                "new_items": new_items,
                "missing_active": missing_active,
                "reset_processing": reset_processing,
            }

    def mark_processing(self, folder_key: str, rel_path: str, *, source_name: str, lease_seconds: int) -> None:
        with self.lock:
            manifest = self.load(folder_key)
            item = self.ensure_item(manifest, rel_path, source_name=source_name)
            item["status"] = STATUS_PROCESSING
            item["source_name"] = source_name
            item["attempts"] = int(item.get("attempts") or 0) + 1
            item["lease_until"] = time.time() + max(lease_seconds, 30)
            item["updated_at"] = time.time()
            self.save(folder_key, manifest)

    def mark_completed(self, folder_key: str, old_rel_path: str, final_rel_path: str, *, source_name: str, target_name: str, status: str, reason: str = "") -> None:
        with self.lock:
            manifest = self.load(folder_key)
            item = manifest["items"].pop(old_rel_path, None)
            now = time.time()
            if not item:
                item = self.ensure_item(manifest, final_rel_path, source_name=source_name)
            else:
                manifest["items"][final_rel_path] = item

            prev_status = normalize_item_status(item.get("status"))
            prev_source_name = str(item.get("source_name") or source_name)
            prev_target_name = str(item.get("target_name") or target_name)
            preserve_done = (
                old_rel_path == final_rel_path
                and prev_status == STATUS_DONE
                and status == STATUS_ALREADY_OK
                and prev_target_name
                and prev_target_name != prev_source_name
            )

            if preserve_done:
                item["source_name"] = prev_source_name
                item["target_name"] = prev_target_name
                item["status"] = STATUS_DONE
            else:
                item["source_name"] = source_name
                item["target_name"] = target_name
                item["status"] = status
            item["last_error"] = reason or ""
            item["lease_until"] = 0.0
            item["updated_at"] = now
            item["last_seen_scan_revision"] = manifest.get("scan_revision", 0)
            self.save(folder_key, manifest)

    def mark_failed(self, folder_key: str, rel_path: str, *, source_name: str, target_name: str, reason: str) -> None:
        self.mark_completed(
            folder_key,
            rel_path,
            rel_path,
            source_name=source_name,
            target_name=target_name,
            status=STATUS_FAILED,
            reason=reason,
        )

    def finalize_decision(self, folder_key: str, rel_paths: list[str]) -> dict:
        rec = self.reconcile(folder_key, rel_paths)
        manifest = rec["manifest"]
        items = manifest.get("items", {})

        counts = {status: 0 for status in ITEM_STATUS_ORDER}
        blockers: list[str] = []
        for rel_path, item in items.items():
            status = str(item.get("status") or STATUS_PENDING)
            if status not in counts:
                counts[status] = 0
            counts[status] += 1
            if status in BLOCKING_ITEM_STATUSES:
                blockers.append(rel_path)

        ready = not blockers and rec["new_items"] == 0 and rec["missing_active"] == 0 and rec["reset_processing"] == 0
        return {
            "ready": ready,
            "counts": counts,
            "new_items": rec["new_items"],
            "missing_active": rec["missing_active"],
            "reset_processing": rec["reset_processing"],
            "samples": [Path(p).name for p in blockers[:5]],
            "manifest": manifest,
        }

    def mark_folder_completed(self, folder_key: str):
        with self.lock:
            manifest = self.load(folder_key)
            manifest["status"] = BATCH_STATUS_COMPLETED
            manifest["updated_at"] = time.time()
            self.save(folder_key, manifest)

    def mark_folder_failed(self, folder_key: str, reason: str):
        with self.lock:
            manifest = self.load(folder_key)
            manifest["status"] = f"{BATCH_STATUS_FAILED}:{reason or UNKNOWN_REASON}"
            manifest["updated_at"] = time.time()
            self.save(folder_key, manifest)

    def folder_report(self, folder_key: str, *, detail_limit: int = 60) -> dict:
        with self.lock:
            manifest = self.load(folder_key)

        renamed_count = 0
        already_ok_count = 0
        failed_count = 0
        rename_items: list[tuple[str, str]] = []
        fail_items: list[tuple[str, str]] = []

        for rel_path, item in sorted((manifest.get("items") or {}).items()):
            source_name = str((item or {}).get("source_name") or Path(rel_path).name)
            target_name = str((item or {}).get("target_name") or source_name)
            status = normalize_item_status((item or {}).get("status"))
            reason = str((item or {}).get("last_error") or UNKNOWN_REASON)

            if status == STATUS_DONE:
                renamed_count += 1
                if len(rename_items) < detail_limit:
                    rename_items.append((source_name, target_name))
            elif status == STATUS_ALREADY_OK:
                already_ok_count += 1
            elif status in (STATUS_FAILED, STATUS_MISSING):
                failed_count += 1
                if len(fail_items) < detail_limit:
                    fail_items.append((target_name or source_name, reason))

        return {
            "renamed_count": renamed_count,
            "already_ok_count": already_ok_count,
            "failed_count": failed_count,
            "rename_items": rename_items,
            "fail_items": fail_items,
        }

    def list_manifests_summary(self) -> list[dict]:
        summaries: list[dict] = []
        for path in sorted(self.state_dir.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception as exc:
                summaries.append(
                    {
                        "folder_key": path.stem,
                        "status": f"broken:{exc}",
                        "counts": {},
                        "updated_at": 0,
                        "last_scan_at": 0,
                        "samples": [],
                    }
                )
                continue

            items = data.get("items") or {}
            counts: dict[str, int] = {status: 0 for status in ITEM_STATUS_ORDER}
            blockers: list[str] = []
            for rel_path, item in items.items():
                status = str((item or {}).get("status") or STATUS_PENDING)
                counts[status] = counts.get(status, 0) + 1
                if status in BLOCKING_ITEM_STATUSES:
                    blockers.append(Path(rel_path).name)

            summaries.append(
                {
                    "folder_key": str(data.get("folder_key") or path.stem),
                    "status": str(data.get("status") or BATCH_STATUS_ACTIVE),
                    "status_label": humanize_batch_status(data.get("status") or BATCH_STATUS_ACTIVE),
                    "counts": counts,
                    "updated_at": float(data.get("updated_at") or 0),
                    "last_scan_at": float(data.get("last_scan_at") or 0),
                    "samples": blockers[:5],
                }
            )

        summaries.sort(key=lambda x: (x.get("status") != BATCH_STATUS_ACTIVE, -float(x.get("updated_at") or 0)))
        return summaries

    def cleanup_expired_manifests(self, retention_hours: int) -> int:
        if retention_hours <= 0:
            return 0
        cutoff = time.time() - (retention_hours * 3600)
        removed = 0
        for path in sorted(self.state_dir.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            status = str(data.get("status") or BATCH_STATUS_ACTIVE)
            status_code, _ = split_batch_status(status)
            updated_at = float(data.get("updated_at") or 0)
            if status_code == BATCH_STATUS_ACTIVE:
                continue
            if updated_at <= 0 or updated_at > cutoff:
                continue
            try:
                path.unlink()
                removed += 1
            except Exception as exc:
                logging.warning("⚠️ STRM manifest 清理失败: %s (%s)", path, exc)
        if removed:
            logging.info("🧹 STRM manifest 已自动清理: removed=%s retention_hours=%s", removed, retention_hours)
        return removed
