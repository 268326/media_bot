"""
Emby 刷新辅助模块
"""
from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.request
from typing import Sequence


def normalize_path(path: str) -> str:
    text = (path or "").rstrip(os.sep)
    return os.path.abspath(text or os.sep)


def _http_post_json(
    url: str,
    payload: dict,
    api_key: str,
    timeout: int,
    retries: int,
    backoff: float,
) -> dict[str, object]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {
        "Content-Type": "application/json; charset=utf-8",
        "X-Emby-Token": api_key,
    }
    last_error = "unknown error"

    for attempt in range(1, retries + 1):
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                text = raw.decode("utf-8", "ignore") if raw else ""
                return {
                    "ok": True,
                    "status": getattr(resp, "status", 200),
                    "body": text,
                    "attempt": attempt,
                }
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", "ignore") if exc.fp else ""
            last_error = f"HTTP {exc.code}: {raw or exc.reason}"
            if 400 <= exc.code < 500:
                break
        except Exception as exc:  # pragma: no cover - 网络异常依赖环境
            last_error = str(exc)

        if attempt < retries:
            wait_seconds = backoff ** (attempt - 1)
            logging.warning(
                "⚠️ Emby 请求失败，%.1fs 后重试（%s/%s）：%s",
                wait_seconds,
                attempt,
                retries,
                last_error,
            )
            time.sleep(wait_seconds)

    return {
        "ok": False,
        "status": None,
        "body": "",
        "attempt": retries,
        "error": last_error,
    }


def _load_virtual_folders(emby_url: str, api_key: str, timeout: int) -> tuple[list[tuple[str, str, str]], list[str]]:
    endpoint = f"{emby_url.rstrip('/')}/Library/VirtualFolders/Query"
    req = urllib.request.Request(endpoint, headers={"X-Emby-Token": api_key}, method="GET")

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "ignore")
    except Exception as exc:  # pragma: no cover - 网络异常依赖环境
        return [], [f"查询 Emby 媒体库失败: {exc}"]

    try:
        data = json.loads(raw)
    except Exception as exc:
        return [], [f"解析 Emby 媒体库列表失败: {exc}"]

    libraries: list[tuple[str, str, str]] = []
    for item in data.get("Items", []):
        item_id = item.get("ItemId") or item.get("Id")
        if not item_id:
            continue
        name = str(item.get("Name") or "Unknown")
        for location in item.get("Locations") or []:
            libraries.append((normalize_path(str(location)), str(item_id), name))

    libraries.sort(key=lambda entry: len(entry[0]), reverse=True)
    return libraries, []


def notify_after_delete(
    parent_dirs: Sequence[str],
    emby_url: str,
    api_key: str,
    update_type: str,
    timeout: int,
    retries: int,
    backoff: float,
) -> dict[str, object]:
    endpoint = f"{emby_url.rstrip('/')}/Library/Media/Updated"
    notified_dirs: list[str] = []
    refreshed_item_ids: list[str] = []
    errors: list[str] = []

    for parent_dir in parent_dirs:
        payload = {"Updates": [{"Path": parent_dir, "UpdateType": update_type}]}
        logging.info("🔔 通知 Emby 局部刷新：%s", parent_dir)
        result = _http_post_json(endpoint, payload, api_key, timeout, retries, backoff)
        if result.get("ok"):
            notified_dirs.append(parent_dir)
            continue
        errors.append(
            f"Emby 局部刷新失败: {parent_dir} -> {result.get('error')} "
            f"(HTTP {result.get('status')}, 尝试 {result.get('attempt')} 次)"
        )

    libraries, load_errors = _load_virtual_folders(emby_url, api_key, timeout)
    errors.extend(load_errors)
    if not libraries:
        return {
            "parent_dirs": list(parent_dirs),
            "notified_dirs": notified_dirs,
            "refreshed_item_ids": refreshed_item_ids,
            "errors": errors,
        }

    target_item_ids: list[str] = []
    for parent_dir in parent_dirs:
        matched = False
        normalized_parent = normalize_path(parent_dir)
        for location, item_id, library_name in libraries:
            if normalized_parent == location or normalized_parent.startswith(location + os.sep):
                matched = True
                if item_id not in target_item_ids:
                    target_item_ids.append(item_id)
                    logging.info("📚 命中 Emby 媒体库：%s | ItemId=%s", library_name, item_id)
                break
        if not matched:
            errors.append(f"未找到目录所属 Emby 媒体库: {normalized_parent}")

    for item_id in target_item_ids:
        refresh_endpoint = (
            f"{emby_url.rstrip('/')}/Items/{item_id}/Refresh"
            "?Recursive=true"
            "&MetadataRefreshMode=Default"
            "&ImageRefreshMode=Default"
            "&ReplaceAllMetadata=false"
            "&ReplaceAllImages=false"
        )
        result = _http_post_json(refresh_endpoint, {}, api_key, timeout, retries, backoff)
        if result.get("ok"):
            refreshed_item_ids.append(item_id)
            logging.info("♻️ Emby 媒体库递归刷新成功：ItemId=%s", item_id)
            continue
        errors.append(
            f"Emby 媒体库递归刷新失败: ItemId={item_id} -> {result.get('error')} "
            f"(HTTP {result.get('status')}, 尝试 {result.get('attempt')} 次)"
        )

    return {
        "parent_dirs": list(parent_dirs),
        "notified_dirs": notified_dirs,
        "refreshed_item_ids": refreshed_item_ids,
        "errors": errors,
    }
