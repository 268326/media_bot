"""
HDHive 爬虫模块
负责资源搜索、解析、链接提取、解锁等核心爬虫功能

使用浏览器方法进行所有操作
"""
import asyncio
import logging
import re
import os
import traceback
from urllib.parse import urlparse, parse_qs
from playwright.async_api import async_playwright, Page
from browser import get_browser_context, check_and_login, ensure_logged_in
from utils import cleanup_debug_files, extract_115_link, extract_points_from_text, extract_user_id_from_link
from session_manager import session_manager
from config import (
    BROWSER_TIMEOUT, SHORT_WAIT, MEDIUM_WAIT, LONG_WAIT,
    HDHIVE_USER_ID
)


# ==================== 资源解析辅助函数 ====================

async def parse_resource_card(card_container, link):
    """
    异步解析单个资源卡片
    
    Args:
        card_container: 卡片容器元素
        link: 资源链接元素
        
    Returns:
        dict | None: 资源信息字典
    """
    try:
        href = await link.get_attribute("href")
        res_id = href.split("/")[-1]
        
        logging.info(f"🔍 开始解析资源 ID: {res_id}")
        
        card = card_container
        
        # 提取标题
        title = "未知资源"
        title_element = link.locator('p.MuiTypography-body2')
        if await title_element.count() > 0:
            title = await title_element.first.inner_text()
        
        if title == "未知资源" or not title.strip():
            title_element = link.locator('p.MuiTypography-root')
            if await title_element.count() > 0:
                for p in await title_element.all():
                    text = await p.inner_text()
                    if text and len(text.strip()) > 5:
                        title = text.strip()
                        break
        
        # 提取用户名
        uploader = "未知用户"
        user_span = card.locator('span.MuiTypography-caption[title]')
        if await user_span.count() > 0:
            uploader = await user_span.first.get_attribute("title")
        
        if uploader == "未知用户":
            user_span = card.locator('span.MuiTypography-caption')
            if await user_span.count() > 0:
                text = await user_span.first.inner_text()
                if text and text.strip():
                    uploader = text.strip()
        
        # 提取积分或免费状态
        points = "未知"
        all_chips = await card.locator('span.MuiChip-label').all()
        for chip in all_chips:
            chip_text = await chip.inner_text()
            chip_text = chip_text.strip()
            if '积分' in chip_text or chip_text == '免费' or chip_text == '已解锁':
                points = chip_text
                break
        
        # 提取标签
        tags = []
        tag_containers = link.locator('div.MuiBox-root > div.MuiBox-root')
        for tag_box in await tag_containers.all():
            try:
                tag_text = await tag_box.inner_text()
                tag_text = tag_text.strip()
                if (tag_text and 
                    1 <= len(tag_text) <= 30 and 
                    tag_text not in [title, uploader, points] and
                    tag_text not in tags):
                    tags.append(tag_text)
            except:
                pass
        
        # 如果没找到标签，尝试从所有div中提取
        if not tags:
            all_divs = link.locator('div.MuiBox-root')
            for div in await all_divs.all()[:20]:
                try:
                    div_text = await div.inner_text()
                    div_text = div_text.strip()
                    if (div_text and 
                        1 <= len(div_text) <= 30 and
                        div_text not in [title, uploader, points] and
                        div_text not in tags):
                        tags.append(div_text)
                except:
                    pass
        
        # 去重并限制数量
        tags = list(dict.fromkeys(tags))[:15]
        
        resource_info = {
            "id": res_id,
            "title": title,
            "uploader": uploader,
            "points": points,
            "tags": tags
        }
        
        tags_str = " ".join(tags[:8]) if tags else "无标签"
        logging.info(f"  📦 [{points}] {uploader} | {tags_str} | {title[:50]}")
        logging.info(f"✅ 资源 {res_id} 解析完成\n")
        
        return resource_info
        
    except Exception as e:
        logging.warning(f"⚠️ 解析资源卡片失败: {e}")
        logging.warning(traceback.format_exc())
        return None


# ==================== 主要爬虫函数 ====================

