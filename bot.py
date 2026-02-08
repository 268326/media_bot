import asyncio
import os
import logging
import sys
import re
import glob
import aiohttp
from urllib.parse import urlparse, parse_qs
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from playwright.async_api import async_playwright

# ================= 配置加载区域 =================
load_dotenv()

# 超时配置常量
BROWSER_TIMEOUT = 30000
LOGIN_TIMEOUT = 20000
SEARCH_TIMEOUT = 10000
SHORT_WAIT = 1000
MEDIUM_WAIT = 1500
LONG_WAIT = 3000

BOT_TOKEN = os.getenv("BOT_TOKEN")
HDHIVE_USER = os.getenv("HDHIVE_USER")
HDHIVE_PASS = os.getenv("HDHIVE_PASS")
HDHIVE_USER_ID = os.getenv("HDHIVE_USER_ID", "")  # HDHive 用户ID (用于查询积分)
TMDB_API_KEY = os.getenv("TMDB_API_KEY", "")  # TMDB API Key (可选)
ALLOWED_USER_ID = int(os.getenv("ALLOWED_USER_ID", "0"))  # 允许使用机器人的用户ID
TARGET_GROUP_ID = int(os.getenv("TARGET_GROUP_ID", "0"))  # 目标群组ID (已废弃，保留兼容)
SA_URL = os.getenv("SA_URL", "")  # Symedia地址
SA_PARENT_ID = os.getenv("SA_PARENT_ID", "")  # 115保存目录ID
COOKIE_FILE = "auth.json"

if not all([BOT_TOKEN, HDHIVE_USER, HDHIVE_PASS]):
    print("❌ 错误: 未找到配置信息，请检查 .env 文件。")
    sys.exit(1)

if ALLOWED_USER_ID == 0:
    print("⚠️ 警告: 未配置 ALLOWED_USER_ID，任何人都可以使用机器人！")

if not SA_URL or not SA_PARENT_ID:
    print("⚠️ 警告: 未配置 SA_URL 或 SA_PARENT_ID，无法自动添加到Symedia。")
# ===============================================

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
logging.basicConfig(level=logging.INFO)

# ================= 权限检查中间件 =================
async def check_user_permission(message: types.Message):
    """检查用户是否有权限使用机器人"""
    if ALLOWED_USER_ID == 0:
        return True  # 未配置限制，允许所有人
    
    user_id = message.from_user.id
    if user_id != ALLOWED_USER_ID:
        await message.reply(
            "⛔️ <b>权限不足</b>\n\n"
            "抱歉，您没有权限使用此机器人。",
            parse_mode="HTML"
        )
        logging.warning(f"❌ 用户 {user_id} ({message.from_user.username}) 尝试使用机器人但被拒绝")
        return False
    return True
# ===============================================

async def get_browser_context(p):
    """启动浏览器并加载 Cookie"""
    browser = await p.chromium.launch(
        headless=True,
        args=[
            "--no-sandbox", 
            "--disable-setuid-sandbox",
            "--disable-dev-shm-usage",  # 防止共享内存不足
            "--disable-blink-features=AutomationControlled"  # 防止被检测为自动化
        ]
    )
    # 强制 1920x1080 确保 UI 完整显示
    viewport_size = {'width': 1920, 'height': 1080}

    if os.path.exists(COOKIE_FILE):
        try:
            context = await browser.new_context(storage_state=COOKIE_FILE, viewport=viewport_size)
            logging.info("✅ 成功加载已保存的 Cookie")
        except Exception as e:
            logging.warning(f"⚠️ Cookie 文件损坏，将重新登录: {e}")
            context = await browser.new_context(viewport=viewport_size)
    else:
        context = await browser.new_context(viewport=viewport_size)
        logging.info("📝 首次运行，将执行登录")
    
    await context.set_extra_http_headers({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    })
    return browser, context

async def login_logic(page, context):
    """执行登录操作"""
    logging.info("正在执行登录操作...")
    try:
        if "/login" not in page.url:
            await page.goto("https://hdhive.com/login", wait_until="domcontentloaded")
        
        await page.wait_for_selector('#username', state='visible', timeout=10000)
        await page.fill('#username', HDHIVE_USER)
        await page.fill('#password', HDHIVE_PASS)
        
        # 点击登录按钮
        await page.click('button[type="submit"]')
        
        # 等待登录成功（跳转离开登录页）
        await page.wait_for_url(lambda u: "/login" not in u, timeout=LOGIN_TIMEOUT)
        logging.info("✅ 登录成功，保存 Cookies...")
        await context.storage_state(path=COOKIE_FILE)
        
        # 额外等待，确保状态稳定
        await page.wait_for_timeout(SHORT_WAIT)
    except Exception as e:
        logging.error(f"❌ 登录失败: {e}")
        await page.screenshot(path="error_login.png")
        raise e 

async def search_tmdb(keyword, media_type="multi"):
    """
    使用TMDB API搜索影视内容
    media_type: 'movie' (电影), 'tv' (剧集), 'multi' (全部)
    返回: 如果找到完全匹配,返回单个dict; 否则返回包含多个结果的list
    """
    if not TMDB_API_KEY:
        logging.info("⚠️ 未配置TMDB_API_KEY，跳过TMDB API")
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
                        keyword_lower = keyword.lower()
                        
                        # 检查是否有完全匹配的结果
                        for item in results:
                            title = (item.get("title") or item.get("name", "")).lower()
                            if title == keyword_lower:
                                # 完全匹配,直接返回这个结果
                                result_type = item.get("media_type") if media_type == "multi" else media_type
                                poster_path = item.get("poster_path")
                                
                                tmdb_info = {
                                    "tmdb_id": item.get("id"),
                                    "media_type": result_type,
                                    "title": item.get("title") or item.get("name"),
                                    "overview": item.get("overview", "暂无简介"),
                                    "poster_url": f"https://image.tmdb.org/t/p/w500{poster_path}" if poster_path else None,
                                    "rating": item.get("vote_average", 0),
                                    "release_date": item.get("release_date") or item.get("first_air_date", "未知")
                                }
                                
                                logging.info(f"✅ TMDB完全匹配: {tmdb_info['title']} (ID: {tmdb_info['tmdb_id']})")
                                return tmdb_info
                        
                        # 没有完全匹配,返回前5个结果供用户选择
                        logging.info(f"⚠️ 未找到完全匹配,返回 {min(5, len(results))} 个结果供选择")
                        search_results = []
                        for item in results[:5]:
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
                        
                        return search_results  # 返回列表
                    else:
                        logging.warning(f"⚠️ TMDB未找到结果: {keyword}")
                        return None
                else:
                    logging.error(f"❌ TMDB API请求失败: {response.status}")
                    return None
    except Exception as e:
        logging.error(f"❌ TMDB API出错: {e}")
        return None

async def get_tmdb_details(tmdb_id, media_type):
    """
    通过TMDB ID获取详细信息
    media_type: 'movie' (电影) 或 'tv' (剧集)
    返回: 包含详细信息的dict
    """
    if not TMDB_API_KEY:
        logging.info("⚠️ 未配置TMDB_API_KEY")
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

async def get_resources_by_tmdb_id(tmdb_id, media_type):
    """
    通过TMDB ID直接获取资源列表
    tmdb_id: TMDB ID (数字) 或 UUID
    media_type: 'movie' 或 'tv'
    """
    import glob
    debug_files = glob.glob("debug_*.png") + glob.glob("error_*.png")
    for file in debug_files:
        try:
            os.remove(file)
        except:
            pass
    
    async with async_playwright() as p:
        browser, context = await get_browser_context(p)
        page = await context.new_page()
        
        results = []
        try:
            # 判断是TMDB ID（数字）还是直接链接UUID
            if str(tmdb_id).isdigit():
                # TMDB ID格式: /tmdb/movie/123
                target_url = f"https://hdhive.com/tmdb/{media_type}/{tmdb_id}"
            else:
                # 直接链接格式: /movie/uuid 或 /tv/uuid
                target_url = f"https://hdhive.com/{media_type}/{tmdb_id}"
            
            logging.info(f"🎯 直接导航到页面: {target_url}")
            logging.info(f"🎯 直接导航到页面: {target_url}")
            await page.goto(target_url, wait_until="domcontentloaded", timeout=BROWSER_TIMEOUT)
            
            # 检查是否被重定向到登录页
            if "/login" in page.url or await page.locator("#username").count() > 0:
                logging.info("需要登录，开始登录...")
                await login_logic(page, context)
                # 登录后重新导航到目标页面
                logging.info(f"重新导航到: {target_url}")
                await page.goto(target_url, wait_until="domcontentloaded", timeout=BROWSER_TIMEOUT)
            
            await page.wait_for_timeout(LONG_WAIT)
            
            logging.info(f"✅ 当前页面: {page.url}")
            await page.screenshot(path="debug_01_tmdb_page.png")
            
            # 验证是否在详情页
            current_url = page.url
            if "/tv/" not in current_url and "/movie/" not in current_url:
                logging.error(f"❌ 未能进入详情页，当前URL: {current_url}")
                return []
            
            # === 注释掉115 Tab切换功能（从未成功过但也不影响） ===
            # # 切换到115网盘 Tab
            # logging.info("正在寻找 115网盘 按钮...")
            # logging.info(f"当前页面URL: {page.url}")
            # 
            # # 先检查页面是否有Tab结构
            # tab_buttons = await page.locator('button[role="tab"]').all()
            # logging.info(f"找到 {len(tab_buttons)} 个Tab按钮")
            # 
            # for i, tab in enumerate(tab_buttons[:10]):
            #     try:
            #         tab_text = await tab.inner_text()
            #         logging.info(f"  Tab {i+1}: '{tab_text}'")
            #     except:
            #         pass
            # 
            # tab_btn = page.locator('button[role="tab"]', has_text="115")
            # 
            # if await tab_btn.count() > 0:
            #     await tab_btn.first.wait_for(state="visible", timeout=5000)
            #     await tab_btn.first.evaluate("element => element.click()")
            #     logging.info("✅ 成功点击 115 按钮")
            #     await page.wait_for_timeout(MEDIUM_WAIT)
            #     await page.screenshot(path="debug_02_after_115_tab.png")
            # else:
            #     logging.warning("⚠️ 未找到115按钮")
            #     await page.screenshot(path="debug_02_no_115_button.png")
            
            # 解析资源列表
            await page.wait_for_timeout(MEDIUM_WAIT)
            logging.info("开始解析资源列表...")
            
            # 先查看页面上所有的链接
            all_links = await page.locator('a').all()
            logging.info(f"页面总共有 {len(all_links)} 个链接")
            
            # 获取资源链接
            resource_links = await page.locator('a[href^="/resource/"]').all()
            logging.info(f"找到 {len(resource_links)} 个资源链接")
            
            # 通过资源链接找到它们的父级卡片容器（包含完整信息）
            resource_cards = []
            for link in resource_links[:8]:
                # 向上2层找到完整的卡片容器：a -> div.MuiBox-root -> div.MuiBox-root (包含用户信息的那层)
                # 使用 XPath 向上查找2层
                parent_container = link.locator('xpath=../..').first
                resource_cards.append((parent_container, link))
            
            logging.info(f"找到 {len(resource_cards)} 个资源卡片容器")
            
            # 如果列表为空，再截图并尝试其他选择器
            if not resource_cards:
                logging.warning("⚠️ 资源列表为空，尝试其他选择器...")
                await page.screenshot(path="debug_03_empty_list.png")
                return []
            else:
                # 截图：找到资源列表
                await page.screenshot(path="debug_03_resource_list_found.png")
                logging.info("📸 步骤3截图: debug_03_resource_list_found.png - 找到资源列表")
            
            # === 添加调试：保存第一个资源卡片的HTML ===
            if len(resource_cards) > 0:
                first_card, first_link = resource_cards[0]
                first_card_html = await first_card.inner_html()
                with open('debug_first_card.html', 'w', encoding='utf-8') as f:
                    f.write(first_card_html)
                logging.info("📝 已保存第一个资源卡片HTML到 debug_first_card.html")
            
            # === 并发解析资源卡片 ===
            async def parse_resource_card(card_container, link):
                """异步解析单个资源卡片"""
                try:
                    href = await link.get_attribute("href")
                    res_id = href.split("/")[-1]
                    
                    logging.info(f"🔍 开始解析资源 ID: {res_id}")
                    
                    # 从卡片容器中提取信息（而不是只从link中提取）
                    card = card_container
                    
                    # 提取标题 - 从link中提取（标题在link里）
                    title = "未知资源"
                    # 策略1: p.MuiTypography-body2
                    title_element = link.locator('p.MuiTypography-body2')
                    title_count = await title_element.count()
                    if title_count > 0:
                        title = await title_element.first.inner_text()
                    # 策略2: 任何包含描述文本的p标签
                    if title == "未知资源" or not title.strip():
                        title_element = link.locator('p.MuiTypography-root')
                        p_count = await title_element.count()
                        if p_count > 0:
                            for p in await title_element.all():
                                text = await p.inner_text()
                                if text and len(text.strip()) > 5:
                                    title = text.strip()
                                    break
                    
                    # 提取用户名 - 从整个卡片容器中查找
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
                    
                    # 提取积分或免费状态 - 从整个卡片容器中查找
                    points = "未知"
                    all_chips = await card.locator('span.MuiChip-label').all()
                    for chip in all_chips:
                        chip_text = await chip.inner_text()
                        chip_text = chip_text.strip()
                        if '积分' in chip_text or chip_text == '免费' or chip_text == '已解锁':
                            points = chip_text
                            break
                    
                    # 提取标签（质量、字幕、大小等）- 从整个卡片容器中提取
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
                    
                    # 如果没找到标签，尝试从link的所有div中提取
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
                    import traceback
                    logging.warning(traceback.format_exc())
                    return None
            
            # 并发解析所有资源卡片
            import asyncio
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

