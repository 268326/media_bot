"""
TMDB API 模块
处理所有与 TMDB 相关的搜索和详情获取功能
"""
import logging
import aiohttp
from config import TMDB_API_KEY


async def search_tmdb(keyword: str, media_type: str = "multi"):
    """
    使用 TMDB API 搜索影视内容
    
    Args:
        keyword: 搜索关键词
        media_type: 'movie' (电影), 'tv' (剧集), 'multi' (全部)
        
    Returns:
        dict | list | None: 单个结果(完全匹配) 或 多个结果列表 或 None
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
        
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=params) as response:
                if response.status == 200:
                    data = await response.json()
                    results = data.get("results", [])
                    
                    if results:
                        # 检查是否有完全匹配的结果
                        keyword_lower = keyword.lower()
                        for item in results:
                            title = (item.get("title") or item.get("name", "")).lower()
                            if keyword_lower == title:
                                # 完全匹配，返回单个结果
                                logging.info(f"✅ TMDB完全匹配: {item.get('title') or item.get('name')}")
                                return {
                                    "tmdb_id": item.get("id"),
                                    "media_type": item.get("media_type", media_type),
                                    "title": item.get("title") or item.get("name"),
                                    "overview": item.get("overview", "暂无简介"),
                                    "poster_url": f"https://image.tmdb.org/t/p/w500{item['poster_path']}" if item.get("poster_path") else None,
                                    "rating": item.get("vote_average", 0),
                                    "release_date": item.get("release_date") or item.get("first_air_date", "未知")
                                }
                        
                        # 没有完全匹配，返回多个结果（处理成统一格式）
                        logging.info(f"🔍 TMDB找到 {len(results)} 个相关结果")
                        search_results = []
                        for item in results[:5]:  # 最多返回5个
                            result_type = item.get("media_type") if media_type == "multi" else media_type
                            poster_path = item.get("poster_path")
                            
                            result_info = {
                                "tmdb_id": item.get("id"),
                                "media_type": result_type,
                                "title": item.get("title") or item.get("name"),
                                "overview": item.get("overview", "暂无简介"),
                                "poster_url": f"https://image.tmdb.org/t/p/w500{poster_path}" if poster_path else None,
                                "rating": item.get("vote_average", 0),
                                "release_date": item.get("release_date") or item.get("first_air_date", "未知")
                            }
                            search_results.append(result_info)
                        
                        return search_results  # 返回处理后的列表
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
        
        async with aiohttp.ClientSession() as session:
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