async def get_resources_by_tmdb_id(tmdb_id: str, media_type: str) -> list:
    """
    通过 TMDB ID 直接获取资源列表
    
    Args:
        tmdb_id: TMDB ID (数字) 或 UUID
        media_type: 'movie' 或 'tv'
        
    Returns:
        list: 资源信息列表
    """
    cleanup_debug_files()
    
    async with async_playwright() as p:
        browser, context = await get_browser_context(p)
        page = await context.new_page()
        
        results = []
        try:
            # 判断是TMDB ID（数字）还是直接链接UUID
            if str(tmdb_id).isdigit():
                target_url = f"https://hdhive.com/tmdb/{media_type}/{tmdb_id}"
            else:
                target_url = f"https://hdhive.com/{media_type}/{tmdb_id}"
            
            logging.info(f"🎯 直接导航到页面: {target_url}")
            await page.goto(target_url, wait_until="domcontentloaded", timeout=BROWSER_TIMEOUT)
            
            # 使用增强的登录检测和自动登录
            await ensure_logged_in(page, context, target_url)
            
            await page.wait_for_timeout(LONG_WAIT)
            
            # 再次检查是否被重定向到登录页
            current_url = page.url
            logging.info(f"✅ 当前页面: {current_url}")
            
            if "/login" in current_url:
                logging.warning(f"⚠️ 页面被重定向到登录页，重新登录...")
                await ensure_logged_in(page, context, target_url)
                current_url = page.url
            
            await page.screenshot(path="debug_01_tmdb_page.png")
            
            # 验证是否在详情页
            if "/tv/" not in current_url and "/movie/" not in current_url:
                logging.error(f"❌ 未能进入详情页，当前URL: {current_url}")
                return []
            
            # 解析资源列表
            await page.wait_for_timeout(MEDIUM_WAIT)
            logging.info("开始解析资源列表...")
            
            # 滚动页面以触发懒加载
            try:
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                await page.wait_for_timeout(1000)
                logging.info("✅ 已滚动页面")
            except:
                pass
            
            # 尝试等待资源区域加载
            try:
                # 等待可能的容器元素
                await page.wait_for_selector('a[href*="/resource/"]', timeout=5000)
                logging.info("✅ 资源链接已加载")
            except Exception as e:
                logging.warning(f"⚠️ 等待资源链接超时: {e}")
            
            # 截图看看当前状态
            await page.screenshot(path="debug_02_before_parse.png", full_page=True)
            logging.info("📸 截图已保存: debug_02_before_parse.png")
            
            # 获取资源链接 - 使用多种选择器
            resource_links = await page.locator('a[href^="/resource/"]').all()
            logging.info(f"📊 选择器 a[href^=\"/resource/\"] 找到 {len(resource_links)} 个")
            
            if not resource_links:
                # 尝试备用选择器
                resource_links = await page.locator('a[href*="/resource/"]').all()
                logging.info(f"📊 选择器 a[href*=\"/resource/\"] 找到 {len(resource_links)} 个")
            
            # Debug: 输出页面上所有的 a 标签
            if not resource_links:
                all_links = await page.locator('a[href]').all()
                logging.info(f"🔍 页面上共有 {len(all_links)} 个链接")
                
                # 输出前 20 个链接的 href
                for i, link in enumerate(all_links[:20]):
                    href = await link.get_attribute('href')
                    logging.info(f"  链接 {i+1}: {href}")
            
            logging.info(f"找到 {len(resource_links)} 个资源链接")
            
            # 通过资源链接找到它们的父级卡片容器
            resource_cards = []
            for link in resource_links[:8]:
                parent_container = link.locator('xpath=../..').first
                resource_cards.append((parent_container, link))
            
            logging.info(f"找到 {len(resource_cards)} 个资源卡片容器")
            
            if not resource_cards:
                logging.warning("⚠️ 资源列表为空")
                await page.screenshot(path="debug_03_empty_list.png")
                return []
            else:
                await page.screenshot(path="debug_03_resource_list_found.png")
                logging.info("📸 找到资源列表")
            
            # 并发解析所有资源卡片
            parse_tasks = [parse_resource_card(card, link) for card, link in resource_cards]
            parsed_results = await asyncio.gather(*parse_tasks)
            
            # 过滤掉解析失败的结果
            results = [r for r in parsed_results if r is not None]
            
            logging.info(f"✅ 找到 {len(results)} 个资源")
            return results

        except Exception as e:
            logging.error(f"❌ 获取资源出错: {e}")
            await page.screenshot(path="error_get_resources.png")
            return []
        finally:
            await browser.close()


