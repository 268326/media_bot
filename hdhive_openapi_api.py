"""
HDHive Open API 业务适配层（严格按官方 SDK / 官方文档）。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from hdhive_openapi_adapter import OpenAPIError, build_authenticated_client_context

logger = logging.getLogger(__name__)


def _to_int(value: Any, default: int = 0) -> int:
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return default


def _to_points_status(resource: dict[str, Any]) -> str:
    if bool(resource.get("is_unlocked")):
        return "已解锁"
    points = _to_int(resource.get("unlock_points"), 0)
    return "免费" if points <= 0 else f"{points}积分"


def _normalize_resource(resource: dict[str, Any]) -> dict[str, Any]:
    tags: list[str] = []
    for key in ("video_resolution", "source", "subtitle_language", "subtitle_type"):
        values = resource.get(key) or []
        if isinstance(values, list):
            tags.extend(str(v) for v in values if v)

    share_size = resource.get("share_size")
    if share_size:
        tags.append(str(share_size))

    user = resource.get("user") or {}
    uploader = str(user.get("nickname") or "未知用户").strip() or "未知用户"

    return {
        "id": str(resource.get("slug") or "").strip(),
        "title": str(resource.get("title") or "未知资源").strip() or "未知资源",
        "uploader": uploader,
        "points": _to_points_status(resource),
        "website": str(resource.get("pan_type") or "").strip(),
        "tags": list(dict.fromkeys(tags))[:15],
        "raw": resource,
    }


def _extract_share_data(payload: dict[str, Any]) -> dict[str, Any]:
    data = payload.get("data") or {}
    if not isinstance(data, dict):
        raise RuntimeError("分享详情返回异常")
    return data


def _sync_get_share_detail(resource_id: str) -> dict[str, Any]:
    with build_authenticated_client_context() as client:
        payload = client.get_share_detail(resource_id)
        return _extract_share_data(payload)


async def unlock_resource(
    resource_id: str,
    *,
    user_id: int | None = None,
    wait_callback=None,
) -> dict[str, Any]:
    from hdhive_openapi_unlock_service import hdhive_openapi_unlock_service

    return await hdhive_openapi_unlock_service.unlock(
        resource_id,
        user_id=user_id,
        wait_callback=wait_callback,
    )


def _sync_get_resources_by_tmdb_id(tmdb_id: str, media_type: str) -> list[dict[str, Any]]:
    with build_authenticated_client_context() as client:
        payload = client.query_resources(media_type, str(tmdb_id))
        resources = payload.get("data") or []
        if not isinstance(resources, list):
            raise RuntimeError("资源列表返回异常")
        return [_normalize_resource(item) for item in resources if isinstance(item, dict)]


def _sync_search_resources(keyword: str, search_type: str = "all") -> list[dict[str, Any]]:
    _ = (keyword, search_type)
    logger.info("HDHive OpenAPI 官方文档未提供关键词搜索接口，返回空结果并交由 TMDB 兜底")
    return []


def _extract_user_points(payload: dict[str, Any]) -> int | None:
    data = payload.get("data") or {}
    if not isinstance(data, dict):
        return None
    user_meta = data.get("user_meta") or {}
    if not isinstance(user_meta, dict):
        return None
    return _to_int(user_meta.get("points"), default=0)


def _sync_get_user_points() -> int | None:
    with build_authenticated_client_context() as client:
        payload = client.get_me()
        return _extract_user_points(payload)


async def get_resources_by_tmdb_id(tmdb_id: str, media_type: str) -> list:
    return await asyncio.to_thread(_sync_get_resources_by_tmdb_id, tmdb_id, media_type)


async def search_resources(keyword: str, search_type: str = "all") -> list:
    return await asyncio.to_thread(_sync_search_resources, keyword, search_type)


async def fetch_download_link(resource_id: str, user_id: int | None = None) -> dict | None:
    _ = user_id

    try:
        detail = await asyncio.to_thread(_sync_get_share_detail, resource_id)
    except OpenAPIError as exc:
        if exc.status_code == 404:
            return None
        raise

    website = str(detail.get("pan_type") or "").strip()
    unlock_points = _to_int(detail.get("unlock_points"), 0)
    already_unlocked = bool(detail.get("is_unlocked"))

    if unlock_points > 0 and not already_unlocked:
        return {
            "need_unlock": True,
            "points": unlock_points,
            "website": website,
            "resource_id": resource_id,
            "already_unlocked": False,
        }

    return {
        "need_unlock": False,
        "website": website,
        "resource_id": resource_id,
        "already_unlocked": already_unlocked,
    }


async def unlock_and_fetch(
    resource_id: str,
    user_id: int | None = None,
    wait_callback=None,
) -> dict | None:
    detail = await asyncio.to_thread(_sync_get_share_detail, resource_id)
    result = await unlock_resource(resource_id, user_id=user_id, wait_callback=wait_callback)
    link = str(result.get("full_url") or result.get("url") or "").strip()
    if not link:
        return None
    return {
        "link": link,
        "code": result.get("access_code") or "无",
        "website": str(detail.get("pan_type") or "").strip(),
        "already_owned": bool(result.get("already_owned")),
    }


async def get_user_points() -> int | None:
    try:
        return await asyncio.to_thread(_sync_get_user_points)
    except OpenAPIError as exc:
        logger.warning("查询积分失败: %s (%s)", exc, exc.code)
        return None
    except Exception as exc:
        logger.warning("查询积分失败: %s", exc)
        return None
