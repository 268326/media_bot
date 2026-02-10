"""
HDHive HTTP API client (no browser).
Implements search/resource listing/unlock flow using token cookie and Next.js actions.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Any
from urllib.parse import quote_plus

import requests

from config import HDHIVE_BASE_URL, HDHIVE_ACTION_QUERY, HDHIVE_ACTION_FINAL
from hdhive_auth import build_authenticated_session
from utils import extract_points_from_text

logger = logging.getLogger(__name__)

HOME_STATE_TREE = (
    "%5B%22%22%2C%7B%22children%22%3A%5B%22(app)%22%2C%7B%22children%22%3A%5B%22__PAGE__%22%2C%7B%7D%2Cnull%2Cnull%5D%7D%2Cnull%2Cnull%5D%7D%2Cnull%2Cnull%2Ctrue%5D"
)
MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_7 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.7.3 Mobile/15E148 Safari/604.1"
)

POINTS_PATTERNS = [
    r'\\"points\\":\s*(\d+)',
    r'"points"\s*:\s*(\d+)',
    r'"points"\s*:\s*"(\d+)"',
    r"\\u0022points\\u0022\s*:\s*(\d+)",
    r'points["\s]*:["\s]*(\d+)',
]


def _force_utf8_text(resp: requests.Response) -> str:
    resp.encoding = "utf-8"
    return resp.text


def _parse_rsc_payload(text: str) -> Any:
    m = re.search(r"(?ms)^1:(.*?)(?=^\w+:|\Z)", text)
    if m:
        payload = m.group(1).strip()
        try:
            return json.loads(payload)
        except json.JSONDecodeError:
            return payload
    raise ValueError(f"Cannot parse RSC payload: {text[:200]}")


def _coerce_json_obj(value: Any) -> Any:
    cur = value
    for _ in range(2):
        if isinstance(cur, str):
            s = cur.strip()
            if s.startswith("{") or s.startswith("[") or (s.startswith('"') and s.endswith('"')):
                try:
                    cur = json.loads(s)
                    continue
                except json.JSONDecodeError:
                    return value
        break
    return cur


def _post_next_action(
    s: requests.Session,
    path: str,
    action_id: str,
    payload_list: list[Any],
    referer: str,
) -> Any:
    url = f"{HDHIVE_BASE_URL}{path}"
    headers = {
        "accept": "text/x-component",
        "content-type": "text/plain;charset=UTF-8",
        "next-action": action_id,
        "origin": HDHIVE_BASE_URL,
        "referer": referer,
        "user-agent": MOBILE_UA,
    }
    if path == "/":
        headers["next-router-state-tree"] = HOME_STATE_TREE
    r = s.post(url, headers=headers, data=json.dumps(payload_list), timeout=30)
    r.raise_for_status()
    return _parse_rsc_payload(_force_utf8_text(r))


def _extract_resource_objects(text: str) -> list[dict[str, Any]]:
    pattern = re.compile(
        r'(\{"id":\d+,"slug":"[a-f0-9]{32}".*?"is_forever_vip":(?:true|false)\})',
        re.DOTALL,
    )
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for m in pattern.finditer(text):
        try:
            obj = json.loads(m.group(1))
        except json.JSONDecodeError:
            continue
        slug = obj.get("slug")
        if isinstance(slug, str) and slug not in seen:
            seen.add(slug)
            out.append(obj)
    return out


def _to_points_status(resource: dict[str, Any]) -> str:
    if resource.get("is_unlocked"):
        return "已解锁"
    points = int(resource.get("unlock_points") or 0)
    return "免费" if points <= 0 else f"{points}积分"


def _normalize_resource(resource: dict[str, Any]) -> dict[str, Any]:
    website = resource.get("website")
    tags: list[str] = []
    if website:
        tags.append(str(website))
    for key in ("video_resolution", "source", "subtitle_language", "subtitle_type", "video_encode"):
        values = resource.get(key) or []
        if isinstance(values, list):
            tags.extend(str(v) for v in values if v)
    share_size = resource.get("share_size")
    if share_size:
        tags.append(str(share_size))
    uploader = ((resource.get("user") or {}).get("nickname") or "未知用户").strip() or "未知用户"
    return {
        "id": resource.get("slug"),
        "title": (resource.get("title") or "未知资源").strip() or "未知资源",
        "uploader": uploader,
        "points": _to_points_status(resource),
        "website": str(website or "").strip(),
        "tags": list(dict.fromkeys(tags))[:15],
    }


def _extract_website(payload: Any) -> str:
    if isinstance(payload, dict):
        for key in ("website", "drive", "drive_type", "provider"):
            val = payload.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
    return ""


def _fetch_media_slug_by_tmdb(s: requests.Session, media_type: str, tmdb_id: str | int) -> str:
    url = f"{HDHIVE_BASE_URL}/tmdb/{media_type}/{tmdb_id}"
    r = s.get(
        url,
        headers={
            "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "referer": f"{HDHIVE_BASE_URL}/",
            "user-agent": MOBILE_UA,
        },
        timeout=30,
    )
    r.raise_for_status()
    m = re.search(rf"/{media_type}/([a-f0-9]{{32}})", _force_utf8_text(r))
    if not m:
        raise RuntimeError(f"无法解析 {media_type} slug: tmdb_id={tmdb_id}")
    return m.group(1)


def _fetch_resources_for_slug(s: requests.Session, media_type: str, media_slug: str) -> list[dict[str, Any]]:
    t0 = time.monotonic()
    r = s.get(
        f"{HDHIVE_BASE_URL}/{media_type}/{media_slug}?_rsc=1",
        headers={
            "accept": "*/*",
            "rsc": "1",
            "referer": f"{HDHIVE_BASE_URL}/tmdb/{media_type}/0",
            "user-agent": MOBILE_UA,
        },
        timeout=30,
    )
    t1 = time.monotonic()
    if r.status_code == 200:
        objs = _extract_resource_objects(_force_utf8_text(r))
        t2 = time.monotonic()
        logger.info(
            "HDHive资源页(rsc): slug=%s status=%s http_ms=%s parse_ms=%s count=%s",
            media_slug,
            r.status_code,
            int((t1 - t0) * 1000),
            int((t2 - t1) * 1000),
            len(objs),
        )
        if objs:
            return objs

    r2 = s.get(
        f"{HDHIVE_BASE_URL}/{media_type}/{media_slug}",
        headers={
            "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "referer": f"{HDHIVE_BASE_URL}/",
            "user-agent": MOBILE_UA,
        },
        timeout=30,
    )
    r2.raise_for_status()
    objs = _extract_resource_objects(_force_utf8_text(r2))
    if not objs:
        raise RuntimeError("无法从页面提取资源列表")
    return objs


def _search_media(s: requests.Session, keyword: str, media_type: str) -> list[dict[str, Any]]:
    t0 = time.monotonic()
    encrypted_query = _post_next_action(
        s,
        "/",
        HDHIVE_ACTION_QUERY,
        [json.dumps({"query": keyword, "language": "zh-CN", "page": 1, "utctimestamp": int(time.time())})],
        referer=f"{HDHIVE_BASE_URL}/",
    )
    if not isinstance(encrypted_query, str):
        raise RuntimeError(f"搜索参数加密返回异常: {encrypted_query}")

    search_url = f"{HDHIVE_BASE_URL}/go-api/proxy/tmdb/3/search/{media_type}?query={quote_plus(encrypted_query)}"
    r = s.get(
        search_url,
        headers={
            "accept": "application/json, text/plain, */*",
            "referer": f"{HDHIVE_BASE_URL}/",
            "user-agent": MOBILE_UA,
        },
        timeout=30,
    )
    r.raise_for_status()
    j = r.json()
    if not j.get("success"):
        raise RuntimeError(f"搜索接口失败: {j}")

    decrypted = _post_next_action(
        s,
        "/",
        HDHIVE_ACTION_FINAL,
        [j.get("data")],
        referer=f"{HDHIVE_BASE_URL}/",
    )
    t1 = time.monotonic()
    decrypted = _coerce_json_obj(decrypted)
    if not isinstance(decrypted, dict):
        raise RuntimeError(f"搜索结果解密异常: {decrypted}")
    results = decrypted.get("results", []) or []
    logger.info(
        "HDHive搜索: keyword=%s type=%s cost_ms=%s results=%s",
        keyword,
        media_type,
        int((t1 - t0) * 1000),
        len(results),
    )
    return results


def _extract_unlock_points(check_json: dict[str, Any]) -> int:
    data = check_json.get("data")
    if isinstance(data, dict):
        val = data.get("unlock_points") or data.get("points")
        if isinstance(val, int):
            return val
        if isinstance(val, str) and val.isdigit():
            return int(val)
    msg = str(check_json.get("message") or "")
    points = extract_points_from_text(msg)
    return points if points is not None else 0


def _extract_user_points_from_html(text: str) -> int | None:
    for pattern in POINTS_PATTERNS:
        match = re.search(pattern, text)
        if match:
            return int(match.group(1))
    return None


def _resolve_or_unlock_resource(s: requests.Session, slug: str, dry_run: bool) -> dict[str, Any]:
    resource_path = f"/resource/115/{slug}"
    referer = f"{HDHIVE_BASE_URL}{resource_path}"
    query_token = _post_next_action(
        s,
        resource_path,
        HDHIVE_ACTION_QUERY,
        [json.dumps({"slug": slug, "utctimestamp": int(time.time())})],
        referer=referer,
    )

    check = s.get(
        f"{HDHIVE_BASE_URL}/go-api/customer/resources/{slug}/url",
        headers={"accept": "application/json, text/plain, */*", "origin": HDHIVE_BASE_URL, "referer": referer},
        params={"query": query_token},
        timeout=30,
    )
    try:
        check_json = check.json()
    except ValueError:
        check.raise_for_status()
        raise RuntimeError(f"url 接口返回非 JSON: {check.text[:300]}")

    if check.status_code >= 500:
        check.raise_for_status()

    if check_json.get("success"):
        encrypted_payload = check_json.get("data")
        if dry_run:
            final_data = _post_next_action(
                s,
                resource_path,
                HDHIVE_ACTION_FINAL,
                [encrypted_payload],
                referer=referer,
            )
            if isinstance(final_data, dict) and "full_url" not in final_data and final_data.get("url") and final_data.get("access_code"):
                final_data["full_url"] = f"{final_data['url']}?password={final_data['access_code']}"
            return {"mode": "already_unlocked", "result": final_data}
    else:
        if dry_run:
            return {"mode": "locked", "check": check_json}
        unlock_data = _post_next_action(
            s,
            resource_path,
            HDHIVE_ACTION_QUERY,
            [json.dumps({"utctimestamp": int(time.time())})],
            referer=referer,
        )
        unlock_resp = s.post(
            f"{HDHIVE_BASE_URL}/go-api/customer/resources/{slug}/unlock",
            headers={
                "accept": "application/json, text/plain, */*",
                "content-type": "application/json",
                "origin": HDHIVE_BASE_URL,
                "referer": referer,
            },
            json={"data": unlock_data},
            timeout=30,
        )
        unlock_resp.raise_for_status()
        unlock_json = unlock_resp.json()
        if not unlock_json.get("success"):
            raise RuntimeError(f"解锁失败: {unlock_json}")
        encrypted_payload = unlock_json.get("data")
        final_data = _post_next_action(
            s,
            resource_path,
            HDHIVE_ACTION_FINAL,
            [encrypted_payload],
            referer=referer,
        )
        if isinstance(final_data, dict) and "full_url" not in final_data and final_data.get("url") and final_data.get("access_code"):
            final_data["full_url"] = f"{final_data['url']}?password={final_data['access_code']}"
        return {"mode": "resolved", "result": final_data}

    raise RuntimeError("未得到有效资源结果")


def _sync_get_resources_by_tmdb_id(tmdb_id: str, media_type: str) -> list[dict[str, Any]]:
    with build_authenticated_session() as s:
        slug = str(tmdb_id)
        if not re.fullmatch(r"[a-f0-9]{32}", slug):
            slug = _fetch_media_slug_by_tmdb(s, media_type, tmdb_id)
        resources = _fetch_resources_for_slug(s, media_type, slug)
        return [_normalize_resource(x) for x in resources]


def _sync_search_resources(keyword: str, search_type: str = "all") -> list[dict[str, Any]]:
    media_type = "tv" if search_type == "tv" else "movie"
    with build_authenticated_session() as s:
        results = _search_media(s, keyword, media_type)
        if not results:
            return []
        first = results[0]
        tmdb_id = first.get("id")
        if not tmdb_id:
            return []
        slug = _fetch_media_slug_by_tmdb(s, media_type, tmdb_id)
        resources = _fetch_resources_for_slug(s, media_type, slug)
        return [_normalize_resource(x) for x in resources]


def _sync_fetch_download_link(resource_id: str) -> dict[str, Any] | None:
    with build_authenticated_session() as s:
        out = _resolve_or_unlock_resource(s, resource_id, dry_run=True)
        if out.get("mode") == "already_unlocked":
            result = out.get("result") or {}
            return {
                "link": result.get("full_url") or result.get("url"),
                "code": result.get("access_code") or "无",
                "website": _extract_website(result),
                "need_unlock": False,
            }
        check = out.get("check") or {}
        return {
            "need_unlock": True,
            "points": _extract_unlock_points(check),
            "message": str(check.get("message") or ""),
            "website": _extract_website(check.get("data")),
        }


def _sync_unlock_and_fetch(resource_id: str) -> dict[str, Any] | None:
    with build_authenticated_session() as s:
        out = _resolve_or_unlock_resource(s, resource_id, dry_run=False)
        result = out.get("result") or {}
        link = result.get("full_url") or result.get("url")
        if not link:
            return None
        return {
            "link": link,
            "code": result.get("access_code") or "无",
            "website": _extract_website(result),
        }


def _sync_get_user_points() -> int | None:
    with build_authenticated_session() as s:
        r = s.get(f"{HDHIVE_BASE_URL}/", headers={"accept": "text/html,*/*"}, timeout=30)
        r.raise_for_status()
        return _extract_user_points_from_html(_force_utf8_text(r))


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
    except Exception as e:
        logger.warning("查询积分失败: %s", e)
        return None