async def search_resources(keyword: str, search_type: str = "all") -> list:
    """
    通过关键词搜索资源
    
    Args:
        keyword: 搜索关键词
        search_type: 'tv' (剧集), 'movie' (电影), 'all' (默认)
        
    Returns:
        list: 资源信息列表
    """
    cleanup_debug_files()
    
    async with async_playwright() as p:
        browser, context = await get_browser_context(p)
        page = await context.new_page()
        
        results = []
        try:
            # 1. 访问首页与登录检查
            logging.info("正在访问首页...")
            await page.goto("https://hdhive.com/", wait_until="domcontentloaded", timeout=BROWSER_TIMEOUT)
            await page.screenshot(path="debug_01_homepage.png")
            
            try:
                await page.wait_for_load_state("networkidle", timeout=5000)
            except:
                pass
            
            # 使用增强的登录检测
            await ensure_logged_in(page, context, "https://hdhive.com/")
            
            await page.wait_for_timeout(LONG_WAIT)
            await page.screenshot(path="debug_02_after_login.png")

            # 2. 打开搜索框
            logging.info("正在打开搜索框...")
            search_btn = page.locator('button.MuiButton-root:has-text("前往搜索")')
            if await search_btn.count() > 0:
                logging.info("找到前往搜索按钮")
                await search_btn.first.click()
                await page.wait_for_timeout(SHORT_WAIT)
            
            await page.screenshot(path="debug_04_after_search_button.png")
            
            # 等待搜索输入框出现
            try:
                await page.wait_for_selector('input[placeholder="搜索剧集..."]', timeout=5000)
                search_input = page.locator('input[placeholder="搜索剧集..."]').first
            except:
                await page.wait_for_selector('input.MuiInputBase-input', timeout=5000)
                search_input = page.locator('input.MuiInputBase-input').first

            # 3. 输入关键词
            logging.info(f"正在输入关键词: {keyword}")
            await search_input.click()
            await page.wait_for_timeout(300)
            await search_input.fill(keyword)
            await page.wait_for_timeout(MEDIUM_WAIT)
            
            input_value = await search_input.input_value()
            logging.info(f"✅ 已输入: {input_value}")
            await page.screenshot(path="debug_05_after_input_keyword.png")
            
            # 4. 应用筛选
            if search_type == "tv":
                chip = page.locator('div.MuiChip-root.MuiChip-outlined:has(span.MuiChip-label:text("剧集"))')
                if await chip.count() > 0:
                    await chip.first.click()
                    await page.wait_for_timeout(SHORT_WAIT)
            elif search_type == "movie":
                chip = page.locator('div.MuiChip-root.MuiChip-outlined:has(span.MuiChip-label:text("电影"))')
                if await chip.count() > 0:
                    await chip.first.click()
                    await page.wait_for_timeout(SHORT_WAIT)
            
            await page.screenshot(path="debug_06_after_filter.png")

            # 5. 执行搜索
            logging.info("正在执行搜索...")
            await search_input.press('Enter')
            await page.wait_for_timeout(LONG_WAIT)
            await page.screenshot(path="debug_07_search_results.png")

            # 6. 定位搜索结果
            selector = f'div.MuiDialogContent-root a[href^="/tmdb/{search_type}/"]' if search_type != "all" else 'div.MuiDialogContent-root a[href^="/tmdb/"]'
            
            try:
                await page.wait_for_selector(selector, timeout=5000)
                logging.info(f"✅ 找到搜索结果")
            except Exception as e:
                logging.error(f"❌ 未找到搜索结果: {e}")
                await page.screenshot(path="error_no_results.png")
                return []
            
            # 获取所有搜索结果，找最匹配的
            all_results = await page.locator(selector).all()
            logging.info(f"🔍 找到 {len(all_results)} 个搜索结果")
            
            # 选择最佳匹配
            keyword_lower = keyword.lower()
            best_match_href = None
            best_match_score = -1
            
            for idx, result in enumerate(all_results[:10]):
                try:
                    href = await result.get_attribute('href')
                    if not href or not href.startswith('/tmdb/'):
                        continue
                    
                    # 获取标题
                    img = result.locator('img[alt]')
                    if await img.count() > 0:
                        result_text = await img.first.get_attribute('alt')
                    else:
                        h6 = result.locator('h6.MuiTypography-subtitle1')
                        if await h6.count() > 0:
                            result_text = await h6.first.inner_text()
                        else:
                            result_text = await result.inner_text()
                    
                    if not result_text.strip():
                        continue
                    
                    # 计算匹配分数
                    result_text_lower = result_text.lower()
                    score = 0
                    
                    if keyword_lower == result_text_lower:
                        score = 100
                    elif keyword_lower in result_text_lower:
                        score = 80
                    elif result_text_lower in keyword_lower:
                        score = 60
                    else:
                        keyword_words = set(keyword_lower.split())
                        result_words = set(result_text_lower.split())
                        common_words = keyword_words & result_words
                        if common_words:
                            score = len(common_words) * 10
                    
                    if score > best_match_score:
                        best_match_score = score
                        best_match_href = href
                        logging.info(f"🎯 更新最佳匹配 (得分={score}): {result_text[:50]}")
                    
                except Exception as e:
                    logging.warning(f"⚠️ 检查结果 {idx+1} 失败: {e}")
                    continue
            
            if not best_match_href or best_match_score <= 0:
                logging.error("❌ 没有找到任何匹配的结果")
                return []
            
            # 7. 导航到详情页
            full_url = f"https://hdhive.com{best_match_href}"
            logging.info(f"🔗 导航到: {full_url}")
            await page.goto(full_url, wait_until="domcontentloaded", timeout=BROWSER_TIMEOUT)
            await page.wait_for_timeout(LONG_WAIT)
            
            current_url = page.url
            logging.info(f"✅ 当前页面: {current_url}")
            await page.screenshot(path="debug_09_detail_page.png")
            
            if "/tv/" not in current_url and "/movie/" not in current_url:
                logging.error(f"❌ 未能进入详情页")
                return []
            
            # 8. 解析资源列表
            await page.wait_for_timeout(MEDIUM_WAIT)
            logging.info("开始解析资源列表...")
            
            resource_links = await page.locator('a[href^="/resource/"]').all()
            logging.info(f"找到 {len(resource_links)} 个资源链接")
            
            if not resource_links:
                logging.warning("⚠️ 资源列表为空")
                await page.screenshot(path="debug_11_empty_list.png")
                return []
            
            await page.screenshot(path="debug_11_resource_list_found.png")
            
            # 解析资源卡片
            for card in resource_links[:8]:
                try:
                    href = await card.get_attribute("href")
                    res_id = href.split("/")[-1]
                    
                    # 提取标题
                    title = "未知资源"
                    title_element = card.locator('p.MuiTypography-body2')
                    if await title_element.count() > 0:
                        title = await title_element.first.inner_text()
                    
                    # 提取用户名
                    uploader = "未知用户"
                    user_span = card.locator('span.MuiTypography-caption[title]')
                    if await user_span.count() > 0:
                        uploader = await user_span.first.get_attribute("title")
                    
                    # 提取积分
                    points = "未知"
                    all_chips = await card.locator('span.MuiChip-label').all()
                    for chip in all_chips:
                        chip_text = await chip.inner_text()
                        if '积分' in chip_text or chip_text.strip() == '免费':
                            points = chip_text.strip()
                            break
                    
                    # 提取标签
                    tags = []
                    tag_containers = card.locator('div.MuiBox-root > div.MuiBox-root')
                    for tag_box in await tag_containers.all():
                        try:
                            tag_text = await tag_box.inner_text()
                            tag_text = tag_text.strip()
                            if (tag_text and 1 <= len(tag_text) <= 30 and 
                                tag_text not in [title, uploader, points] and
                                tag_text not in tags):
                                tags.append(tag_text)
                        except:
                            pass
                    
                    tags = list(dict.fromkeys(tags))[:15]
                    
                    resource_info = {
                        "id": res_id,
                        "title": title,
                        "uploader": uploader,
                        "points": points,
                        "tags": tags
                    }
                    
                    results.append(resource_info)
                    logging.info(f"  📦 [{points}] {uploader} | {title[:50]}")
                    
                except Exception as e:
                    logging.warning(f"⚠️ 解析资源失败: {e}")
                    continue
            
            logging.info(f"✅ 找到 {len(results)} 个资源")
            return results

        except Exception as e:
            logging.error(f"❌ 搜索出错: {e}")
            await page.screenshot(path="error_search_fail.png")
            return []
        finally:
            await browser.close()