async def search_resources(keyword, search_type="all"):
    """
    search_type: 'tv' (剧集), 'movie' (电影), 'all' (默认)
    """
    # 清除上次的调试图片
    import glob
    debug_files = glob.glob("debug_*.png") + glob.glob("error_*.png")
    for file in debug_files:
        try:
            os.remove(file)
            logging.info(f"🗑️ 清除调试图片: {file}")
        except:
            pass
    
    async with async_playwright() as p:
        browser, context = await get_browser_context(p)
        page = await context.new_page()
        
        results = []
        try:
            # 1. 访问首页与登录检查
            logging.info("正在访问首页...")
            await page.goto("https://hdhive.com/", wait_until="domcontentloaded", timeout=BROWSER_TIMEOUT)
            
            # 截图：首页加载完成
            await page.screenshot(path="debug_01_homepage.png")
            logging.info("📸 步骤1截图: debug_01_homepage.png - 首页加载完成")
            
            try: 
                await page.wait_for_load_state("networkidle", timeout=5000)
            except Exception as e:
                logging.warning(f"页面网络空闲状态等待超时: {e}")
                pass 

            if "/login" in page.url or await page.locator("#username").count() > 0:
                logging.info("检测到未登录，开始登录...")
                await login_logic(page, context)
                await page.wait_for_timeout(LONG_WAIT)  # 增加等待时间确保登录状态稳定
                
                # 截图：登录完成后
                await page.screenshot(path="debug_02_after_login.png")
                logging.info("📸 步骤2截图: debug_02_after_login.png - 登录完成")

            # 2. 寻找并打开搜索框
            logging.info("正在打开搜索框...")
            
            # 截图：寻找前往搜索按钮前
            await page.screenshot(path="debug_03_before_search_button.png")
            logging.info("📸 步骤3截图: debug_03_before_search_button.png - 寻找前往搜索按钮前")
            
            # 点击"前往搜索"按钮打开搜索对话框 - 使用更精确的选择器
            search_btn = page.locator('button.MuiButton-root:has-text("前往搜索")')
            if await search_btn.count() > 0:
                logging.info("找到前往搜索按钮（策略A）")
                await search_btn.first.click()
                await page.wait_for_timeout(SHORT_WAIT)
            else:
                # 备用选择器
                search_btn = page.locator('button:has-text("前往搜索")')
                if await search_btn.count() > 0:
                    logging.info("找到前往搜索按钮（策略B）")
                    await search_btn.first.click()
                    await page.wait_for_timeout(SHORT_WAIT)
                else:
                    logging.warning("未找到前往搜索按钮!")
            
            # 截图：点击前往搜索按钮后
            await page.screenshot(path="debug_04_after_search_button.png")
            logging.info("📸 步骤4截图: debug_04_after_search_button.png - 点击前往搜索按钮后")
            # 等待搜索输入框出现 - 使用实际的placeholder文本
            try:
                await page.wait_for_selector('input[placeholder="搜索剧集..."]', timeout=5000)
                search_input = page.locator('input[placeholder="搜索剧集..."]').first
                logging.info("找到搜索输入框（策略A）")
            except:
                # 备用选择器
                await page.wait_for_selector('input.MuiInputBase-input', timeout=5000)
                search_input = page.locator('input.MuiInputBase-input').first
                logging.info("找到搜索输入框（策略B）")

            # 3. 输入关键词
            logging.info(f"正在输入关键词: {keyword}")
            await search_input.click()
            await page.wait_for_timeout(300)
            await search_input.fill(keyword)
            await page.wait_for_timeout(MEDIUM_WAIT)
            
            # 验证输入
            input_value = await search_input.input_value()
            logging.info(f"✅ 已输入: {input_value}")
            
            # 截图：输入关键词后
            await page.screenshot(path="debug_05_after_input_keyword.png")
            logging.info("📸 步骤5截图: debug_05_after_input_keyword.png - 输入关键词后")
            
            # 4. 点击筛选按钮
            logging.info(f"正在应用筛选: {search_type}...")
            if search_type == "tv":
                # 点击"剧集"筛选 - 检查是否已经选中，如果是outlined状态则点击
                chip = page.locator('div.MuiChip-root.MuiChip-outlined:has(span.MuiChip-label:text("剧集"))')
                if await chip.count() > 0:
                    logging.info("点击剧集筛选按钮")
                    await chip.first.click()
                    await page.wait_for_timeout(SHORT_WAIT)
                else:
                    logging.info("剧集筛选已经选中")
            elif search_type == "movie":
                # 点击"电影"筛选 - 检查是否已经选中，如果是outlined状态则点击
                chip = page.locator('div.MuiChip-root.MuiChip-outlined:has(span.MuiChip-label:text("电影"))')
                if await chip.count() > 0:
                    logging.info("点击电影筛选按钮")
                    await chip.first.click()
                    await page.wait_for_timeout(SHORT_WAIT)
                else:
                    logging.info("电影筛选已经选中")
            
            # 截图：应用筛选后
            await page.screenshot(path="debug_06_after_filter.png")
            logging.info("📸 步骤6截图: debug_06_after_filter.png - 应用筛选后")

            # 5. 点击搜索按钮执行搜索
            logging.info("正在点击搜索按钮执行搜索...")
            
            # 直接按回车键执行搜索（最可靠的方法）
            logging.info("直接按回车键执行搜索")
            await search_input.press('Enter')
            
            # 等待搜索执行
            await page.wait_for_timeout(LONG_WAIT)  # 增加等待时间确保搜索完成
            
            # 截图：执行搜索后
            await page.screenshot(path="debug_06_5_after_search_execute.png")
            logging.info("📸 步骤6.5截图: debug_06_5_after_search_execute.png - 执行搜索后")

            # 6. 定位搜索对话框中的结果
            logging.info("等待搜索结果...")
            
            # 等待搜索结果容器出现
            await page.wait_for_timeout(LONG_WAIT)
            
            # 截图：等待搜索结果后
            await page.screenshot(path="debug_07_search_results_found.png")
            logging.info("📸 步骤7截图: debug_07_search_results_found.png - 搜索结果页面")
            
            # 搜索结果在弹窗对话框中，链接格式是 /tmdb/movie/xxx 或 /tmdb/tv/xxx
            # 需要点击这些链接才能跳转到真正的详情页
            
            # 额外调试：查看对话框中的所有链接
            logging.info("🔍 调试：查看对话框中的所有链接")
            all_dialog_links = await page.locator('div.MuiDialogContent-root a[href^="/tmdb/"]').all()
            logging.info(f"找到 {len(all_dialog_links)} 个TMDB搜索结果链接")
            
            for i, link in enumerate(all_dialog_links[:20]):
                try:
                    href = await link.get_attribute('href')
                    # 从img alt获取标题
                    img = link.locator('img[alt]')
                    if await img.count() > 0:
                        text = await img.first.get_attribute('alt')
                    else:
                        # 从h6标题获取
                        h6 = link.locator('h6')
                        if await h6.count() > 0:
                            text = await h6.first.inner_text()
                        else:
                            text = await link.inner_text()
                    
                    logging.info(f"  链接 {i+1}: href='{href}' text='{text[:80]}'")
                except:
                    pass
            
            # 选择器：查找对话框中的TMDB链接
            if search_type == "tv": 
                selector = 'div.MuiDialogContent-root a[href^="/tmdb/tv/"]'
            elif search_type == "movie": 
                selector = 'div.MuiDialogContent-root a[href^="/tmdb/movie/"]'
            else: 
                selector = 'div.MuiDialogContent-root a[href^="/tmdb/"]'
            
            # 等待搜索结果出现
            try: 
                await page.wait_for_selector(selector, timeout=5000)
                logging.info(f"✅ 找到搜索结果 (选择器: {selector})")
            except Exception as e:
                logging.error(f"❌ 未找到搜索结果: {e}")
                await page.screenshot(path="error_no_results.png")
                return []
            
            # 获取所有搜索结果，找最匹配的
            all_results = await page.locator(selector).all()
            logging.info(f"🔍 找到 {len(all_results)} 个搜索结果")
            
            # 先截图查看搜索结果页面
            await page.screenshot(path="debug_08_search_results_detail.png")
            logging.info("📸 步骤8截图: debug_08_search_results_detail.png - 搜索结果详情")
            
            keyword_lower = keyword.lower()
            best_match = None
            best_match_score = -1
            best_match_href = ""
            best_match_title = ""
            
            for idx, result in enumerate(all_results[:10]):  # 只检查前10个
                try:
                    href = ""
                    result_text = ""
                    
                    # 获取链接href (TMDB链接)
                    href = await result.get_attribute('href')
                    if not href:
                        logging.warning(f"  ⚠️ 结果 {idx+1}: 没有href属性，跳过")
                        continue
                    
                    # 验证是TMDB链接
                    if not href.startswith('/tmdb/'):
                        logging.info(f"  ⚠️ 结果 {idx+1}: 不是TMDB链接 href='{href}'，跳过")
                        continue
                    
                    # 优先从img的alt属性获取标题（最准确）
                    img = result.locator('img[alt]')
                    if await img.count() > 0:
                        result_text = await img.first.get_attribute('alt')
                        text_source = "图片alt"
                    
                    # 备用：从h6标题获取
                    if not result_text.strip():
                        h6 = result.locator('h6.MuiTypography-subtitle1')
                        if await h6.count() > 0:
                            result_text = await h6.first.inner_text()
                            text_source = "h6标题"
                    
                    # 备用：从链接文本
                    if not result_text.strip():
                        result_text = await result.inner_text()
                        text_source = "链接文本"
                    
                    if not result_text.strip():
                        logging.warning(f"  ⚠️ 结果 {idx+1}: 无法获取标题文本 href='{href}'")
                        result_text = "未知标题"
                        text_source = "无"
                    
                    logging.info(f"  🔸 结果 {idx+1}: href='{href}' text='{result_text[:80]}' (来源:{text_source})")
                    
                    # 计算匹配分数
                    result_text_lower = result_text.lower()
                    score = 0
                    
                    # 完全匹配得最高分
                    if keyword_lower == result_text_lower:
                        score = 100
                        logging.info(f"       ✅ 完全匹配! 得分={score}")
                    # 包含关键词
                    elif keyword_lower in result_text_lower:
                        score = 80
                        logging.info(f"       ✅ 包含关键词! 得分={score}")
                    # 关键词包含在结果中
                    elif result_text_lower in keyword_lower:
                        score = 60
                        logging.info(f"       🔸 部分匹配 得分={score}")
                    # 检查是否有共同的词
                    else:
                        keyword_words = set(keyword_lower.split())
                        result_words = set(result_text_lower.split())
                        common_words = keyword_words & result_words
                        if common_words:
                            score = len(common_words) * 10
                            logging.info(f"       🔸 共同词汇={common_words} 得分={score}")
                        else:
                            logging.info(f"       ⚠️ 无匹配 得分=0")
                    
                    # 更新最佳匹配（保存链接元素本身）
                    if score > best_match_score:
                        best_match_score = score
                        best_match = result  # 保存 <a> 元素
                        best_match_href = href
                        best_match_title = result_text
                        logging.info(f"       🎯 更新最佳匹配: 结果 {idx+1} 得分={score}")
                    
                except Exception as e:
                    logging.warning(f"  ❌ 检查结果 {idx+1} 失败: {e}")
                    continue
            
            # 使用最佳匹配结果
            if best_match and best_match_score > 0:
                final_href = best_match_href
                final_title = best_match_title
                logging.info(f"🎯 选择最佳匹配结果 (得分={best_match_score}): {final_href}")
                logging.info(f"🏷️ 标题: {final_title[:100]}")
                logging.info(f"🔍 搜索词: '{keyword}' -> 选中: '{final_title[:50]}'")
            else:
                logging.error("❌ 没有找到任何匹配的TMDB链接")
                return []
            
            # === 直接导航到TMDB页面（会自动重定向到详情页）===
            try:
                full_url = f"https://hdhive.com{final_href}"
                logging.info(f"🔗 导航到TMDB页面: {full_url}")
                await page.goto(full_url, wait_until="domcontentloaded", timeout=BROWSER_TIMEOUT)
                
                # TMDB页面会自动重定向到详情页，等待一下
                await page.wait_for_timeout(LONG_WAIT)
                logging.info(f"✅ 导航完成，当前URL: {page.url}")
                
            except Exception as nav_error:
                logging.error(f"❌ 导航失败: {nav_error}")
                raise
            
            # 6. 等待详情页加载完成
            logging.info("正在等待详情页加载...")
            await page.wait_for_timeout(MEDIUM_WAIT)
            
            # 验证当前URL
            current_url = page.url
            logging.info(f"✅ 当前页面: {current_url}")
            
            # 截图：详情页加载后
            await page.screenshot(path="debug_09_detail_page.png")
            logging.info("📸 步骤9截图: debug_09_detail_page.png - 详情页已加载")
            
            # 确认已经在详情页
            if "/tv/" not in current_url and "/movie/" not in current_url:
                logging.error(f"❌ 未能进入详情页，当前URL: {current_url}")
                await page.screenshot(path="error_not_detail_page.png")
                return []
            
            # 等待页面完全加载
            try:
                await page.wait_for_load_state("networkidle", timeout=5000)
            except:
                logging.info("页面networkidle等待超时，继续执行")
                pass

            # 7. 寻找并切换到 115网盘 Tab
            logging.info("正在寻找 115网盘 按钮...")
            logging.info(f"当前页面URL: {page.url}")
            
            # 先检查页面是否有Tab结构
            tab_buttons = await page.locator('button[role="tab"]').all()
            logging.info(f"找到 {len(tab_buttons)} 个Tab按钮")
            
            for i, tab in enumerate(tab_buttons):
                tab_text = await tab.inner_text()
                logging.info(f"  Tab {i+1}: '{tab_text}'")
            
            # 策略A: 找 Role 为 tab 且包含 115 (最准确)
            tab_btn = page.locator('button[role="tab"]', has_text="115")
            
            # 策略B: 备用 - 普通按钮包含115
            if await tab_btn.count() == 0:
                logging.info("策略A未找到，尝试策略B...")
                tab_btn = page.locator('button', has_text="115")
                
            # 策略C: 更宽泛的查找
            if await tab_btn.count() == 0:
                logging.info("策略B未找到，尝试策略C...")
                tab_btn = page.locator('*:has-text("115")')

            try:
                if await tab_btn.count() > 0:
                    # 等待按钮可见
                    await tab_btn.first.wait_for(state="visible", timeout=5000)
                    
                    # === [核心修复] 使用 JS 强制点击 (修复Tab被遮挡) ===
                    logging.info("执行强制点击 115 Tab...")
                    await tab_btn.first.evaluate("element => element.click()")
                    logging.info("✅ 成功点击 115 按钮")
                    
                    # 等待115内容加载
                    await page.wait_for_timeout(MEDIUM_WAIT)
                    
                    # 截图：点击115 Tab后
                    await page.screenshot(path="debug_10_after_115_tab.png")
                    logging.info("📸 步骤10截图: debug_10_after_115_tab.png - 点击115 Tab后")
                else:
                    logging.warning("⚠️ 在任何策略下都未找到115按钮")
                    # 截图帮助调试
                    await page.screenshot(path="debug_10_no_115_button.png")
                    logging.info("📸 步骤10截图: debug_10_no_115_button.png - 未找到115按钮")
                    
            except Exception as e:
                logging.warning(f"⚠️ 点击 115 按钮出错 (尝试继续抓取): {e}")
                pass

            # 8. 解析资源列表
            await page.wait_for_timeout(MEDIUM_WAIT) # 等待列表刷新
            logging.info("开始解析资源列表...")
            
            # 先查看页面上所有的链接
            all_links = await page.locator('a').all()
            logging.info(f"页面总共有 {len(all_links)} 个链接")
            
            resource_links = await page.locator('a[href^="/resource/"]').all()
            logging.info(f"找到 {len(resource_links)} 个资源链接")
            
            resource_cards = await page.locator('a[href^="/resource/"]').all()
            
            # 如果列表为空，再截图并尝试其他选择器
            if not resource_cards:
                logging.warning("⚠️ 资源列表为空，尝试其他选择器...")
                await page.screenshot(path="debug_11_empty_list.png")
                logging.info("📸 步骤11截图: debug_11_empty_list.png - 资源列表为空")
                
                # 尝试查找其他可能的资源链接格式
                alt_resources = await page.locator('a[href*="/resource/"]').all()
                logging.info(f"使用备用选择器找到 {len(alt_resources)} 个资源")
                resource_cards = alt_resources
            else:
                # 截图：找到资源列表
                await page.screenshot(path="debug_11_resource_list_found.png")
                logging.info("📸 步骤11截图: debug_11_resource_list_found.png - 找到资源列表")

            for card in resource_cards[:8]:
                try:
                    href = await card.get_attribute("href")
                    res_id = href.split("/")[-1]
                    
                    # 提取标题 - 尝试多种选择器
                    title = "未知资源"
                    # 策略1: p.MuiTypography-body2
                    title_element = card.locator('p.MuiTypography-body2')
                    if await title_element.count() > 0:
                        title = await title_element.first.inner_text()
                    # 策略2: 任何包含描述文本的p标签
                    if title == "未知资源" or not title.strip():
                        title_element = card.locator('p.MuiTypography-root')
                        if await title_element.count() > 0:
                            for p in await title_element.all():
                                text = await p.inner_text()
                                if text and len(text.strip()) > 5:  # 标题通常较长
                                    title = text.strip()
                                    break
                    
                    # 提取用户名 - 改进选择器
                    uploader = "未知用户"
                    # 策略1: 从span标签的title属性
                    user_span = card.locator('span.MuiTypography-caption[title]')
                    if await user_span.count() > 0:
                        uploader = await user_span.first.get_attribute("title")
                    # 策略2: 直接从span文本
                    if uploader == "未知用户":
                        user_span = card.locator('span.MuiTypography-caption')
                        if await user_span.count() > 0:
                            text = await user_span.first.inner_text()
                            if text and text.strip():
                                uploader = text.strip()
                    
                    # 提取积分或免费状态 - 改进匹配
                    points = "未知"
                    # 查找所有Chip标签
                    all_chips = await card.locator('span.MuiChip-label').all()
                    for chip in all_chips:
                        chip_text = await chip.inner_text()
                        chip_text = chip_text.strip()
                        # 匹配 "X积分" 或 "免费"
                        if '积分' in chip_text or chip_text == '免费':
                            points = chip_text
                            break
                    
                    # 提取标签（质量、字幕、大小等）- 改进提取逻辑
                    tags = []
                    # 从所有小标签容器中提取
                    tag_containers = card.locator('div.MuiBox-root > div.MuiBox-root')
                    for tag_box in await tag_containers.all():
                        try:
                            tag_text = await tag_box.inner_text()
                            tag_text = tag_text.strip()
                            # 过滤有效标签：长度合适，不是标题或用户名
                            if (tag_text and 
                                1 <= len(tag_text) <= 30 and 
                                tag_text not in [title, uploader, points] and
                                tag_text not in tags):
                                tags.append(tag_text)
                        except:
                            pass
                    
                    # 如果没找到标签，尝试从所有div中提取
                    if not tags:
                        all_divs = card.locator('div.MuiBox-root')
                        for div in await all_divs.all():
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
                    tags = list(dict.fromkeys(tags))[:15]  # 最多15个标签
                    
                    resource_info = {
                        "id": res_id,
                        "title": title,
                        "uploader": uploader,
                        "points": points,
                        "tags": tags
                    }
                    
                    results.append(resource_info)
                    tags_str = " ".join(tags[:8]) if tags else "无标签"  # 日志中最多显示8个
                    logging.info(f"  📦 [{points}] {uploader} | {tags_str} | {title}")
                    
                except Exception as e:
                    logging.warning(f"⚠️ 解析资源卡片失败: {e}")
                    continue
            
            logging.info(f"✅ 找到 {len(results)} 个资源")
            return results

        except Exception as e:
            logging.error(f"❌ 搜索出错: {e}")
            await page.screenshot(path="error_search_fail.png")
            return []
        finally:
            await browser.close()

