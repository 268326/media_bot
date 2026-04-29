"""
TMDB API 模块
处理所有与 TMDB 相关的搜索和详情获取功能
"""
import logging
import aiohttp
from config import TMDB_API_KEY

TMDB_TIMEOUT = aiohttp.ClientTimeout(total=20, connect=10, sock_read=20)
TMDB_HEADERS = {"Accept": "application/json", "User-Agent": "MediaBot/1.0"}


def _get_result_title(item: dict) -> str:
    return item.get("title") or item.get("name") or ""


def _get_original_title(item: dict) -> str:
    return item.get("original_title") or item.get("original_name") or ""


def _normalize_search_result(item: dict, media_type: str) -> dict:
    result_type = item.get("media_type") if media_type == "multi" else media_type
    poster_path = item.get("poster_path")
    return {
        "tmdb_id": item.get("id"),
        "media_type": result_type,
        "title": _get_result_title(item),
        "overview": item.get("overview", "暂无简介"),
        "poster_url": f"https://image.tmdb.org/t/p/w500{poster_path}" if poster_path else None,
        "rating": item.get("vote_average", 0),
        "release_date": item.get("release_date") or item.get("first_air_date", "未知"),
    }


def _sort_search_results(keyword: str, results: list[dict]) -> list[dict]:
    keyword_lower = keyword.strip().lower()

    def sort_key(item: dict):
        title = _get_result_title(item).strip().lower()
        original_title = _get_original_title(item).strip().lower()
        exact = title == keyword_lower or original_title == keyword_lower
        contains = keyword_lower in title or keyword_lower in original_title
        release_date = item.get("release_date") or item.get("first_air_date") or ""
        popularity = item.get("popularity", 0) or 0
        vote_count = item.get("vote_count", 0) or 0
        return (
            0 if exact else 1,
            0 if contains else 1,
            -vote_count,
            -popularity,
            release_date,
        )

    return sorted(results, key=sort_key)


async def search_tmdb(keyword: str, media_type: str = "multi"):
    """
    使用 TMDB API 搜索影视内容
    
    Args:
        keyword: 搜索关键词
        media_type: 'movie' (电影), 'tv' (剧集), 'multi' (全部)
        
    Returns:
        list | None: 搜索结果列表 或 None
    """
    if not TMDB_API_KEY:
        logging.info("⚠️ 未配置 TMDB_API_KEY，跳过 TMDB API")
        return None
    
    try:
        url = f"https://api.tmdb.org/3/search/{media_type}"
        params = {
            "api_key": TMDB_API_KEY,
            "query": keyword,
            "language": "zh-CN",
            "page": 1
        }
        
        async with aiohttp.ClientSession(timeout=TMDB_TIMEOUT, headers=TMDB_HEADERS) as session:
            async with session.get(url, params=params) as response:
                if response.status == 200:
                    data = await response.json()
                    results = data.get("results", [])
                    
                    if results:
                        sorted_results = _sort_search_results(keyword, results)
                        search_results = [
                            _normalize_search_result(item, media_type)
                            for item in sorted_results[:8]
                        ]
                        logging.info("🔍 TMDB找到 %s 个相关结果，返回 %s 个候选项", len(results), len(search_results))
                        return search_results
                    else:
                        logging.info("⚠️ TMDB未找到结果")
                        return None
                else:
                    logging.error(f"❌ TMDB API请求失败: {response.status}")
                    return None
                    
    except Exception as e:
        logging.error(f"❌ TMDB API出错: {e}")
        return None


async def get_tmdb_details(tmdb_id: int, media_type: str):
    """
    通过 TMDB ID 获取详细信息
    
    Args:
        tmdb_id: TMDB ID
        media_type: 'movie' (电影) 或 'tv' (剧集)
        
    Returns:
        dict | None: 包含详细信息的字典
    """
    if not TMDB_API_KEY:
        logging.info("⚠️ 未配置 TMDB_API_KEY")
        return None
    
    try:
        url = f"https://api.tmdb.org/3/{media_type}/{tmdb_id}"
        params = {
            "api_key": TMDB_API_KEY,
            "language": "zh-CN"
        }
        
        async with aiohttp.ClientSession(timeout=TMDB_TIMEOUT, headers=TMDB_HEADERS) as session:
            async with session.get(url, params=params) as response:
                if response.status == 200:
                    data = await response.json()
                    poster_path = data.get("poster_path")
                    
                    tmdb_info = {
                        "tmdb_id": data.get("id"),
                        "media_type": media_type,
                        "title": data.get("title") or data.get("name"),
                        "overview": data.get("overview", "暂无简介"),
                        "poster_url": f"https://image.tmdb.org/t/p/w500{poster_path}" if poster_path else None,
                        "rating": data.get("vote_average", 0),
                        "release_date": data.get("release_date") or data.get("first_air_date", "未知")
                    }
                    
                    logging.info(f"✅ TMDB详情获取成功: {tmdb_info['title']} (ID: {tmdb_id})")
                    return tmdb_info
                else:
                    logging.error(f"❌ TMDB API请求失败: {response.status}")
                    return None
                    
    except Exception as e:
        logging.error(f"❌ TMDB API出错: {e}")
        return None
