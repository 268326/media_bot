"""
每日签到服务模块（基于官方 HDHive Python SDK 适配层）
"""
from __future__ import annotations

import asyncio

from config import CHECKIN_GAMBLE
from hdhive_openapi_adapter import OpenAPIError, build_authenticated_client_context


def _extract_points(payload: dict) -> int | None:
    data = payload.get("data") or {}
    if not isinstance(data, dict):
        return None
    user_meta = data.get("user_meta") or {}
    if not isinstance(user_meta, dict):
        return None
    points = user_meta.get("points")
    if isinstance(points, int):
        return points
    if isinstance(points, str) and points.strip().isdigit():
        return int(points.strip())
    return None


def _read_points(client) -> int | None:
    payload = client.get_me()
    return _extract_points(payload)


def _daily_check_in_sync() -> dict:
    with build_authenticated_client_context() as client:
        before_points = None
        after_points = None

        try:
            before_points = _read_points(client)
        except OpenAPIError:
            before_points = None

        payload = client.checkin(is_gambler=bool(CHECKIN_GAMBLE))

        data = payload.get("data") or {}
        message = str(
            (data.get("message") if isinstance(data, dict) else "")
            or payload.get("message")
            or "签到请求已发送"
        )
        checked_in = bool(data.get("checked_in")) if isinstance(data, dict) else False

        try:
            after_points = _read_points(client)
        except OpenAPIError:
            after_points = None

        return {
            "success": True,
            "already_checked_in": not checked_in,
            "message": message,
            "before_points": before_points,
            "after_points": after_points,
        }


async def daily_check_in() -> dict:
    try:
        return await asyncio.to_thread(_daily_check_in_sync)
    except Exception as exc:
        return {
            "success": False,
            "already_checked_in": False,
            "message": str(exc),
            "before_points": None,
            "after_points": None,
        }

