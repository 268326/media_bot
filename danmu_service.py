"""
Bilibili danmaku (XML) fetcher.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from urllib.parse import urlparse, parse_qs

import aiohttp


USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

logger = logging.getLogger(__name__)


class DanmuError(Exception):
    pass


@dataclass
class DanmuResult:
    filename: str
    content: bytes
    title: str
    cid: int


def _sanitize_filename(name: str) -> str:
    name = re.sub(r"[\\/:*?\"<>|]", "_", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name or "bilibili_danmaku"


def _extract_url_parts(url: str) -> dict:
    parsed = urlparse(url)
    path = parsed.path or ""
    query = parse_qs(parsed.query)

    bvid_match = re.search(r"/video/(BV[0-9A-Za-z]+)", path)
    if bvid_match:
        return {"type": "video", "bvid": bvid_match.group(1), "p": _read_page(query)}

    aid_match = re.search(r"/video/av(\d+)", path)
    if aid_match:
        return {"type": "video", "aid": aid_match.group(1), "p": _read_page(query)}

    ep_match = re.search(r"/bangumi/play/ep(\d+)", path)
    if ep_match:
        return {"type": "bangumi_ep", "ep_id": ep_match.group(1)}

    ss_match = re.search(r"/bangumi/play/ss(\d+)", path)
    if ss_match:
        return {"type": "bangumi_ss", "season_id": ss_match.group(1)}

    return {"type": "unknown"}


def _read_page(query: dict) -> int:
    try:
        return max(1, int(query.get("p", ["1"])[0]))
    except (TypeError, ValueError):
        return 1


async def _resolve_b23(session: aiohttp.ClientSession, url: str) -> str:
    if "b23.tv" not in url:
        return url
    try:
        logger.info("解析短链: %s", url)
        async with session.get(url, allow_redirects=True) as resp:
            resolved = str(resp.url)
            logger.info("短链解析完成: %s -> %s", url, resolved)
            return resolved
    except aiohttp.ClientError as exc:
        raise DanmuError(f"无法解析短链: {exc}") from exc


async def _fetch_json(session: aiohttp.ClientSession, url: str) -> dict:
    try:
        logger.debug("请求JSON: %s", url)
        async with session.get(url) as resp:
            if resp.status != 200:
                raise DanmuError(f"请求失败: HTTP {resp.status}")
            data = await resp.json(content_type=None)
            logger.debug("JSON响应成功: %s", url)
            return data
    except aiohttp.ClientError as exc:
        raise DanmuError(f"网络请求失败: {exc}") from exc


async def _fetch_danmaku_xml(session: aiohttp.ClientSession, cid: int) -> bytes:
    url = f"https://api.bilibili.com/x/v1/dm/list.so?oid={cid}"
    try:
        logger.info("请求弹幕XML: cid=%s", cid)
        async with session.get(url) as resp:
            if resp.status != 200:
                raise DanmuError(f"弹幕获取失败: HTTP {resp.status}")
            content = await resp.read()
            logger.info("弹幕XML获取完成: cid=%s size=%s", cid, len(content))
            return content
    except aiohttp.ClientError as exc:
        raise DanmuError(f"弹幕请求失败: {exc}") from exc


async def _fetch_video_cid(session: aiohttp.ClientSession, info: dict) -> tuple[int, str]:
    if "bvid" in info:
        url = f"https://api.bilibili.com/x/web-interface/view?bvid={info['bvid']}"
    else:
        url = f"https://api.bilibili.com/x/web-interface/view?aid={info['aid']}"

    logger.info("获取视频信息: %s", url)
    data = await _fetch_json(session, url)
    if data.get("code") != 0 or not data.get("data"):
        raise DanmuError("无法获取视频信息")

    payload = data["data"]
    pages = payload.get("pages") or []
    if not pages:
        raise DanmuError("视频分P信息缺失")

    p_index = info.get("p", 1) - 1
    if p_index < 0 or p_index >= len(pages):
        p_index = 0

    page = pages[p_index]
    cid = page.get("cid")
    if not cid:
        raise DanmuError("无法获取cid")

    title = payload.get("title") or "bilibili"
    part = page.get("part") or ""
    filename = title if len(pages) == 1 else f"{title}-{part}"
    filename = _sanitize_filename(filename)
    logger.info("解析视频cid成功: cid=%s filename=%s", cid, filename)
    return int(cid), filename


async def _fetch_bangumi_cid(session: aiohttp.ClientSession, info: dict) -> tuple[int, str]:
    if "ep_id" in info:
        url = f"https://api.bilibili.com/pgc/view/web/season?ep_id={info['ep_id']}"
    else:
        url = f"https://api.bilibili.com/pgc/view/web/season?season_id={info['season_id']}"

    logger.info("获取番剧信息: %s", url)
    data = await _fetch_json(session, url)
    if data.get("code") != 0 or not data.get("result"):
        raise DanmuError("无法获取番剧信息")

    result = data["result"]
    episodes = result.get("episodes") or []
    if not episodes:
        raise DanmuError("番剧分集信息缺失")

    episode = episodes[0]
    if "ep_id" in info:
        for ep in episodes:
            if str(ep.get("id")) == str(info["ep_id"]):
                episode = ep
                break

    cid = episode.get("cid")
    if not cid:
        raise DanmuError("无法获取cid")

    title = result.get("title") or "bilibili"
    ep_title = (
        episode.get("share_copy")
        or episode.get("long_title")
        or episode.get("title")
        or f"ep{episode.get('id')}"
    )
    filename = f"{title}-{ep_title}" if ep_title else title
    filename = _sanitize_filename(filename)
    logger.info("解析番剧cid成功: cid=%s filename=%s", cid, filename)
    return int(cid), filename


async def fetch_bilibili_danmaku_xml(url: str) -> DanmuResult:
    headers = {"User-Agent": USER_AGENT, "Referer": "https://www.bilibili.com/"}
    async with aiohttp.ClientSession(headers=headers) as session:
        logger.info("开始处理弹幕请求: %s", url)
        resolved_url = await _resolve_b23(session, url)
        info = _extract_url_parts(resolved_url)
        if info["type"] == "video":
            cid, filename = await _fetch_video_cid(session, info)
        elif info["type"] in ("bangumi_ep", "bangumi_ss"):
            cid, filename = await _fetch_bangumi_cid(session, info)
        else:
            raise DanmuError("无法识别链接类型")

        content = await _fetch_danmaku_xml(session, cid)
        logger.info("弹幕请求完成: cid=%s filename=%s", cid, filename)
        return DanmuResult(filename=filename, content=content, title=filename, cid=cid)
