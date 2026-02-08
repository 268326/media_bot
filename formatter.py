"""
消息格式化模块
负责 Telegram 消息的格式化、按钮构建、标签分类
"""
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from config import SA_URL, SA_PARENT_ID


def classify_tags(tags: list) -> dict:
    """
    分类标签
    
    Args:
        tags: 标签列表
        
    Returns:
        dict: 分类后的标签字典
    """
    quality_tags = []
    subtitle_tags = []
    format_tags = []
    encode_tags = []
    feature_tags = []
    size_tags = []
    
    for tag in tags:
        if any(q in tag for q in ['4K', '1080', '2160', '720', 'HDR', 'DV', 'SDR', '8K']):
            quality_tags.append(tag)
        elif any(s in tag for s in ['简中', '繁中', '英', '双语', '中文', '字幕']):
            subtitle_tags.append(tag)
        elif any(f in tag for f in ['蓝光原盘', 'BluRay', 'Blu-ray', 'ISO']):
            format_tags.append(tag)
        elif any(e in tag for e in ['REMUX', 'BDRip', 'WEB-DL', 'WEB', 'BluRayEncode', 'x264', 'x265', 'HEVC']):
            encode_tags.append(tag)
        elif any(ft in tag for ft in ['内封', '外挂', '内嵌']):
            feature_tags.append(tag)
        elif any(size_char in tag for size_char in ['G', 'M', 'T']) and any(c.isdigit() for c in tag):
            size_tags.append(tag)
    
    return {
        'quality': quality_tags,
        'subtitle': subtitle_tags,
        'format': format_tags,
        'encode': encode_tags,
        'feature': feature_tags,
        'size': size_tags
    }


def format_tags_inline(tags: list) -> str:
    """
    将标签格式化为单行文本
    
    Args:
        tags: 标签列表
        
    Returns:
        str: 格式化后的标签文本
    """
    if not tags:
        return ""
    
    classified = classify_tags(tags)
    tag_parts = []
    
    if classified['quality']:
        tag_parts.append(" / ".join(classified['quality']))
    if classified['encode']:
        tag_parts.append(" / ".join(classified['encode']))
    if classified['format']:
        tag_parts.append(" / ".join(classified['format']))
    if classified['subtitle']:
        tag_parts.append(" / ".join(classified['subtitle']))
    if classified['feature']:
        tag_parts.append(" / ".join(classified['feature']))
    if classified['size']:
        tag_parts.append(" / ".join(classified['size']))
    
    return "\n" + " | ".join(tag_parts) if tag_parts else ""


def build_resource_buttons(resources: list, media_type: str) -> InlineKeyboardMarkup:
    """
    构建资源选择按钮（横向排列）
    
    Args:
        resources: 资源列表
        media_type: 'movie' 或 'tv'
        
    Returns:
        InlineKeyboardMarkup: 按钮键盘
    """
    kb = InlineKeyboardMarkup(inline_keyboard=[])
    button_row = []
    
    for idx, res in enumerate(resources, 1):
        btn = InlineKeyboardButton(
            text=f"{idx}",
            callback_data=f"{media_type}_{idx}:{res['id']}"
        )
        button_row.append(btn)
        
        # 每5个按钮换一行，或者是最后一个资源
        if len(button_row) == 5 or idx == len(resources):
            kb.inline_keyboard.append(button_row)
            button_row = []
    
    return kb


def format_resource_list(resources: list, media_type: str, title: str = None) -> tuple[str, InlineKeyboardMarkup]:
    """
    格式化资源列表为 Telegram 消息
    
    Args:
        resources: 资源列表
        media_type: 'movie' 或 'tv'
        title: 可选的标题
        
    Returns:
        tuple: (消息文本, 按钮键盘)
    """
    type_emoji = "🎬" if media_type == "movie" else "📺"
    type_name = "电影" if media_type == "movie" else "剧集"
    
    if title:
        result_text = f"{type_emoji} <b>{title}</b>\n"
    else:
        result_text = f"{type_emoji} <b>{type_name} · {len(resources)} 项资源</b>\n"
    
    result_text += "─────────────────\n"
    
    # 构建资源卡片
    for idx, res in enumerate(resources, 1):
        result_text += f"\n<blockquote>\n<b>{idx}.</b> {res['title']}\n"
        
        # 用户和积分信息
        uploader = res.get('uploader', '未知用户')
        points_status = res.get('points', '未知')
        result_text += f"{uploader} · <code>{points_status}</code>\n"
        
        # 标签展示
        if res.get('tags'):
            tags_formatted = format_tags_inline(res['tags'])
            result_text += tags_formatted
        
        result_text += "\n</blockquote>"
    
    result_text += "\n─────────────────\n"
    result_text += "<b>轻触数字获取链接</b>"
    
    # 构建按钮
    kb = build_resource_buttons(resources, media_type)
    
    return result_text, kb