async def fetch_download_link(resource_id):
    """提取链接"""
    url = f"https://hdhive.com/resource/{resource_id}"
    async with async_playwright() as p:
        browser, context = await get_browser_context(p)
        page = await context.new_page()
        try:
            logging.info(f"提取中: {url}")
            await page.goto(url, wait_until="domcontentloaded", timeout=BROWSER_TIMEOUT)
            
            if "/login" in page.url or await page.locator("#username").count() > 0:
                await login_logic(page, context)
                await page.goto(url, wait_until="domcontentloaded")

            await page.wait_for_timeout(LONG_WAIT)
            
            # 截图：资源详情页
            await page.screenshot(path="debug_resource_page.png")
            logging.info(f"📸 资源页面截图: debug_resource_page.png")
            logging.info(f"当前URL: {page.url}")
            
            # 检查当前页面URL是否已经是115链接（有些资源会自动跳转）
            current_url = page.url
            if '115.com/s/' in current_url or '115cdn.com/s/' in current_url:
                logging.info(f"✅ 页面已自动跳转到115链接: {current_url}")
                # 从URL中提取链接和密码
                share_link = current_url.split('?')[0] if '?' in current_url else current_url
                parsed = urlparse(current_url)
                params = parse_qs(parsed.query)
                share_code = params.get('password', [None])[0]
                
                # 构建完整链接
                if share_code and '?' not in share_link:
                    full_link = f"{share_link}?password={share_code}"
                else:
                    full_link = share_link
                
                result = {"link": full_link, "code": share_code or "无"}
                logging.info(f"✅ 提取成功: {full_link}")
                return result
            
            # 检查是否需要积分解锁
            unlock_text_element = page.locator('div.MuiBox-root:has-text("需要使用")')
            if await unlock_text_element.count() > 0:
                unlock_text = await unlock_text_element.first.inner_text()
                logging.info(f"⚠️ 需要积分解锁: {unlock_text}")
                
                # 提取需要的积分数
                import re
                match = re.search(r'需要使用\s*(\d+)\s*积分', unlock_text)
                if match:
                    required_points = int(match.group(1))
                    logging.info(f"需要 {required_points} 积分")
                    
                    # 返回特殊标记，让调用者决定是否解锁
                    return {
                        "need_unlock": True,
                        "points": required_points,
                        "resource_id": resource_id
                    }
                else:
                    logging.warning("无法提取积分数量")
            
            # 从页面中查找115分享链接
            share_link = None
            share_code = None
            
            # 调试：查看所有链接
            logging.info("🔍 查找页面上的所有链接...")
            all_page_links = await page.locator('a').all()
            logging.info(f"页面总共有 {len(all_page_links)} 个链接")
            
            for i, link in enumerate(all_page_links[:30]):
                try:
                    href = await link.get_attribute('href')
                    text = await link.inner_text()
                    if href and ('115' in href or '115' in text or '前往' in text):
                        logging.info(f"  链接 {i+1}: href='{href}' text='{text[:50]}'")
                except:
                    pass
            
            all_links = await page.locator('a[href*="115"]').all()
            logging.info(f"找到 {len(all_links)} 个包含115的链接")
            
            for link in all_links:
                href = await link.get_attribute('href')
                if href and ('115.com/s/' in href or '115cdn.com/s/' in href):
                    share_link = href
                    parsed = urlparse(href)
                    params = parse_qs(parsed.query)
                    share_code = params.get('password', [None])[0]
                    logging.info(f"从页面提取到链接: {share_link}")
                    break
            
            # 如果没找到，尝试点击按钮
            if not share_link:
                logging.info("未找到直接链接，尝试查找按钮...")
                
                # 查找所有可能的按钮
                logging.info("查找 '前往' 按钮...")
                goto_btns = await page.locator('a:has-text("前往")').all()
                logging.info(f"找到 {len(goto_btns)} 个'前往'按钮")
                
                logging.info("查找 '115' 按钮...")
                btn_115 = await page.locator('a:has-text("115")').all()
                logging.info(f"找到 {len(btn_115)} 个'115'按钮")
                
                # 尝试更宽泛的选择器
                logging.info("查找包含115的按钮...")
                all_btns = await page.locator('button, a').all()
                for i, btn in enumerate(all_btns[:20]):
                    try:
                        text = await btn.inner_text()
                        href = await btn.get_attribute('href')
                        if '115' in text or (href and '115' in href):
                            logging.info(f"  按钮 {i+1}: text='{text[:30]}' href='{href}'")
                    except:
                        pass
                
                try:
                    btns = page.locator('a:has-text("前往"), a:has-text("115")')
                    if await btns.count() > 0:
                        logging.info(f"准备点击按钮...")
                        await btns.first.click()
                        await page.wait_for_timeout(LONG_WAIT)
                        logging.info(f"点击后页面数量: {len(context.pages)}")
                    else:
                        logging.warning("未找到可点击的按钮")
                    
                    if len(context.pages) > 1:
                        final_page = context.pages[-1]
                        await final_page.wait_for_load_state(timeout=SEARCH_TIMEOUT)
                        final_url = final_page.url
                        if '115.com/s/' in final_url or '115cdn.com/s/' in final_url:
                            share_link = final_url
                            parsed = urlparse(final_url)
                            params = parse_qs(parsed.query)
                            share_code = params.get('password', [None])[0]
                except Exception as e:
                    logging.warning(f"点击按钮时出错: {e}")
                    await page.screenshot(path="error_click_button.png")
                    pass
            
            if share_link:
                # 构建包含提取码的完整链接
                if share_code and '?' not in share_link:
                    full_link = f"{share_link}?password={share_code}"
                else:
                    full_link = share_link
                
                result = {"link": full_link, "code": share_code or "无"}
                logging.info(f"✅ 提取成功: {full_link}")
                return result
            else:
                logging.warning("未找到115链接")
                await page.screenshot(path="error_no_115_link.png")
                return None
        except Exception as e:
            logging.error(f"❌ 提取出错: {e}")
            await page.screenshot(path="error_fetch_link.png")
            return None
        finally:
            await browser.close()

