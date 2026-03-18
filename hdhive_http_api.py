"""
HDHive Open API client.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from hdhive_auth import OpenAPIError, build_authenticated_session, request_open_api_json

logger = logging.getLogger(__name__)


def _to_int(value: Any, default: int = 0) -> int:
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return default


def _to_points_status(resource: dict[str, Any]) -> str:
    if resource.get("is_unlocked"):
        return "已解锁"
    points = _to_int(resource.get("actual_unlock_points"), -1)
    if points < 0:
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

    remark = str(resource.get("remark") or "").strip()
    if remark:
        tags.append(remark)

    user = resource.get("user") or {}
    uploader = str(user.get("nickname") or "未知用户").strip() or "未知用户"

    return {
        "id": str(resource.get("slug") or "").strip(),
        "title": str(resource.get("title") or "未知资源").strip() or "未知资源",
        "uploader": uploader,
        "points": _to_points_status(resource),
        "website": str(resource.get("pan_type") or resource.get("website") or "").strip(),
        "tags": list(dict.fromkeys(tags))[:15],
    }


def _request_share_detail(session, resource_id: str) -> dict[str, Any]:
    payload = request_open_api_json(session, "GET", f"/shares/{resource_id}")
    data = payload.get("data") or {}
    if not isinstance(data, dict):
        raise RuntimeError("分享详情返回异常")
    return data


def _request_unlock(session, resource_id: str) -> dict[str, Any]:
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


def _sync_get_resources_by_tmdb_id(tmdb_id: str, media_type: str) -> list[dict[str, Any]]:
    with build_authenticated_session() as session:
        payload = request_open_api_json(session, "GET", f"/resources/{media_type}/{tmdb_id}")
        resources = payload.get("data") or []
        if not isinstance(resources, list):
            raise RuntimeError("资源列表返回异常")
        return [_normalize_resource(item) for item in resources if isinstance(item, dict)]


def _sync_search_resources(keyword: str, search_type: str = "all") -> list[dict[str, Any]]:
    _ = (keyword, search_type)
    logger.info("HDHive Open API 未提供关键词搜索接口，返回空结果并交由 TMDB 兜底")
    return []


def _sync_fetch_download_link(resource_id: str) -> dict[str, Any] | None:
    with build_authenticated_session() as session:
        try:
            detail = _request_share_detail(session, resource_id)
        except OpenAPIError as exc:
            if exc.status_code == 404:
                return None
            raise

        website = str(detail.get("pan_type") or "").strip()
        actual_points = _to_int(detail.get("actual_unlock_points"), -1)
        if actual_points < 0:
            actual_points = _to_int(detail.get("unlock_points"), 0)

        if actual_points > 0 and not bool(detail.get("is_free_for_user")):
            return {
                "need_unlock": True,
                "points": actual_points,
                "message": str(detail.get("unlock_message") or ""),
                "website": website,
            }

        result = _request_unlock(session, resource_id)
        link = str(result.get("full_url") or result.get("url") or "").strip()
        if not link:
            return None
        return {
            "link": link,
            "code": result.get("access_code") or "无",
            "website": website,
            "need_unlock": False,
        }


def _sync_unlock_and_fetch(resource_id: str) -> dict[str, Any] | None:
    with build_authenticated_session() as session:
        detail = _request_share_detail(session, resource_id)
        result = _request_unlock(session, resource_id)
        link = str(result.get("full_url") or result.get("url") or "").strip()
        if not link:
            return None
        return {
            "link": link,
            "code": result.get("access_code") or "无",
            "website": str(detail.get("pan_type") or "").strip(),
        }


def _sync_get_user_points() -> int | None:
    with build_authenticated_session() as session:
        payload = request_open_api_json(session, "GET", "/me")
        data = payload.get("data") or {}
        if not isinstance(data, dict):
            return None
        user_meta = data.get("user_meta") or {}
        if not isinstance(user_meta, dict):
            return None
        return _to_int(user_meta.get("points"), default=0)


async def get_resources_by_tmdb_id(tmdb_id: str, media_type: str) -> list:
    return await asyncio.to_thread(_sync_get_resources_by_tmdb_id, tmdb_id, media_type)


async def search_resources(keyword: str, search_type: str = "all") -> list:
    return await asyncio.to_thread(_sync_search_resources, keyword, search_type)


async def fetch_download_link(
    resource_id: str,
    user_id: int = None,
    keep_session: bool = False,
    start_url: str | None = None,
) -> dict | None:
    _ = (user_id, keep_session, start_url)
    return await asyncio.to_thread(_sync_fetch_download_link, resource_id)


async def unlock_and_fetch(resource_id: str, user_id: int = None) -> dict | None:
    _ = user_id
    return await asyncio.to_thread(_sync_unlock_and_fetch, resource_id)


async def get_user_points() -> int | None:
    try:
        return await asyncio.to_thread(_sync_get_user_points)
    except OpenAPIError as exc:
        logger.warning("查询积分失败: %s (%s)", exc, exc.code)
        return None
    except Exception as exc:
        logger.warning("查询积分失败: %s", exc)
        return None