def format_download_link(link: str, code: str, resource_id: str = None) -> tuple[str, InlineKeyboardMarkup]:
    """
    格式化下载链接消息
    
    Args:
        link: 115 网盘链接
        code: 提取码
        resource_id: 资源ID（可选）
        
    Returns:
        tuple: (消息文本, 按钮键盘)
    """
    text = (
        f"✅ <b>提取成功</b>\n\n"
        f"<blockquote>\n"
        f"🔗 <a href='{link}'>115网盘链接</a>\n"
        f"🔑 提取码: <code>{code}</code>\n"
        f"</blockquote>"
    )
    
    # 构建按钮
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔗 打开115网盘", url=link)]
    ])
    
    # 如果配置了SA，添加添加到SA按钮
    if SA_URL and SA_PARENT_ID:
        kb.inline_keyboard.append([
            InlineKeyboardButton(text="📤 添加到SA", callback_data=f"send_to_group:{link}")
        ])
    
    return text, kb


def format_unlock_confirmation(resource_id: str, points: int, user_points: int) -> tuple[str, InlineKeyboardMarkup]:
    """
    格式化解锁确认消息
    
    Args:
        resource_id: 资源ID
        points: 需要的积分
        user_points: 用户当前积分
        
    Returns:
        tuple: (消息文本, 按钮键盘)
    """
    text = (
        f"🔓 <b>资源需要解锁</b>\n\n"
        f"🆔 <code>{resource_id}</code>\n"
        f"💰 需要积分: <code>{points}</code>\n"
        f"💳 当前积分: <code>{user_points}</code>\n"
        f"📊 解锁后剩余: <code>{user_points - points}</code>\n\n"
        f"是否确定解锁?"
    )
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ 确定解锁", callback_data=f"unlock:{resource_id}"),
            InlineKeyboardButton(text="❌ 取消", callback_data="cancel_unlock")
        ]
    ])
    
    return text, kb


def format_tmdb_info(tmdb_info: dict) -> str:
    """
    格式化 TMDB 信息
    
    Args:
        tmdb_info: TMDB 信息字典
        
    Returns:
        str: 格式化后的文本
    """
    title = tmdb_info.get('title', '未知')
    release_date = tmdb_info.get('release_date', '未知')
    rating = tmdb_info.get('rating', 0)
    overview = tmdb_info.get('overview', '暂无简介')
    
    text = f"<b>{title}</b>\n"
    
    if release_date and release_date != '未知':
        text += f"📅 {release_date}\n"
    
    if rating > 0:
        text += f"⭐️ {rating:.1f}/10\n"
    
    if overview:
        text += f"\n{overview[:200]}{'...' if len(overview) > 200 else ''}\n"
    
    return text


def format_points_message(points: int) -> str:
    """
    格式化积分查询消息
    
    Args:
        points: 积分数量
        
    Returns:
        str: 格式化后的文本
    """
    return (
        f"💰 <b>积分余额</b>\n\n"
        f"<blockquote>\n"
        f"当前积分: <code>{points}</code>\n"
        f"</blockquote>"
    )


def format_error_message(error_type: str, details: str = None) -> str:
    """
    格式化错误消息
    
    Args:
        error_type: 错误类型
        details: 详细信息
        
    Returns:
        str: 格式化后的错误消息
    """
    error_messages = {
        'no_results': '❌ 未找到资源',
        'no_resources': '❌ 该影片暂无资源',
        'fetch_failed': '❌ 提取失败，请检查链接是否有效',
        'insufficient_points': '❌ <b>积分不足</b>',
        'points_unavailable': '❌ <b>无法获取积分信息</b>',
        'permission_denied': '⛔️ <b>权限不足</b>\n\n抱歉，您没有权限使用此机器人。',
        'invalid_command': '❌ 命令格式错误',
    }
    
    base_message = error_messages.get(error_type, '❌ 发生错误')
    
    if details:
        return f"{base_message}\n\n{details}"
    
    return base_message


def format_help_message() -> str:
    """
    格式化帮助消息
    
    Returns:
        str: 帮助文本
    """
    return (
        "🤖 <b>Media Bot 使用指南</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "📋 <b>基本命令：</b>\n"
        "• <code>/hdt 剧名</code> - 搜索剧集\n"
        "• <code>/hdm 片名</code> - 搜索电影\n"
        "• <code>/points</code> - 查询积分余额\n"
        "• <code>/checkin</code> - 执行每日签到\n"
        "• <code>/danmu B站链接</code> - 下载B站弹幕XML\n"
        "• <code>/start</code> - 检查Bot状态\n"
        "• <code>/help</code> - 显示此帮助\n\n"
        "💡 <b>支持功能：</b>\n"
        "• 🔍 关键词搜索资源\n"
        "• 🔗 直接发送链接解析\n"
        "• 🎬 TMDB信息自动展示\n"
        "• 📤 一键添加到SA\n"
        "• 💰 积分资源自动解锁\n\n"
        "📝 <b>使用示例：</b>\n"
        "• <code>/hdt 狂飙</code>\n"
        "• <code>/hdm 流浪地球</code>\n"
        "• 直接发送资源链接\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━"
    )


def format_start_message(bot_id: int, user_id: int) -> str:
    """
    格式化启动消息
    
    Args:
        bot_id: Bot ID
        user_id: 用户ID
        
    Returns:
        str: 启动消息文本
    """
    return (
        "✅ <b>Bot运行正常</b>\n\n"
        f"🆔 Bot ID: <code>{bot_id}</code>\n"
        f"👤 你的ID: <code>{user_id}</code>\n\n"
        "💡 发送 /help 查看使用帮助"
    )