async def get_user_points():
    """获取当前用户的积分信息"""
    async with async_playwright() as p:
        browser, context = await get_browser_context(p)
        page = await context.new_page()
        try:
            # 优先使用环境变量中的用户ID
            user_id = HDHIVE_USER_ID
            
            if not user_id:
                # 如果没有配置用户ID，尝试从页面获取
                logging.info("未配置HDHIVE_USER_ID，尝试从页面获取...")
                await page.goto("https://hdhive.com/", wait_until="domcontentloaded", timeout=BROWSER_TIMEOUT)
                
                # 检查是否需要登录
                if "/login" in page.url or await page.locator("#username").count() > 0:
                    await login_logic(page, context)
                    await page.goto("https://hdhive.com/", wait_until="domcontentloaded")
                
                await page.wait_for_timeout(SHORT_WAIT)
                
                # 从页面中获取用户ID
                user_links = await page.locator('a[href*="/user/"]').all()
                
                for link in user_links:
                    href = await link.get_attribute('href')
                    if href and '/user/' in href:
                        # 提取用户ID
                        match = re.search(r'/user/(\d+)', href)
                        if match:
                            user_id = match.group(1)
                            logging.info(f"📝 从页面提取到用户ID: {user_id}")
                            break
                
                if not user_id:
                    logging.error("❌ 无法获取用户ID，请在 .env 文件中配置 HDHIVE_USER_ID")
                    await page.screenshot(path="error_no_user_id.png")
                    return None
            else:
                logging.info(f"📝 使用配置的用户ID: {user_id}")
            
            # 访问用户页面API获取积分信息
            user_url = f"https://hdhive.com/user/{user_id}"
            
            logging.info(f"🌐 正在访问用户页面: {user_url}")
            await page.goto(user_url, wait_until="domcontentloaded", timeout=BROWSER_TIMEOUT)
            
            # 直接从页面内容中提取积分
            logging.info("📥 正在从页面提取积分信息...")
            page_content = await page.content()
            
            # 尝试多种正则模式提取points字段
            patterns = [
                r'\\"points\\":\s*(\d+)',  # 转义的JSON: \"points\":691
                r'"points"\s*:\s*(\d+)',  # 普通JSON: "points": 691
                r'"points"\s*:\s*"(\d+)"',  # 字符串格式: "points": "691"  
                r'\\u0022points\\u0022\s*:\s*(\d+)',  # Unicode转义
                r'points["\s]*:["\s]*(\d+)',  # 兼容格式
            ]
            
            points = None
            for pattern in patterns:
                match = re.search(pattern, page_content)
                if match:
                    points = int(match.group(1))
                    logging.info(f"✅ 成功提取积分: {points} (使用模式: {pattern})")
                    break
            
            if points is not None:
                return points
            else:
                # 保存页面内容用于调试
                with open('debug_page_content.html', 'w', encoding='utf-8') as f:
                    f.write(page_content)
                logging.error("❌ 未能从页面中提取积分，页面内容已保存到 debug_page_content.html")
                await page.screenshot(path="error_no_points_data.png")
                return None
                
        except Exception as e:
            logging.error(f"❌ 获取用户积分失败: {e}")
            await page.screenshot(path="error_get_points.png")
            return None
        finally:
            await browser.close()

async def unlock_and_fetch(resource_id):
    """解锁资源并获取链接 - 新版本"""
    url = f"https://hdhive.com/resource/{resource_id}"
    async with async_playwright() as p:
        browser, context = await get_browser_context(p)
        page = await context.new_page()
        try:
            logging.info(f"🔓 正在解锁资源: {url}")
            await page.goto(url, wait_until="domcontentloaded", timeout=BROWSER_TIMEOUT)
            
            # 检查登录状态
            if "/login" in page.url or await page.locator("#username").count() > 0:
                await login_logic(page, context)
                await page.goto(url, wait_until="domcontentloaded")

            await page.wait_for_timeout(MEDIUM_WAIT)
            
            # 步骤1: 从特定父元素查找积分支付按钮
            logging.info("🔍 步骤1: 查找积分支付按钮...")
            
            # 从父容器 mui-7texn5 中查找按钮（更精确的定位）
            parent_container = page.locator('div.mui-7texn5')
            
            if await parent_container.count() > 0:
                logging.info("✅ 找到按钮父容器")
                unlock_btn = parent_container.locator('button.MuiLoadingButton-root:has-text("确定解锁")')
            else:
                logging.info("⚠️ 父容器未找到，使用备用选择器")
                # 备用：直接查找按钮
                unlock_btn = page.locator('button.MuiLoadingButton-root:has(span.MuiLoadingButton-label:text("确定解锁"))')
            
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
            
            # 截图：点击前
            await page.screenshot(path="debug_before_unlock_click.png")
            logging.info("📸 点击前截图已保存")
            
            # 步骤3: 点击按钮（使用JS强制点击避免被遮挡）
            logging.info("🖱️ 步骤3: 点击解锁按钮...")
            await unlock_btn.first.evaluate("element => element.click()")
            logging.info("✅ 已点击解锁按钮")
            
            # 步骤4: 等待页面处理（等待跳转到115页面）
            logging.info("⏳ 步骤4: 等待页面跳转...")
            
            try:
                # 等待URL变化到115域名（最多等待10秒）
                await page.wait_for_url(
                    lambda url: '115.com/s/' in url or '115cdn.com/s/' in url,
                    timeout=10000
                )
                logging.info("✅ 页面已跳转到115链接")
            except Exception as e:
                logging.warning(f"⚠️ 等待跳转超时: {e}")
                # 等待一段时间后检查
                await page.wait_for_timeout(LONG_WAIT)
            
            # 步骤5: 返回跳转的115分享链接
            logging.info("🔗 步骤5: 获取115分享链接...")
            current_url = page.url
            logging.info(f"当前URL: {current_url}")
            
            # 截图：跳转后
            await page.screenshot(path="debug_after_unlock_redirect.png")
            logging.info("📸 跳转后截图已保存")
            
            # 验证是否跳转到115页面
            if '115.com/s/' in current_url or '115cdn.com/s/' in current_url:
                # 提取链接和提取码
                share_link = current_url.split('?')[0] if '?' in current_url else current_url
                parsed = urlparse(current_url)
                params = parse_qs(parsed.query)
                share_code = params.get('password', [None])[0]
                
                # 构建完整链接
                if share_code and '?' not in share_link:
                    full_link = f"{share_link}?password={share_code}"
                else:
                    full_link = share_link
                
                result = {"link": full_link, "code": share_code or "无"}
                logging.info(f"✅ 解锁成功！链接: {full_link}")
                return result
            else:
                logging.error(f"❌ 未跳转到115页面，当前URL: {current_url}")
                await page.screenshot(path="error_unlock_no_redirect.png")
                return None
                
        except Exception as e:
            logging.error(f"❌ 解锁过程出错: {e}")
            import traceback
            logging.error(traceback.format_exc())
            await page.screenshot(path="error_unlock_exception.png")
            return None
        finally:
            # 步骤6: 关闭浏览器
            logging.info("🔚 步骤6: 关闭浏览器")
            await browser.close()