async def fetch_download_link(
    resource_id: str,
    user_id: int = None,
    keep_session: bool = False,
    start_url: str | None = None,
) -> dict | None:
    """
    提取资源下载链接（使用浏览器方法）
    
    Args:
        resource_id: 资源ID
        user_id: 用户ID（用于会话管理）
        keep_session: 是否保持会话（用于后续解锁）
        
    Returns:
        dict | None: {"link": "...", "code": "..."} 或 {"need_unlock": True, "points": X, "session_id": "..."}
    """
    logging.info(f"🌐 使用浏览器方法提取链接: {resource_id}")
    url = start_url or f"https://hdhive.com/resource/{resource_id}"
    session_id = f"{user_id}:{resource_id}" if user_id else resource_id
    
    # 如果需要保持会话，使用 SessionManager
    if keep_session and user_id:
        await session_manager.start()
        session = await session_manager.create_session(session_id)
        page = session.page
        context = session.context
        should_close = False
    else:
        # 传统模式：独立浏览器会话
        async with async_playwright() as p:
            browser, context = await get_browser_context(p)
            page = await context.new_page()
            should_close = True
    
    try:
        logging.info(f"提取中: {url}")
        await page.goto(url, wait_until="domcontentloaded", timeout=BROWSER_TIMEOUT)
        
        # 使用增强的登录检测
        await ensure_logged_in(page, context, url)

        await page.wait_for_timeout(LONG_WAIT)
        await page.screenshot(path="debug_resource_page.png")
        
        # 检查是否自动跳转到115（包括 /resource/115/ 中转）
        current_url = page.url
        if '/resource/115/' in current_url:
            logging.info("🔁 检测到 /resource/115/ 中转页，等待跳转到115链接...")
            try:
                await page.wait_for_url(
                    lambda u: "115.com/s/" in u or "115cdn.com/s/" in u,
                    timeout=10000
                )
            except Exception:
                pass
            current_url = page.url

        if '115.com/s/' in current_url or '115cdn.com/s/' in current_url:
            logging.info("✅ 页面已自动跳转到115链接")
            full_link, code = extract_115_link(current_url)
            
            # 成功获取链接，关闭会话
            if keep_session:
                await session_manager.close_session(session_id)
            
            return {"link": full_link, "code": code}
        
        # 检查是否需要积分解锁
        unlock_text_element = page.locator('div.MuiBox-root:has-text("需要使用")')
        if await unlock_text_element.count() > 0:
            unlock_text = await unlock_text_element.first.inner_text()
            logging.info(f"⚠️ 需要积分解锁: {unlock_text}")
            
            required_points = extract_points_from_text(unlock_text)
            if required_points:
                # 需要解锁，保持会话
                logging.info(f"💾 保持浏览器会话以待解锁: {session_id}")
                return {
                    "need_unlock": True,
                    "points": required_points,
                    "resource_id": resource_id,
                    "session_id": session_id  # 返回会话ID
                }
        
        # 查找115链接
        share_link = None
        share_code = None
        
        all_links = await page.locator('a[href*="115"]').all()
        logging.info(f"找到 {len(all_links)} 个包含115的链接")
        
        for link in all_links:
            href = await link.get_attribute('href')
            if href and ('115.com/s/' in href or '115cdn.com/s/' in href):
                full_link, code = extract_115_link(href)
                logging.info(f"✅ 提取成功: {full_link}")
                
                # 成功获取链接，关闭会话
                if keep_session:
                    await session_manager.close_session(session_id)
                
                return {"link": full_link, "code": code}
        
        logging.warning("未找到115链接")
        await page.screenshot(path="error_no_115_link.png")
        
        # 失败，关闭会话
        if keep_session:
            await session_manager.close_session(session_id)
        
        return None
        
    except Exception as e:
        logging.error(f"❌ 提取出错: {e}")
        try:
            await page.screenshot(path="error_fetch_link.png")
        except:
            pass
        
        # 出错，关闭会话
        if keep_session:
            await session_manager.close_session(session_id)
        
        return None
    finally:
        # 只有在非会话模式下才关闭浏览器
        if should_close and 'browser' in locals():
            await browser.close()


