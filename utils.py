"""
工具函数模块
提供通用的辅助功能
"""
import os
import re
import glob
import logging
from urllib.parse import urlparse, parse_qs


def parse_hdhive_link(text: str) -> dict:
    """
    解析 HDHive 链接
    
    Args:
        text: 包含链接的文本
        
    Returns:
        dict: {"type": "resource|tmdb|none", "id": "...", "media_type": "movie|tv|none"}
    """
    # 资源链接: https://hdhive.com/resource/115/[uuid] 或 https://hdhive.com/resource/[uuid]
    resource_match = re.search(r'hdhive\.com/resource/115/([a-f0-9-]+)', text)
    if resource_match:
        return {
            "type": "resource",
            "id": resource_match.group(1),
            "media_type": None,
            "resource_url": f"https://hdhive.com/resource/115/{resource_match.group(1)}",
        }

    resource_match = re.search(r'hdhive\.com/resource/([a-f0-9-]+)', text)
    if resource_match:
        return {
            "type": "resource",
            "id": resource_match.group(1),
            "media_type": None,
            "resource_url": f"https://hdhive.com/resource/{resource_match.group(1)}",
        }
    
    # TMDB页面链接: https://hdhive.com/tmdb/movie/12345 或 https://hdhive.com/tmdb/tv/67890
    tmdb_match = re.search(r'hdhive\.com/tmdb/(movie|tv)/(\d+)', text)
    if tmdb_match:
        return {
            "type": "tmdb",
            "media_type": tmdb_match.group(1),
            "id": tmdb_match.group(2)
        }
    
    # 简化链接: https://hdhive.com/movie/[uuid] 或 https://hdhive.com/tv/[uuid]
    short_match = re.search(r'hdhive\.com/(movie|tv)/([a-f0-9-]+)', text)
    if short_match:
        return {
            "type": "tmdb",
            "media_type": short_match.group(1),
            "id": short_match.group(2)
        }
    
    return {"type": "none", "id": None, "media_type": None}


def extract_115_link(url: str) -> tuple[str, str]:
    """
    从 URL 中提取 115 分享链接和提取码
    
    Args:
        url: 115 分享链接
        
    Returns:
        tuple: (完整链接, 提取码)
    """
    share_link = url.split('?')[0] if '?' in url else url
    parsed = urlparse(url)
    params = parse_qs(parsed.query)
    share_code = params.get('password', [None])[0]
    
    # 构建完整链接
    if share_code and '?' not in share_link:
        full_link = f"{share_link}?password={share_code}"
    else:
        full_link = share_link
    
    return full_link, share_code or "无"


def cleanup_debug_files():
    """清理调试截图文件"""
    debug_files = glob.glob("debug_*.png") + glob.glob("error_*.png")
    for file in debug_files:
        try:
            os.remove(file)
            logging.info(f"🗑️ 清除调试图片: {file}")
        except Exception as e:
            logging.warning(f"⚠️ 无法删除 {file}: {e}")


def extract_points_from_text(text: str) -> int | None:
    """
    从文本中提取积分数量
    
    Args:
        text: 包含积分信息的文本
        
    Returns:
        int | None: 积分数量
    """
    match = re.search(r'需要使用\s*(\d+)\s*积分', text)
    if match:
        return int(match.group(1))
    return None


def extract_user_id_from_link(href: str) -> str | None:
    """
    从用户链接中提取用户ID
    
    Args:
        href: 用户链接
        
    Returns:
        str | None: 用户ID
    """
    match = re.search(r'/user/(\d+)', href)
    if match:
        return match.group(1)
    return None


def detect_share_provider(link: str) -> tuple[str, str]:
    """
    根据分享链接识别网盘提供方

    Returns:
        tuple[str, str]: (provider_key, provider_name)
    """
    host = (urlparse(link).netloc or "").lower()

    if any(x in host for x in ("115.com", "115cdn.com", "anxia.com")):
        return "115", "115网盘"
    if "pan.baidu.com" in host:
        return "baidu", "百度网盘"
    if "123684.com" in host or "123865.com" in host or "123pan.com" in host:
        return "123", "123网盘"
    if "cloud.189.cn" in host:
        return "tianyi", "天翼云盘"
    if "pan.xunlei.com" in host:
        return "xunlei", "迅雷云盘"
    if any(x in host for x in ("aliyundrive.com", "alipan.com")):
        return "aliyun", "阿里云盘"
    if "pan.quark.cn" in host:
        return "quark", "夸克网盘"

    return "unknown", "网盘"


def is_115_share_link(link: str) -> bool:
    provider_key, _ = detect_share_provider(link)
    return provider_key == "115"


def detect_provider_by_website(website: str | None) -> tuple[str, str]:
    """
    根据接口返回的 website 字段识别网盘
    """
    w = str(website or "").strip().lower()
    mapping = {
        "115": ("115", "115网盘"),
        "123": ("123", "123网盘"),
        "baidu": ("baidu", "百度网盘"),
        "bd": ("baidu", "百度网盘"),
        "189": ("tianyi", "天翼云盘"),
        "tianyi": ("tianyi", "天翼云盘"),
        "xunlei": ("xunlei", "迅雷云盘"),
        "aliyun": ("aliyun", "阿里云盘"),
        "ali": ("aliyun", "阿里云盘"),
        "quark": ("quark", "夸克网盘"),
    }
    if w in mapping:
        return mapping[w]
    return "unknown", "网盘"