# ================= TG 指令处理 =================

async def parse_hdhive_link(text):
    """
    解析 HDHive 链接
    返回: {"type": "resource|tmdb|none", "id": "...", "media_type": "movie|tv|none"}
    """
    # 资源链接: https://hdhive.com/resource/[uuid]
    resource_match = re.search(r'hdhive\.com/resource/([a-f0-9]+)', text)
    if resource_match:
        return {"type": "resource", "id": resource_match.group(1), "media_type": None}
    
    # TMDB页面链接: https://hdhive.com/tmdb/movie/12345 或 https://hdhive.com/tmdb/tv/67890
    tmdb_match = re.search(r'hdhive\.com/tmdb/(movie|tv)/(\d+)', text)
    if tmdb_match:
        return {"type": "tmdb", "media_type": tmdb_match.group(1), "id": tmdb_match.group(2)}
    
    # 简化链接: https://hdhive.com/movie/[uuid] 或 https://hdhive.com/tv/[uuid]
    short_match = re.search(r'hdhive\.com/(movie|tv)/([a-f0-9]+)', text)
    if short_match:
        return {"type": "tmdb", "media_type": short_match.group(1), "id": short_match.group(2)}
    
    return {"type": "none", "id": None, "media_type": None}

async def handle_search(message: types.Message, search_type: str):
    # 权限检查
    if not await check_user_permission(message):
        return
    
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        cmd = "/hdt" if search_type == "tv" else "/hdm"
        await message.reply(f"请使用: <code>{cmd} 名字或链接</code>", parse_mode="HTML")
        return
    
    user_input = args[1]
    
    # 解析链接
    link_info = await parse_hdhive_link(user_input)
    
    # 优先级1: 资源直接链接
    if link_info["type"] == "resource":
        resource_id = link_info["id"]
        wait_msg = await message.reply(
            f"🔗 <b>检测到资源链接</b>\n\n"
            f"🆔 <code>{resource_id}</code>\n"
            f"⏳ 正在提取链接...",
            parse_mode="HTML"
        )
        
        result = await fetch_download_link(resource_id)
        
        if result and result.get("need_unlock"):
            # 需要解锁
            points = result["points"]
            user_points = await get_user_points()
            
            if user_points is None:
                await wait_msg.edit_text(
                    f"❌ <b>无法获取积分信息</b>\n\n"
                    f"该资源需要 <code>{points}</code> 积分解锁",
                    parse_mode="HTML"
                )
                return
            
            if user_points < points:
                await wait_msg.edit_text(
                    f"❌ <b>积分不足</b>\n\n"
                    f"需要: <code>{points}</code> 积分\n"
                    f"当前: <code>{user_points}</code> 积分\n"
                    f"缺少: <code>{points - user_points}</code> 积分",
                    parse_mode="HTML"
                )
                return
            
            # 询问是否解锁
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [
                    InlineKeyboardButton(text="✅ 确定解锁", callback_data=f"unlock:{resource_id}"),
                    InlineKeyboardButton(text="❌ 取消", callback_data="cancel_unlock")
                ]
            ])
            
            await wait_msg.edit_text(
                f"🔓 <b>资源需要解锁</b>\n\n"
                f"🆔 <code>{resource_id}</code>\n"
                f"💰 需要积分: <code>{points}</code>\n"
                f"💳 当前积分: <code>{user_points}</code>\n"
                f"📊 解锁后剩余: <code>{user_points - points}</code>\n\n"
                f"是否确定解锁?",
                reply_markup=kb,
                parse_mode="HTML"
            )
            return
        
        elif result and result.get("link"):
            # 成功提取链接
            link = result["link"]
            code = result.get("code", "无")
            
            # 构建链接按钮
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔗 打开115网盘", url=link)]
            ])
            
            # 如果配置了SA，添加添加到SA按钮
            if SA_URL and SA_PARENT_ID:
                kb.inline_keyboard.append([
                    InlineKeyboardButton(text="📤 添加到SA", callback_data=f"send_to_group:{link}")
                ])
            
            await wait_msg.edit_text(
                f"✅ <b>提取成功</b>\n\n"
                f"<blockquote>\n"
                f"🔗 <a href='{link}'>115网盘链接</a>\n"
                f"🔑 提取码: <code>{code}</code>\n"
                f"</blockquote>",
                reply_markup=kb,
                parse_mode="HTML",
                disable_web_page_preview=True
            )
            return
        else:
            await wait_msg.edit_text(f"❌ 提取失败，请检查链接是否有效", parse_mode="HTML")
            return
    
    # 优先级2: TMDB页面链接
    if link_info["type"] == "tmdb":
        tmdb_id = link_info["id"]
        media_type = link_info["media_type"]
        type_name = "🎬 电影" if media_type == "movie" else "📺 剧集"
        
        wait_msg = await message.reply(
            f"🔗 <b>检测到{type_name}页面</b>\n\n"
            f"🆔 TMDB ID: <code>{tmdb_id}</code>\n"
            f"⏳ 正在获取资源列表...",
            parse_mode="HTML"
        )
        
        resources = await get_resources_by_tmdb_id(tmdb_id, media_type)
        
        if not resources:
            await wait_msg.edit_text(f"❌ 该{type_name}暂无资源", parse_mode="HTML")
            return
        
        # 构建按钮列表
        kb = InlineKeyboardMarkup(inline_keyboard=[])
        button_row = []
        
        for idx, res in enumerate(resources, 1):
            btn = InlineKeyboardButton(
                text=f"{idx}",
                callback_data=f"{media_type}_{idx}:{res['id']}"
            )
            button_row.append(btn)
            
            if len(button_row) == 5 or idx == len(resources):
                kb.inline_keyboard.append(button_row)
                button_row = []
        
        # 构建资源列表文本
        type_emoji = "🎬" if media_type == "movie" else "📺"
        result_text = f"{type_emoji} <b>{type_name} · {len(resources)} 项资源</b>\n"
        result_text += "─────────────────\n"
        
        for idx, res in enumerate(resources, 1):
            result_text += f"\n<blockquote>\n<b>{idx}.</b> {res['title']}\n"
            uploader = res.get('uploader', '未知用户')
            points_status = res.get('points', '未知')
            result_text += f"{uploader} · <code>{points_status}</code>\n"
            
            if res.get('tags'):
                quality_tags = []
                subtitle_tags = []
                format_tags = []
                encode_tags = []
                feature_tags = []
                size_tags = []
                
                for tag in res['tags']:
                    if any(q in tag for q in ['4K', '1080', '2160', '720', 'HDR', 'DV', 'SDR']):
                        quality_tags.append(tag)
                    elif any(s in tag for s in ['简中', '繁中', '英', '双语', '中文']):
                        subtitle_tags.append(tag)
                    elif any(f in tag for f in ['蓝光原盘', 'BluRay', 'Blu-ray', 'ISO']):
                        format_tags.append(tag)
                    elif any(e in tag for e in ['REMUX', 'BDRip', 'WEB-DL', 'WEB', 'BluRayEncode']):
                        encode_tags.append(tag)
                    elif any(ft in tag for ft in ['内封', '外挂', '内嵌']):
                        feature_tags.append(tag)
                    elif any(size_char in tag for size_char in ['G', 'M', 'T']) and any(c.isdigit() for c in tag):
                        size_tags.append(tag)
                
                tag_parts = []
                if quality_tags:
                    tag_parts.append(" / ".join(quality_tags))
                if encode_tags:
                    tag_parts.append(" / ".join(encode_tags))
                if format_tags:
                    tag_parts.append(" / ".join(format_tags))
                if subtitle_tags:
                    tag_parts.append(" / ".join(subtitle_tags))
                if feature_tags:
                    tag_parts.append(" / ".join(feature_tags))
                if size_tags:
                    tag_parts.append(" / ".join(size_tags))
                
                if tag_parts:
                    result_text += " / ".join(tag_parts) + "\n"
            
            result_text += "</blockquote>"
        
        result_text += "\n─────────────────\n"
        result_text += "<b>轻触数字获取链接</b>"
        
        await wait_msg.edit_text(result_text, reply_markup=kb, parse_mode="HTML")
        return
    
    # 优先级3: 关键词搜索（原有功能）
    keyword = user_input
    type_name = "📺 剧集" if search_type == "tv" else "🎬 电影"
    media_type = "tv" if search_type == "tv" else "movie"  # 提前定义，避免作用域问题
    wait_msg = await message.reply(f"搜索中 · {keyword}", parse_mode="HTML")
    
    try:
        tmdb_info = None
        # 优先尝试使用TMDB API
        if TMDB_API_KEY:
            tmdb_result = await search_tmdb(keyword, media_type)
            
            if tmdb_result:
                # 检查是否返回的是列表(多个结果)
                if isinstance(tmdb_result, list):
                    # 多个搜索结果,让用户选择
                    await wait_msg.delete()
                    
                    result_text = f"🔍 <b>找到 {len(tmdb_result)} 个匹配结果</b>\n"
                    result_text += "─────────────────\n"
                    result_text += "请选择你要的是哪一个:\n"
                    
                    # 构建选择按钮
                    kb = InlineKeyboardMarkup(inline_keyboard=[])
                    
                    for idx, item in enumerate(tmdb_result, 1):
                        # 显示每个结果的信息
                        overview = item.get('overview', '暂无简介')
                        if len(overview) > 100:
                            overview = overview[:100] + "…"
                        
                        result_text += f"\n<blockquote>\n"
                        result_text += f"<b>{idx}. {item['title']}</b>\n"
                        result_text += f"📅 {item.get('release_date', '未知')}"
                        if item.get('rating'):
                            result_text += f" · ⭐️ {item['rating']:.1f}\n"
                        else:
                            result_text += "\n"
                        result_text += f"{overview}\n"
                        result_text += f"</blockquote>"
                        
                        # 添加选择按钮
                        btn = InlineKeyboardButton(
                            text=f"{idx}",
                            callback_data=f"select_tmdb:{item['tmdb_id']}:{item['media_type']}"
                        )
                        kb.inline_keyboard.append([btn])
                    
                    result_text += "\n─────────────────\n"
                    result_text += "<b>点击数字选择</b>"
                    
                    await message.reply(result_text, reply_markup=kb, parse_mode="HTML")
                    return
                
                # 单个结果(完全匹配)
                tmdb_info = tmdb_result
                tmdb_id = tmdb_result["tmdb_id"]
                result_type = tmdb_result["media_type"]
                title = tmdb_result["title"]
                
                # 发送TMDB信息（图片+简介）
                info_text = f"<b>{title}</b>\n"
                if tmdb_result.get("release_date"):
                    info_text += f"{tmdb_result['release_date']}"
                if tmdb_result.get("rating"):
                    info_text += f" · ⭐️ {tmdb_result['rating']:.1f}\n"
                else:
                    info_text += "\n"
                
                # 简介长度限制
                overview = tmdb_result.get('overview', '暂无简介')
                if len(overview) > 200:
                    overview = overview[:200] + "…"
                
                info_text += f"\n{overview}\n\n"
                info_text += "正在获取资源…"
                
                # 如果有海报图片，发送图片+文字
                if tmdb_result.get("poster_url"):
                    try:
                        from aiogram.types import InputMediaPhoto
                        await message.answer_photo(
                            photo=tmdb_result["poster_url"],
                            caption=info_text,
                            parse_mode="HTML"
                        )
                        await wait_msg.delete()
                    except Exception as e:
                        logging.warning(f"发送图片失败: {e}")
                        await wait_msg.edit_text(info_text, parse_mode="HTML")
                else:
                    await wait_msg.edit_text(info_text, parse_mode="HTML")
                
                resources = await get_resources_by_tmdb_id(tmdb_id, result_type)
            else:
                # TMDB API没找到，使用网页搜索
                logging.info("TMDB API未找到，使用网页搜索")
                await wait_msg.edit_text(f"网页搜索中…", parse_mode="HTML")
                resources = await search_resources(keyword, search_type)
        else:
            # 没有配置TMDB API，使用网页搜索
            resources = await search_resources(keyword, search_type)
            
    except Exception as e:
        await wait_msg.edit_text(f"❌ 运行出错: {e}")
        return
    
    if not resources:
        error_msg = f"未找到资源"
        if tmdb_info:
            # 如果有TMDB信息但没有资源，单独发送提示
            await message.reply(error_msg)
        else:
            await wait_msg.edit_text(error_msg)
        return

    # 构建按钮列表 - 横向排列
    kb = InlineKeyboardMarkup(inline_keyboard=[])
    
    # 构建显示文本 - 使用 HTML 和 blockquote
    type_emoji = "🎬" if media_type == "movie" else "📺"
    result_text = f"{type_emoji} <b>{type_name} · {len(resources)} 项资源</b>\n"
    result_text += "─────────────────\n"
    
    # 按钮横向排列（每行最多5个按钮）
    button_row = []
    for idx, res in enumerate(resources, 1):
        # 简洁按钮 - 只有数字
        btn = InlineKeyboardButton(
            text=f"{idx}",
            callback_data=f"{media_type}_{idx}:{res['id']}"
        )
        button_row.append(btn)
        
        # 每5个按钮换一行，或者是最后一个资源
        if len(button_row) == 5 or idx == len(resources):
            kb.inline_keyboard.append(button_row)
            button_row = []
    
    # === 构建资源卡片内容 - 每个资源独立的绿色引用块 ===
    for idx, res in enumerate(resources, 1):
        result_text += f"\n<blockquote>\n<b>{idx}.</b> {res['title']}\n"
        
        # 用户和积分信息
        uploader = res.get('uploader', '未知用户')
        points_status = res.get('points', '未知')
        
        result_text += f"{uploader} · <code>{points_status}</code>\n"
        
        # 标签展示
        if res.get('tags'):
            # 分类标签
            quality_tags = []
            subtitle_tags = []
            format_tags = []
            encode_tags = []
            feature_tags = []
            size_tags = []
            
            for tag in res['tags']:
                if any(q in tag for q in ['4K', '1080', '2160', '720', 'HDR', 'DV', 'SDR']):
                    quality_tags.append(tag)
                elif any(s in tag for s in ['简中', '繁中', '英', '双语', '中文']):
                    subtitle_tags.append(tag)
                elif any(f in tag for f in ['蓝光原盘', 'BluRay', 'Blu-ray', 'ISO']):
                    format_tags.append(tag)
                elif any(e in tag for e in ['REMUX', 'BDRip', 'WEB-DL', 'WEB', 'BluRayEncode']):
                    encode_tags.append(tag)
                elif any(ft in tag for ft in ['内封', '外挂', '内嵌']):
                    feature_tags.append(tag)
                elif any(size_char in tag for size_char in ['G', 'M', 'T']) and any(c.isdigit() for c in tag):
                    size_tags.append(tag)
            
            # 构建标签行（用 / 分隔）
            tag_parts = []
            
            if quality_tags:
                tag_parts.append(" / ".join(quality_tags))
            if encode_tags:
                tag_parts.append(" / ".join(encode_tags))
            if format_tags:
                tag_parts.append(" / ".join(format_tags))
            if subtitle_tags:
                tag_parts.append(" / ".join(subtitle_tags))
            if feature_tags:
                tag_parts.append(" / ".join(feature_tags))
            if size_tags:
                tag_parts.append(" · ".join(size_tags))
            
            if tag_parts:
                result_text += " · ".join(tag_parts) + "\n"
        
        result_text += "</blockquote>"
    
    result_text += "\n─────────────────\n"
    result_text += "<b>轻触数字获取链接</b>"
    
    # 发送资源列表
    if tmdb_info:
        # 如果之前发送了TMDB信息，这里只发送资源列表
        await message.reply(result_text, reply_markup=kb, parse_mode="HTML")
    else:
        # 没有TMDB信息，更新原消息
        await wait_msg.edit_text(result_text, reply_markup=kb, parse_mode="HTML")

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    """启动命令 - 检查Bot状态"""
    # 权限检查
    if not await check_user_permission(message):
        return
    
    status_text = (
        "✅ <b>Bot运行正常</b>\n\n"
        f"🆔 Bot ID: <code>{bot.id}</code>\n"
        f"👤 你的ID: <code>{message.from_user.id}</code>\n\n"
        "💡 发送 /help 查看使用帮助"
    )
    
    await message.reply(status_text, parse_mode="HTML")