async def unlock_and_fetch(resource_id: str, user_id: int = None) -> dict | None:
    """
    解锁资源并获取链接（使用浏览器方法）
    
    Args:
        resource_id: 资源ID
        user_id: 用户ID（用于获取会话）
        
    Returns:
        dict | None: {"link": "...", "code": "..."}
    """
    logging.info(f"🌐 使用浏览器方法解锁资源: {resource_id}")
    session_id = f"{user_id}:{resource_id}" if user_id else resource_id
    
    # 尝试获取已有会话
    session = await session_manager.get_session(session_id)
    
    browser = None
    should_close_browser = False
    
    if session:
        # 使用已有会话
        logging.info(f"♻️ 使用现有会话解锁: {session_id}")
        page = session.page
        context = session.context
        should_close_session = True
    else:
        # 会话不存在，创建新的
        logging.warning(f"⚠️ 会话不存在，创建新会话: {session_id}")
        p = await async_playwright().start()
        browser, context = await get_browser_context(p)
        page = await context.new_page()
        should_close_session = False
        should_close_browser = True
    
    try:
        url = f"https://hdhive.com/resource/{resource_id}"
        
        # 如果是新会话，需要导航
        if not session:
            logging.info(f"🔓 正在导航到资源页: {url}")
            await page.goto(url, wait_until="domcontentloaded", timeout=BROWSER_TIMEOUT)
            
            # 使用增强的登录检测
            await ensure_logged_in(page, context, url)

            # 增加等待时间，确保页面完全加载
            logging.info("⏳ 等待页面加载完成...")
            await page.wait_for_timeout(LONG_WAIT)
            
            # 尝试等待网络空闲
            try:
                await page.wait_for_load_state("networkidle", timeout=5000)
                logging.info("✅ 页面网络已空闲")
            except:
                logging.info("⚠️ 网络空闲等待超时，继续执行")
                pass
        else:
            # 使用已有会话，页面已经在资源页了
            logging.info(f"✅ 页面已在资源页: {page.url}")
            # 小小等待确保状态稳定
            await page.wait_for_timeout(SHORT_WAIT)
        
        # 查找解锁按钮 - 使用更可靠的等待策略
        logging.info("🔍 步骤1: 等待并查找积分支付按钮...")
        
        # 先等待按钮出现（最多等待10秒）
        try:
            # 等待按钮元素出现在DOM中
            await page.wait_for_selector(
                'button.MuiLoadingButton-root:has-text("确定解锁")',
                state="attached",
                timeout=10000
            )
            logging.info("✅ 按钮已出现在DOM中")
        except Exception as e:
            logging.error(f"❌ 按钮等待超时: {e}")
            try:
                await page.screenshot(path="error_button_not_found.png", timeout=5000)
            except:
                logging.warning("⚠️ 截图失败")
            # 尝试备用选择器
            try:
                await page.wait_for_selector(
                    'button:has-text("确定解锁")',
                    state="attached",
                    timeout=5000
                )
                logging.info("✅ 使用备用选择器找到按钮")
            except:
                logging.error("❌ 备用选择器也未找到按钮")
                try:
                    await page.screenshot(path="error_no_unlock_button.png", timeout=5000)
                except:
                    logging.warning("⚠️ 截图失败")
                return None
        
        # 从父容器 mui-7texn5 中查找按钮（更精确的定位）
        parent_container = page.locator('div.mui-7texn5')
        
        if await parent_container.count() > 0:
            logging.info("✅ 找到按钮父容器")
            unlock_btn = parent_container.locator('button.MuiLoadingButton-root:has-text("确定解锁")')
        else:
            logging.info("⚠️ 父容器未找到，使用备用选择器")
            # 备用：直接查找按钮
            unlock_btn = page.locator('button.MuiLoadingButton-root:has-text("确定解锁")')
        
        btn_count = await unlock_btn.count()
        logging.info(f"找到 {btn_count} 个解锁按钮")
        
        if btn_count == 0:
            logging.error("❌ 未找到'确定解锁'按钮")
            await page.screenshot(path="error_no_unlock_button.png")
            return None
        
        # 步骤2: 等待按钮可见并可点击
        logging.info("⏳ 步骤2: 等待按钮可用...")
        try:
            await unlock_btn.first.wait_for(state="visible", timeout=5000)
            logging.info("✅ 按钮已可见")
        except Exception as e:
            logging.warning(f"⚠️ 按钮等待超时，尝试继续: {e}")
        
        # 截图：点击前（如果失败不影响主流程）
        try:
            await page.screenshot(path="debug_before_unlock_click.png", timeout=5000)
            logging.info("📸 点击前截图已保存")
        except:
            logging.warning("⚠️ 截图失败，继续执行")
        
        # 步骤3: 点击按钮（使用JS强制点击避免被遮挡）
        logging.info("🖱️ 步骤3: 点击解锁按钮...")
        await unlock_btn.first.evaluate("element => element.click()")
        logging.info("✅ 已点击解锁按钮")
        
        # 步骤4: 等待页面变化（URL变化或内容加载）
        logging.info("⏳ 步骤4: 等待页面响应...")
        await page.wait_for_timeout(LONG_WAIT)  # 给页面一点时间响应
        try:
            await page.wait_for_url(
                lambda u: "/resource/115/" in u or "115.com/s/" in u or "115cdn.com/s/" in u,
                timeout=8000
            )
        except Exception:
            pass
        
        # 步骤5: 获取当前URL（可能已经变成115链接）
        current_url = page.url
        if '/resource/115/' in current_url:
            logging.info("🔁 检测到 /resource/115/ 中转页，等待跳转到115链接...")
            try:
                await page.wait_for_url(
                    lambda u: "115.com/s/" in u or "115cdn.com/s/" in u,
                    timeout=10000
                )
            except Exception:
                pass
            current_url = page.url
        logging.info(f"🔗 步骤5: 获取115分享链接...")
        logging.info(f"当前URL: {current_url}")
        
        # 验证是否是115页面
        if '115.com/s/' in current_url or '115cdn.com/s/' in current_url:
            full_link, code = extract_115_link(current_url)
            logging.info(f"✅ 解锁成功！链接: {full_link}")
            return {"link": full_link, "code": code}
        
        # 尝试从页面中提取115链接（有些场景不自动跳转）
        all_links = await page.locator('a[href*="115"]').all()
        logging.info(f"找到 {len(all_links)} 个包含115的链接")
        for link in all_links:
            href = await link.get_attribute('href')
            if href and ('115.com/s/' in href or '115cdn.com/s/' in href):
                full_link, code = extract_115_link(href)
                logging.info(f"✅ 解锁成功（页面链接）: {full_link}")
                return {"link": full_link, "code": code}
        
        # 仍未跳转，手动访问中转页
        intermediate_url = f"https://hdhive.com/resource/115/{resource_id}"
        logging.info(f"🔁 手动访问中转页: {intermediate_url}")
        try:
            await page.goto(intermediate_url, wait_until="domcontentloaded", timeout=BROWSER_TIMEOUT)
            await page.wait_for_url(
                lambda u: "115.com/s/" in u or "115cdn.com/s/" in u,
                timeout=10000
            )
        except Exception:
            pass
        
        current_url = page.url
        logging.info(f"🔗 中转后URL: {current_url}")
        if '115.com/s/' in current_url or '115cdn.com/s/' in current_url:
            full_link, code = extract_115_link(current_url)
            logging.info(f"✅ 解锁成功（中转）: {full_link}")
            return {"link": full_link, "code": code}
        
        logging.error(f"❌ 未跳转到115页面")
        try:
            await page.screenshot(path="error_unlock_no_redirect.png", timeout=5000)
        except:
            logging.warning("⚠️ 截图失败")
        return None
            
    except Exception as e:
        logging.error(f"❌ 解锁过程出错: {e}")
        logging.error(traceback.format_exc())
        try:
            await page.screenshot(path="error_unlock_exception.png")
        except:
            logging.warning("⚠️ 截图失败（可能页面已关闭）")
        return None
    finally:
        # 清理会话
        if should_close_session:
            await session_manager.close_session(session_id)
        elif should_close_browser and browser:
            try:
                await browser.close()
                logging.info("✅ 浏览器已关闭")
            except Exception as e:
                logging.warning(f"⚠️ 关闭浏览器失败: {e}")


