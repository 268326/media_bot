"""
每日签到服务模块（HTTP接口版）
使用 Next-Action 接口执行签到，不依赖浏览器点击。
"""
from __future__ import annotations

import asyncio
import json
import os
import re
from typing import Any

import requests

from config import (
    CHECKIN_ACTION_ID,
    CHECKIN_GAMBLE,
    HDHIVE_BASE_URL,
)
from hdhive_auth import build_authenticated_session

HOME_STATE_TREE = (
    "%5B%22%22%2C%7B%22children%22%3A%5B%22(app)%22%2C%7B%22children%22%3A%5B%22__PAGE__%22%2C%7B%7D%2Cnull%2Cnull%5D%7D%2Cnull%2Cnull%5D%7D%2Cnull%2Cnull%2Ctrue%5D"
)
MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_7 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.7.3 Mobile/15E148 Safari/604.1"
)


def _extract_points_from_html(content: str) -> int | None:
    patterns = [
        r'\\"points\\":\s*(\d+)',
        r'"points"\s*:\s*(\d+)',
        r'"points"\s*:\s*"(\d+)"',
        r"\\u0022points\\u0022\s*:\s*(\d+)",
        r'points["\s]*:["\s]*(\d+)',
    ]
    for pattern in patterns:
        match = re.search(pattern, content)
        if match:
            return int(match.group(1))
    return None


def _read_points(s: requests.Session) -> int | None:
    r = s.get(f"{HDHIVE_BASE_URL}/", headers={"accept": "text/html,*/*"}, timeout=30)
    r.raise_for_status()
    r.encoding = "utf-8"
    return _extract_points_from_html(r.text)


def _parse_rsc_payload(text: str) -> Any:
    m = re.search(r"(?ms)^1:(.*?)(?=^\w+:|\Z)", text)
    if m:
        payload = m.group(1).strip()
        try:
            return json.loads(payload)
        except json.JSONDecodeError:
            return payload

    lines = [line.strip() for line in text.splitlines() if line.strip()]
    for line in lines:
        if line.startswith("1:"):
            payload = line[2:]
            try:
                return json.loads(payload)
            except json.JSONDecodeError:
                return payload
    return None


def _post_checkin(s: requests.Session) -> dict:
    headers = {
        "accept": "text/x-component",
        "content-type": "text/plain;charset=UTF-8",
        "origin": HDHIVE_BASE_URL,
        "referer": f"{HDHIVE_BASE_URL}/",
        "next-action": CHECKIN_ACTION_ID,
        "next-router-state-tree": HOME_STATE_TREE,
    }
    payload = [bool(CHECKIN_GAMBLE)]
    r = s.post(f"{HDHIVE_BASE_URL}/", headers=headers, data=json.dumps(payload), timeout=30)
    r.raise_for_status()
    r.encoding = "utf-8"
    parsed = _parse_rsc_payload(r.text)

    if isinstance(parsed, dict):
        return parsed
    return {"raw": str(parsed)}


def _parse_checkin_result(data: dict) -> tuple[bool, bool, str]:
    # 常见结构：{"error": {"success": false, "message": "...", "description": "..."}}
    if isinstance(data.get("error"), dict):
        err = data["error"]
        success = bool(err.get("success"))
        message = str(err.get("description") or err.get("message") or "签到失败")
    else:
        success = bool(data.get("success"))
        message = str(data.get("description") or data.get("message") or "签到请求已发送")

    already = ("已签到" in message) or ("明天再来" in message)
    return success or already, already, message


def _daily_check_in_sync() -> dict:
    with build_authenticated_session() as s:
        before_points = _read_points(s)
        payload = _post_checkin(s)
        success, already, message = _parse_checkin_result(payload)
        after_points = _read_points(s)

        return {
            "success": success,
            "already_checked_in": already,
            "message": message,
            "before_points": before_points,
            "after_points": after_points,
        }


async def daily_check_in() -> dict:
    """
    执行每日签到（接口方式）
    """
    try:
        return await asyncio.to_thread(_daily_check_in_sync)
    except Exception as e:
        return {
            "success": False,
            "already_checked_in": False,
            "message": str(e),
            "before_points": None,
            "after_points": None,
        }