@dp.message(Command("help"))
async def cmd_help(message: types.Message):
    """显示帮助信息"""
    # 权限检查
    if not await check_user_permission(message):
        return
    
    help_text = (
        "🤖 <b>Media Bot 使用指南</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "📋 <b>基本命令：</b>\n"
        "• <code>/hdt 剧名</code> - 搜索剧集\n"
        "• <code>/hdm 片名</code> - 搜索电影\n"
        "• <code>/points</code> - 查询积分余额\n"
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
    
    await message.reply(help_text, parse_mode="HTML")

@dp.message(Command("hdt"))
async def cmd_search_tv(message: types.Message):
    await handle_search(message, "tv")

@dp.message(Command("hdm"))
async def cmd_search_movie(message: types.Message):
    await handle_search(message, "movie")

@dp.message(Command("points"))
async def cmd_check_points(message: types.Message):
    """查询用户积分"""
    # 权限检查
    if not await check_user_permission(message):
        return
    
    wait_msg = await message.reply("查询中…", parse_mode="HTML")
    
    try:
        points = await get_user_points()
        
        if points is not None:
            text = (
                "<b>积分余额</b>\n"
                "─────────────────\n\n"
                f"<b>{points}</b> 积分\n\n"
                "─────────────────"
            )
            await wait_msg.edit_text(text, parse_mode="HTML")
        else:
            # 提供更详细的错误信息
            debug_files = []
            import glob
            for f in glob.glob("debug_response_*.txt") + glob.glob("error_*.png"):
                debug_files.append(f)
            
            error_text = "❌ 查询积分失败\n\n"
            if debug_files:
                error_text += f"🔍 已生成 {len(debug_files)} 个调试文件:\n"
                for f in debug_files[:5]:
                    error_text += f"• {f}\n"
                error_text += "\n请检查服务器日志和这些文件"
            else:
                error_text += "请检查:\n"
                error_text += "1. HDHIVE_USER_ID 是否配置正确\n"
                error_text += "2. 服务器日志输出"
            
            await wait_msg.edit_text(error_text, parse_mode="HTML")
    except Exception as e:
        logging.error(f"查询积分出错: {e}", exc_info=True)
        await wait_msg.edit_text(f"❌ 查询出错: {str(e)[:200]}", parse_mode="HTML")

@dp.message(Command("coin_debug"))
async def cmd_debug_points(message: types.Message):
    """调试积分查询功能"""
    await message.reply(
        "🔧 <b>积分查询调试模式</b>\n\n"
        f"配置的用户ID: <code>{HDHIVE_USER_ID or '未配置'}</code>\n"
        "开始详细调试...",
        parse_mode="HTML"
    )
    
    # 清理旧的调试文件
    import glob
    old_files = glob.glob("debug_response_*.txt") + glob.glob("error_*.png")
    for f in old_files:
        try:
            os.remove(f)
        except:
            pass
    
    # 执行查询
    points = await get_user_points()
    
    # 收集调试文件
    debug_files = glob.glob("debug_response_*.txt") + glob.glob("error_*.png")
    
    result_text = "🔍 <b>调试结果</b>\n\n"
    
    if points is not None:
        result_text += f"✅ 成功获取积分: <b>{points}</b>\n\n"
    else:
        result_text += "❌ 获取失败\n\n"
    
    result_text += f"📁 生成了 {len(debug_files)} 个调试文件:\n"
    for f in debug_files:
        file_size = os.path.getsize(f) / 1024  # KB
        result_text += f"• <code>{f}</code> ({file_size:.1f}KB)\n"
    
    result_text += "\n💡 请检查服务器上的这些文件和日志"
    
    await message.reply(result_text, parse_mode="HTML")

@dp.message(Command("test_entity"))
async def cmd_test_entity(message: types.Message):
    """测试消息entity结构"""
    await message.reply("✅ 收到测试命令，现在请转发或发送包含链接的消息...")

@dp.message(Command("debug"))  
async def cmd_debug_search(message: types.Message):
    """调试命令，输出详细日志"""
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.reply("请使用: <code>/debug 搜索词</code>", parse_mode="HTML")
        return
    
    keyword = args[1]
    await message.reply(f"🔧 开始调试搜索: <code>{keyword}</code>\n检查服务器日志和截图文件", parse_mode="HTML")
    
    try:
        # 使用默认搜索类型进行调试
        resources = await search_resources(keyword, "all")
        if resources:
            debug_text = f"✅ 调试完成，找到 {len(resources)} 个资源:\n"
            for i, res in enumerate(resources[:3], 1):
                debug_text += f"{i}. {res.get('title', '无标题')[:30]}...\n"
            await message.reply(debug_text)
        else:
            await message.reply("❌ 调试完成，但没有找到资源。请检查截图文件。")
    except Exception as e:
        await message.reply(f"❌ 调试出错: {e}")

# ================= 直接链接处理 (放在所有Command之后) =================
@dp.message(F.text | F.photo | F.video | F.document)
async def handle_direct_link(message: types.Message):
    """处理直接发送的链接（无需命令）- 从 entities 提取，支持图片/视频的caption"""
    # 权限检查
    if not await check_user_permission(message):
        return
    
    # 获取文本内容 (text 或 caption)
    text = message.text or message.caption
    
    if not text:
        return
    
    # 如果是命令，跳过（由其他handler处理）
    if text.startswith('/'):
        return
    
    # 获取 entities (text 或 caption_entities)
    entities = message.entities or message.caption_entities
    
    # 从 entities 中提取所有链接
    urls = []
    if entities:
        for entity in entities:
            # url: 纯文本URL
            # text_link: 超链接(文字背后的URL)
            if entity.type == "url":
                # 从消息文本中提取URL
                url = text[entity.offset:entity.offset + entity.length]
                urls.append(url)
            elif entity.type == "text_link":
                # 直接从entity.url获取
                urls.append(entity.url)
    
    # 如果没有找到任何链接，忽略
    if not urls:
        return
    
    # 过滤出 hdhive.com 链接
    hdhive_urls = [url for url in urls if 'hdhive.com' in url]
    
    if not hdhive_urls:
        return
    
    logging.info(f"📎 从消息中提取到 {len(hdhive_urls)} 个HDHive链接: {hdhive_urls}")
    
    # 优先处理resource链接（即使消息中有其他链接）
    resource_url = None
    for url in hdhive_urls:
        if '/resource/' in url:
            resource_url = url
            break
    
    if resource_url:
        # 从URL中提取resource ID
        resource_match = re.search(r'/resource/([a-f0-9]+)', resource_url)
        if resource_match:
            resource_id = resource_match.group(1)
        
        wait_msg = await message.reply(
            f"🔗 <b>检测到资源链接</b>\n\n"
            f"🆔 <code>{resource_id}</code>\n"
            f"⏳ 正在提取链接...",
            parse_mode="HTML"
        )
        
        result = await fetch_download_link(resource_id)
        
        if result and result.get("need_unlock"):
            # 需要解锁
            points = result["points"]
            user_points = await get_user_points()
            
            if user_points is None:
                await wait_msg.edit_text(
                    f"❌ <b>无法获取积分信息</b>\n\n"
                    f"该资源需要 <code>{points}</code> 积分解锁",
                    parse_mode="HTML"
                )
                return
            
            if user_points < points:
                await wait_msg.edit_text(
                    f"❌ <b>积分不足</b>\n\n"
                    f"需要: <code>{points}</code> 积分\n"
                    f"当前: <code>{user_points}</code> 积分\n"
                    f"缺少: <code>{points - user_points}</code> 积分",
                    parse_mode="HTML"
                )
                return
            
            # 询问是否解锁
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [
                    InlineKeyboardButton(text="✅ 确定解锁", callback_data=f"unlock:{resource_id}"),
                    InlineKeyboardButton(text="❌ 取消", callback_data="cancel_unlock")
                ]
            ])
            
            await wait_msg.edit_text(
                f"🔓 <b>资源需要解锁</b>\n\n"
                f"🆔 <code>{resource_id}</code>\n"
                f"💰 需要积分: <code>{points}</code>\n"
                f"💳 当前积分: <code>{user_points}</code>\n"
                f"📊 解锁后剩余: <code>{user_points - points}</code>\n\n"
                f"是否确定解锁?",
                reply_markup=kb,
                parse_mode="HTML"
            )
            return
        
        elif result and result.get("link"):
            # 成功提取链接
            link = result["link"]
            code = result.get("code", "无")
            
            # 构建链接按钮
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔗 打开115网盘", url=link)]
            ])
            
            # 如果配置了SA，添加添加到SA按钮
            if SA_URL and SA_PARENT_ID:
                kb.inline_keyboard.append([
                    InlineKeyboardButton(text="📤 添加到SA", callback_data=f"send_to_group:{link}")
                ])
            
            await wait_msg.edit_text(
                f"✅ <b>提取成功</b>\n\n"
                f"<blockquote>\n"
                f"🔗 <a href='{link}'>115网盘链接</a>\n"
                f"🔑 提取码: <code>{code}</code>\n"
                f"</blockquote>",
                reply_markup=kb,
                parse_mode="HTML",
                disable_web_page_preview=True
            )
            return
        else:
            await wait_msg.edit_text(f"❌ 提取失败，请检查链接是否有效", parse_mode="HTML")
            return
    
    # 处理TMDB链接（tv/movie）- 从提取的URL中查找
    tmdb_url = None
    for url in hdhive_urls:
        if '/movie/' in url or '/tv/' in url:
            tmdb_url = url
            break
    
    if tmdb_url:
        # 解析TMDB链接
        link_info = await parse_hdhive_link(tmdb_url)
        if link_info["type"] == "tmdb":
            tmdb_id = link_info["id"]
            media_type = link_info["media_type"]
            type_name = "🎬 电影" if media_type == "movie" else "📺 剧集"
            
            wait_msg = await message.reply(
                f"🔗 <b>检测到{type_name}页面</b>\n\n"
                f"🆔 TMDB ID: <code>{tmdb_id}</code>\n"
                f"⏳ 正在获取资源列表...",
                parse_mode="HTML"
            )
            
            resources = await get_resources_by_tmdb_id(tmdb_id, media_type)
            
            if not resources:
                await wait_msg.edit_text(f"❌ 该{type_name}暂无资源", parse_mode="HTML")
                return
            
            # 构建按钮列表
            kb = InlineKeyboardMarkup(inline_keyboard=[])
            button_row = []
            
            for idx, res in enumerate(resources, 1):
                btn = InlineKeyboardButton(
                    text=f"{idx}",
                    callback_data=f"{media_type}_{idx}:{res['id']}"
                )
                button_row.append(btn)
                
                if len(button_row) == 5 or idx == len(resources):
                    kb.inline_keyboard.append(button_row)
                    button_row = []
            
            # 构建资源列表文本
            type_emoji = "🎬" if media_type == "movie" else "📺"
            result_text = f"{type_emoji} <b>{type_name} · {len(resources)} 项资源</b>\n"
            result_text += "─────────────────\n"
            
            for idx, res in enumerate(resources, 1):
                result_text += f"\n<blockquote>\n<b>{idx}.</b> {res['title']}\n"
                uploader = res.get('uploader', '未知用户')
                points_status = res.get('points', '未知')
                result_text += f"{uploader} · <code>{points_status}</code>\n"
                
                if res.get('tags'):
                    quality_tags = []
                    subtitle_tags = []
                    format_tags = []
                    encode_tags = []
                    feature_tags = []
                    size_tags = []
                    
                    for tag in res['tags']:
                        if any(q in tag for q in ['4K', '1080', '2160', '720', 'HDR', 'DV', 'SDR']):
                            quality_tags.append(tag)
                        elif any(s in tag for s in ['简中', '繁中', '英', '双语', '中文']):
                            subtitle_tags.append(tag)
                        elif any(f in tag for f in ['蓝光原盘', 'BluRay', 'Blu-ray', 'ISO']):
                            format_tags.append(tag)
                        elif any(e in tag for e in ['REMUX', 'BDRip', 'WEB-DL', 'WEB', 'BluRayEncode']):
                            encode_tags.append(tag)
                        elif any(ft in tag for ft in ['内封', '外挂', '内嵌']):
                            feature_tags.append(tag)
                        elif any(size_char in tag for size_char in ['G', 'M', 'T']) and any(c.isdigit() for c in tag):
                            size_tags.append(tag)
                    
                    tag_parts = []
                    if quality_tags:
                        tag_parts.append(" / ".join(quality_tags))
                    if encode_tags:
                        tag_parts.append(" / ".join(encode_tags))
                    if format_tags:
                        tag_parts.append(" / ".join(format_tags))
                    if subtitle_tags:
                        tag_parts.append(" / ".join(subtitle_tags))
                    if feature_tags:
                        tag_parts.append(" / ".join(feature_tags))
                    if size_tags:
                        tag_parts.append(" / ".join(size_tags))
                    
                    if tag_parts:
                        result_text += " / ".join(tag_parts) + "\n"
                
                result_text += "</blockquote>"
            
            result_text += "\n─────────────────\n"
            result_text += "<b>轻触数字获取链接</b>"
            
            await wait_msg.edit_text(result_text, reply_markup=kb, parse_mode="HTML")
            return