async def get_user_points() -> int | None:
    """
    获取当前用户的积分信息
    
    Returns:
        int | None: 用户积分
    """
    async with async_playwright() as p:
        browser, context = await get_browser_context(p)
        page = await context.new_page()
        try:
            user_id = HDHIVE_USER_ID
            
            if not user_id:
                logging.info("未配置HDHIVE_USER_ID，尝试从页面获取...")
                await page.goto("https://hdhive.com/", wait_until="domcontentloaded", timeout=BROWSER_TIMEOUT)
                
                # 使用增强的登录检测
                await ensure_logged_in(page, context, "https://hdhive.com/")
                
                await page.wait_for_timeout(SHORT_WAIT)
                
                # 从页面中获取用户ID
                user_links = await page.locator('a[href*="/user/"]').all()
                for link in user_links:
                    href = await link.get_attribute('href')
                    if href:
                        user_id = extract_user_id_from_link(href)
                        if user_id:
                            logging.info(f"📝 从页面提取到用户ID: {user_id}")
                            break
                
                if not user_id:
                    logging.error("❌ 无法获取用户ID")
                    await page.screenshot(path="error_no_user_id.png")
                    return None
            else:
                logging.info(f"📝 使用配置的用户ID: {user_id}")
            
            # 访问用户页面
            user_url = f"https://hdhive.com/user/{user_id}"
            logging.info(f"🌐 正在访问用户页面: {user_url}")
            await page.goto(user_url, wait_until="domcontentloaded", timeout=BROWSER_TIMEOUT)
            
            # 从页面内容中提取积分
            logging.info("📥 正在从页面提取积分信息...")
            page_content = await page.content()
            
            # 多种正则模式
            patterns = [
                r'\\"points\\":\s*(\d+)',
                r'"points"\s*:\s*(\d+)',
                r'"points"\s*:\s*"(\d+)"',
                r'\\u0022points\\u0022\s*:\s*(\d+)',
                r'points["\s]*:["\s]*(\d+)',
            ]
            
            points = None
            for pattern in patterns:
                match = re.search(pattern, page_content)
                if match:
                    points = int(match.group(1))
                    logging.info(f"✅ 成功提取积分: {points}")
                    break
            
            if points is not None:
                return points
            else:
                logging.error("❌ 未能从页面中提取积分")
                await page.screenshot(path="error_no_points_data.png")
                return None
                
        except Exception as e:
            logging.error(f"❌ 获取用户积分失败: {e}")
            await page.screenshot(path="error_get_points.png")
            return None
        finally:
            await browser.close()
