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


FINAL_STATES = {"done", "already_ok", "failed"}
ACTIVE_STATES = {"pending", "processing"}


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
            "version": 1,
            "folder_key": folder_key,
            "status": "active",
            "created_at": now,
            "updated_at": now,
            "last_scan_at": 0.0,
            "scan_revision": 0,
            "items": {},
        }

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
        data.setdefault("version", 1)
        data.setdefault("folder_key", folder_key)
        data.setdefault("status", "active")
        data.setdefault("created_at", time.time())
        data.setdefault("updated_at", time.time())
        data.setdefault("last_scan_at", 0.0)
        data.setdefault("scan_revision", 0)
        data.setdefault("items", {})
        if not isinstance(data["items"], dict):
            data["items"] = {}
        return data

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
            item.setdefault("status", "pending")
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
            "status": "pending",
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
                if item.get("status") == "processing" and float(item.get("lease_until") or 0) <= now:
                    item["status"] = "pending"
                    item["last_error"] = "processing_lease_expired"
                    item["updated_at"] = now
                    reset_processing += 1
                if item.get("status") == "missing":
                    item["status"] = "pending"
                    item["updated_at"] = now

            for rel_path, item in list(manifest["items"].items()):
                if rel_path in current_set:
                    continue
                status = str(item.get("status") or "pending")
                if status in ACTIVE_STATES:
                    item["status"] = "missing"
                    item["last_error"] = "disappeared_before_completion"
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
            item["status"] = "processing"
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
            status="failed",
            reason=reason,
        )

    def finalize_decision(self, folder_key: str, rel_paths: list[str]) -> dict:
        rec = self.reconcile(folder_key, rel_paths)
        manifest = rec["manifest"]
        items = manifest.get("items", {})

        counts = {
            "pending": 0,
            "processing": 0,
            "done": 0,
            "already_ok": 0,
            "failed": 0,
            "missing": 0,
        }
        blockers: list[str] = []
        for rel_path, item in items.items():
            status = str(item.get("status") or "pending")
            if status not in counts:
                counts[status] = 0
            counts[status] += 1
            if status in {"pending", "processing", "missing"}:
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
            manifest["status"] = "completed"
            manifest["updated_at"] = time.time()
            self.save(folder_key, manifest)

    def mark_folder_failed(self, folder_key: str, reason: str):
        with self.lock:
            manifest = self.load(folder_key)
            manifest["status"] = f"failed:{reason or 'unknown'}"
            manifest["updated_at"] = time.time()
            self.save(folder_key, manifest)

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
            counts: dict[str, int] = {
                "pending": 0,
                "processing": 0,
                "done": 0,
                "already_ok": 0,
                "failed": 0,
                "missing": 0,
            }
            blockers: list[str] = []
            for rel_path, item in items.items():
                status = str((item or {}).get("status") or "pending")
                counts[status] = counts.get(status, 0) + 1
                if status in {"pending", "processing", "missing"}:
                    blockers.append(Path(rel_path).name)

            summaries.append(
                {
                    "folder_key": str(data.get("folder_key") or path.stem),
                    "status": str(data.get("status") or "active"),
                    "counts": counts,
                    "updated_at": float(data.get("updated_at") or 0),
                    "last_scan_at": float(data.get("last_scan_at") or 0),
                    "samples": blockers[:5],
                }
            )

        summaries.sort(key=lambda x: (x.get("status") != "active", -float(x.get("updated_at") or 0)))
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
            status = str(data.get("status") or "active")
            updated_at = float(data.get("updated_at") or 0)
            if status == "active":
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