# ================= Callback Query 处理 =================
@dp.callback_query(F.data.regexp(r"^(movie|tv)_\d+:"))
async def on_resource_select(callback: CallbackQuery):
    # 解析 callback_data: movie_1:12345 或 tv_2:67890
    parts = callback.data.split(":")
    prefix = parts[0]  # movie_1 或 tv_2
    resource_id = parts[1]  # 资源ID
    
    # 提取序号
    resource_num = prefix.split("_")[1]
    
    # 显示加载状态
    loading_text = (
        "⏳ <b>正在提取链接...</b>\n\n"
        f"📦 资源 {resource_num}\n"
        f"🆔 <code>{resource_id}</code>\n"
        "🔄 请稍候，正在访问 115网盘..."
    )
    await callback.message.edit_text(loading_text, parse_mode="HTML")
    
    result = await fetch_download_link(resource_id)
    
    if result and result.get("need_unlock"):
        # 需要积分解锁
        points = result.get("points", 0)
        
        if points == 1:
            # 1积分直接解锁
            unlock_text = (
                "🔓 <b>需要解锁</b>\n\n"
                f"需要 {points} 积分\n"
                "正在自动解锁..."
            )
            await callback.message.edit_text(unlock_text, parse_mode="HTML")
            
            # 执行解锁
            unlock_result = await unlock_and_fetch(resource_id)
            
            if unlock_result:
                # 查询剩余积分
                remaining_points = await get_user_points()
                points_info = f"\n💎 剩余积分: <b>{remaining_points}</b>" if remaining_points is not None else ""
                
                text = (
                    "✅ <b>解锁并提取成功！</b>\n"
                    "━━━━━━━━━━━━━━━━\n\n"
                    f"🔗 <b>115网盘分享链接</b>\n"
                    f"<code>{unlock_result['link']}</code>\n\n"
                    f"💎 消耗积分: {points}"
                    f"{points_info}\n\n"
                    "━━━━━━━━━━━━━━━━\n"
                    "💡 <b>使用说明</b>\n"
                    "• 点击链接或复制到浏览器\n"
                    "• 如需提取码会自动填入\n"
                    "• 保存到你的115网盘即可"
                )
                
                # 添加添加到SA按钮
                if SA_URL and SA_PARENT_ID:
                    kb = InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="📤 添加到SA", callback_data=f"send_to_group:{unlock_result['link']}")]
                    ])
                    await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
                else:
                    await callback.message.edit_text(text, parse_mode="HTML")
            else:
                await callback.message.edit_text("❌ 解锁失败，请稍后重试", parse_mode="HTML")
        else:
            # 大于1积分，询问用户
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [
                    InlineKeyboardButton(text=f"✅ 确认解锁 ({points}积分)", callback_data=f"unlock:{resource_id}:{points}"),
                    InlineKeyboardButton(text="❌ 取消", callback_data="cancel_unlock")
                ]
            ])
            
            unlock_text = (
                "🔓 <b>需要积分解锁</b>\n"
                "━━━━━━━━━━━━━━━━\n\n"
                f"💎 需要积分: <b>{points}</b>\n"
                f"📦 资源ID: <code>{resource_id}</code>\n\n"
                "是否确认解锁？"
            )
            await callback.message.edit_text(unlock_text, reply_markup=kb, parse_mode="HTML")
    elif result:
        # 免费资源，直接显示链接
        text = (
            "✅ <b>提取成功！</b>\n"
            "━━━━━━━━━━━━━━━━\n\n"
            f"🔗 <b>115网盘分享链接</b>\n"
            f"<code>{result['link']}</code>\n\n"
            "━━━━━━━━━━━━━━━━\n"
            "💡 <b>使用说明</b>\n"
            "• 点击链接或复制到浏览器\n"
            "• 如需提取码会自动填入\n"
            "• 保存到你的115网盘即可"
        )
        
        # 添加添加到SA按钮
        if SA_URL and SA_PARENT_ID:
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📤 添加到SA", callback_data=f"send_to_group:{result['link']}")]
            ])
            await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
        else:
            await callback.message.edit_text(text, parse_mode="HTML")
    else:
        error_text = (
            "❌ <b>提取失败</b>\n"
            "━━━━━━━━━━━━━━━━\n\n"
            "可能的原因:\n"
            "• 资源需要验证码\n"
            "• 链接已失效\n"
            "• 网络连接问题\n\n"
            "💡 建议: 尝试其他资源或稍后重试"
        )
        await callback.message.edit_text(error_text, parse_mode="HTML")

@dp.callback_query(F.data.startswith("unlock:"))
async def on_confirm_unlock(callback: CallbackQuery):
    """确认解锁积分资源"""
    parts = callback.data.split(":")
    resource_id = parts[1]
    
    # 如果callback_data包含积分数（从资源选择来的），使用它
    # 否则需要先查询资源信息获取积分数
    if len(parts) > 2:
        points = int(parts[2])
    else:
        # 先获取资源信息看需要多少积分
        temp_result = await fetch_download_link(resource_id)
        if temp_result and temp_result.get("need_unlock"):
            points = temp_result.get("points", 0)
        else:
            await callback.message.edit_text("❌ 获取资源信息失败", parse_mode="HTML")
            return
    
    unlock_text = (
        "🔓 <b>正在解锁...</b>\n\n"
        f"💎 消耗积分: {points}\n"
        "⏳ 请稍候..."
    )
    await callback.message.edit_text(unlock_text, parse_mode="HTML")
    
    # 执行解锁
    result = await unlock_and_fetch(resource_id)
    
    if result:
        # 查询剩余积分
        remaining_points = await get_user_points()
        points_info = f"\n💎 剩余积分: <b>{remaining_points}</b>" if remaining_points is not None else ""
        
        text = (
            "✅ <b>解锁并提取成功！</b>\n"
            "━━━━━━━━━━━━━━━━\n\n"
            f"🔗 <b>115网盘分享链接</b>\n"
            f"<code>{result['link']}</code>\n\n"
            f"💎 消耗积分: {points}"
            f"{points_info}\n\n"
            "━━━━━━━━━━━━━━━━\n"
            "💡 <b>使用说明</b>\n"
            "• 点击链接或复制到浏览器\n"
            "• 如需提取码会自动填入\n"
            "• 保存到你的115网盘即可"
        )
        
        # 添加添加到SA按钮
        if SA_URL and SA_PARENT_ID:
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📤 添加到SA", callback_data=f"send_to_group:{result['link']}")]
            ])
            await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
        else:
            await callback.message.edit_text(text, parse_mode="HTML")
    else:
        await callback.message.edit_text("❌ 解锁失败，请稍后重试", parse_mode="HTML")

@dp.callback_query(F.data == "cancel_unlock")
async def on_cancel_unlock(callback: CallbackQuery):
    """取消解锁"""
    await callback.message.edit_text("❌ 已取消解锁", parse_mode="HTML")

@dp.callback_query(F.data.startswith("send_to_group:"))
async def on_send_to_group(callback: CallbackQuery):
    """发送115链接到SA（Symedia）"""
    try:
        # 提取链接
        link = callback.data.replace("send_to_group:", "")
        
        # 检查是否配置了SA
        if not SA_URL or not SA_PARENT_ID:
            await callback.answer("❌ 未配置SA，无法添加到Symedia", show_alert=True)
            return
        
        # 构建API URL
        api_url = f"{SA_URL}/api/v1/plugin/cloud_helper/add_share_urls_115?token=symedia"
        
        # 构建请求体
        payload = {
            "urls": [link],
            "parent_id": SA_PARENT_ID
        }
        
        # 显示处理状态
        await callback.answer("⏳ 正在添加到Symedia...", show_alert=False)
        
        # 发送POST请求
        async with aiohttp.ClientSession() as session:
            async with session.post(api_url, json=payload) as response:
                if response.status == 200:
                    data = await response.json()
                    message = data.get("message", "添加成功")
                    
                    # 通知用户成功
                    await callback.answer(f"✅ {message}", show_alert=True)
                    
                    # 发送详细通知消息
                    notification = (
                        "🎬 <b>已添加到Symedia</b>\n"
                        "━━━━━━━━━━━━━━━━\n\n"
                        f"� <b>状态:</b> {message}\n"
                        f"🔗 <b>链接:</b> <code>{link}</code>\n"
                        f"� <b>目录ID:</b> <code>{SA_PARENT_ID}</code>"
                    )
                    
                    await callback.message.reply(notification, parse_mode="HTML")
                    
                    # 移除按钮
                    await callback.message.edit_reply_markup(reply_markup=None)
                    
                    logging.info(f"✅ 成功添加到SA: {link} - {message}")
                else:
                    error_text = await response.text()
                    logging.error(f"❌ SA API返回错误: {response.status} - {error_text}")
                    await callback.answer(f"❌ 添加失败: HTTP {response.status}", show_alert=True)
        
    except aiohttp.ClientError as e:
        logging.error(f"❌ 网络请求失败: {e}")
        await callback.answer("❌ 网络请求失败，请检查SA配置", show_alert=True)
    except Exception as e:
        logging.error(f"❌ 添加到SA失败: {e}")
        await callback.answer(f"❌ 添加失败: {str(e)}", show_alert=True)

@dp.callback_query(F.data.startswith("select_tmdb:"))
async def on_select_tmdb(callback: CallbackQuery):
    """用户选择了TMDB搜索结果"""
    try:
        # 解析数据: select_tmdb:12345:movie
        parts = callback.data.split(":")
        tmdb_id = parts[1]
        media_type = parts[2]
        
        # 显示加载状态
        await callback.message.edit_text("⏳ 正在获取TMDB信息...", parse_mode="HTML")
        
        # 获取TMDB详细信息
        tmdb_info = await get_tmdb_details(int(tmdb_id), media_type)
        
        if tmdb_info:
            # 构建TMDB信息文本
            info_text = f"<b>{tmdb_info['title']}</b>\n"
            if tmdb_info.get("release_date"):
                info_text += f"{tmdb_info['release_date']}"
            if tmdb_info.get("rating"):
                info_text += f" · ⭐️ {tmdb_info['rating']:.1f}\n"
            else:
                info_text += "\n"
            
            # 简介长度限制
            overview = tmdb_info.get('overview', '暂无简介')
            if len(overview) > 200:
                overview = overview[:200] + "…"
            
            info_text += f"\n{overview}\n\n"
            info_text += "正在获取资源…"
            
            # 如果有海报图片，发送图片+文字
            if tmdb_info.get("poster_url"):
                try:
                    from aiogram.types import InputMediaPhoto
                    await callback.message.answer_photo(
                        photo=tmdb_info["poster_url"],
                        caption=info_text,
                        parse_mode="HTML"
                    )
                    await callback.message.delete()
                except Exception as e:
                    logging.warning(f"发送图片失败: {e}")
                    await callback.message.edit_text(info_text, parse_mode="HTML")
            else:
                await callback.message.edit_text(info_text, parse_mode="HTML")
        else:
            # 如果获取TMDB信息失败，继续显示加载状态
            await callback.message.edit_text("⏳ 正在获取资源...", parse_mode="HTML")
        
        # 获取资源列表
        resources = await get_resources_by_tmdb_id(int(tmdb_id), media_type)
        
        if not resources:
            # 如果有TMDB信息，在新消息中显示未找到资源
            if tmdb_info:
                await callback.message.answer("❌ 未找到资源")
            else:
                await callback.message.edit_text("❌ 未找到资源")
            return
        
        # 构建按钮列表
        kb = InlineKeyboardMarkup(inline_keyboard=[])
        
        # 构建显示文本
        type_emoji = "🎬" if media_type == "movie" else "📺"
        type_name = "电影" if media_type == "movie" else "剧集"
        result_text = f"{type_emoji} <b>{type_name} · {len(resources)} 项资源</b>\n"
        result_text += "─────────────────\n"
        
        # 按钮横向排列
        button_row = []
        for idx, res in enumerate(resources, 1):
            btn = InlineKeyboardButton(
                text=f"{idx}",
                callback_data=f"{media_type}_{idx}:{res['id']}"
            )
            button_row.append(btn)
            
            if len(button_row) == 5 or idx == len(resources):
                kb.inline_keyboard.append(button_row)
                button_row = []
        
        # 构建资源卡片内容
        for idx, res in enumerate(resources, 1):
            result_text += f"\n<blockquote>\n<b>{idx}.</b> {res['title']}\n"
            
            uploader = res.get('uploader', '未知用户')
            points_status = res.get('points', '未知')
            
            result_text += f"{uploader} · <code>{points_status}</code>\n"
            
            if res.get('tags'):
                quality_tags = []
                subtitle_tags = []
                format_tags = []
                encode_tags = []
                feature_tags = []
                size_tags = []
                
                for tag in res['tags']:
                    if any(q in tag for q in ['4K', '1080', '2160', '720', 'HDR', 'DV', 'SDR']):
                        quality_tags.append(tag)
                    elif any(s in tag for s in ['简中', '繁中', '英', '双语', '中文']):
                        subtitle_tags.append(tag)
                    elif any(f in tag for f in ['蓝光原盘', 'BluRay', 'Blu-ray', 'ISO']):
                        format_tags.append(tag)
                    elif any(e in tag for e in ['REMUX', 'BDRip', 'WEB-DL', 'WEB', 'BluRayEncode']):
                        encode_tags.append(tag)
                    elif any(ft in tag for ft in ['内封', '外挂', '内嵌']):
                        feature_tags.append(tag)
                    elif any(size_char in tag for size_char in ['G', 'M', 'T']) and any(c.isdigit() for c in tag):
                        size_tags.append(tag)
                
                tag_parts = []
                
                if quality_tags:
                    tag_parts.append(" / ".join(quality_tags))
                if encode_tags:
                    tag_parts.append(" / ".join(encode_tags))
                if format_tags:
                    tag_parts.append(" / ".join(format_tags))
                if subtitle_tags:
                    tag_parts.append(" / ".join(subtitle_tags))
                if feature_tags:
                    tag_parts.append(" / ".join(feature_tags))
                if size_tags:
                    tag_parts.append(" · ".join(size_tags))
                
                if tag_parts:
                    result_text += " · ".join(tag_parts) + "\n"
            
            result_text += "</blockquote>"
        
        result_text += "\n─────────────────\n"
        result_text += "<b>轻触数字获取链接</b>"
        
        # 如果已经发送了TMDB信息，在新消息中显示资源列表
        if tmdb_info and tmdb_info.get("poster_url"):
            await callback.message.answer(result_text, reply_markup=kb, parse_mode="HTML")
        else:
            await callback.message.edit_text(result_text, reply_markup=kb, parse_mode="HTML")
        
    except Exception as e:
        logging.error(f"❌ 处理TMDB选择失败: {e}")
        await callback.message.edit_text(f"❌ 处理失败: {e}", parse_mode="HTML")

async def main():
    logging.info("🚀 Media Bot 启动中...")
    
    # 启动时清除所有调试图片
    import glob
    debug_files = glob.glob("debug_*.png") + glob.glob("error_*.png")
    if debug_files:
        for file in debug_files:
            try:
                os.remove(file)
                logging.info(f"🗑️ 启动时清除调试图片: {file}")
            except:
                pass
        logging.info(f"✅ 启动时清除了 {len(debug_files)} 个调试图片")
    
    try:
        await dp.start_polling(bot)
    except Exception as e:
        logging.error(f"❌ Bot 运行出错: {e}")
        raise

if __name__ == "__main__":
    import nest_asyncio
    nest_asyncio.apply()
    asyncio.run(main())
