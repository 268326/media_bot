"""
浏览器管理模块
负责 Playwright 浏览器的启动、Cookie管理、登录逻辑
支持自动检测登录过期并重新登录
"""
import os
import logging
from playwright.async_api import async_playwright, Browser, BrowserContext, Page
from config import (
    COOKIE_FILE, HDHIVE_USER, HDHIVE_PASS,
    BROWSER_ARGS, VIEWPORT_SIZE, USER_AGENT,
    LOGIN_TIMEOUT, SHORT_WAIT, MEDIUM_WAIT
)


async def get_browser_context(p) -> tuple[Browser, BrowserContext]:
    """
    启动浏览器并加载 Cookie
    
    Args:
        p: Playwright 实例
        
    Returns:
        tuple: (browser, context)
    """
    browser = await p.chromium.launch(
        headless=True,
        args=BROWSER_ARGS
    )
    
    # 尝试加载已保存的 Cookie
    if os.path.exists(COOKIE_FILE):
        try:
            context = await browser.new_context(
                storage_state=COOKIE_FILE,
                viewport=VIEWPORT_SIZE
            )
            logging.info("✅ 成功加载已保存的 Cookie")
        except Exception as e:
            logging.warning(f"⚠️ Cookie 文件损坏，将重新登录: {e}")
            context = await browser.new_context(viewport=VIEWPORT_SIZE)
    else:
        context = await browser.new_context(viewport=VIEWPORT_SIZE)
        logging.info("📝 首次运行，将执行登录")
    
    # 设置 User-Agent
    await context.set_extra_http_headers({
        "User-Agent": USER_AGENT
    })
    
    return browser, context


async def login_logic(page: Page, context: BrowserContext, max_retries: int = 3):
    """
    执行登录操作（支持重试）
    
    Args:
        page: 页面对象
        context: 浏览器上下文
        max_retries: 最大重试次数
        
    Raises:
        Exception: 登录失败
    """
    for attempt in range(1, max_retries + 1):
        try:
            logging.info(f"正在执行登录操作... (尝试 {attempt}/{max_retries})")
            
            # 确保在登录页面
            if "/login" not in page.url:
                await page.goto("https://hdhive.com/login", wait_until="domcontentloaded", timeout=LOGIN_TIMEOUT)
            
            # 等待登录表单加载
            await page.wait_for_selector('#username', state='visible', timeout=10000)
            await page.wait_for_timeout(SHORT_WAIT)
            
            # 清空输入框（防止残留）
            await page.fill('#username', '')
            await page.fill('#password', '')
            await page.wait_for_timeout(500)
            
            # 填写登录表单
            await page.fill('#username', HDHIVE_USER)
            await page.fill('#password', HDHIVE_PASS)
            await page.wait_for_timeout(500)
            
            # 点击登录按钮
            await page.click('button[type="submit"]')
            logging.info("已点击登录按钮，等待响应...")
            
            # 等待登录成功（跳转离开登录页）
            try:
                await page.wait_for_url(lambda u: "/login" not in u, timeout=LOGIN_TIMEOUT)
            except Exception as e:
                # 检查是否有错误提示
                error_msg = await page.locator('text=/错误|失败|Error/i').first.inner_text() if await page.locator('text=/错误|失败|Error/i').count() > 0 else ""
                if error_msg:
                    logging.error(f"❌ 登录失败: {error_msg}")
                    await page.screenshot(path=f"error_login_attempt_{attempt}.png")
                    if attempt < max_retries:
                        logging.info(f"将在2秒后重试...")
                        await page.wait_for_timeout(2000)
                        continue
                    else:
                        raise Exception(f"登录失败: {error_msg}")
                raise
            
            # 验证登录成功
            await page.wait_for_timeout(MEDIUM_WAIT)
            
            # 检查是否真的登录成功
            current_url = page.url
            logging.info(f"登录后URL: {current_url}")
            
            # 保存 Cookie
            logging.info("✅ 登录成功，保存 Cookies...")
            await context.storage_state(path=COOKIE_FILE)
            
            logging.info(f"🎉 登录成功！(尝试次数: {attempt})")
            return
            
        except Exception as e:
            logging.error(f"❌ 登录尝试 {attempt} 失败: {e}")
            await page.screenshot(path=f"error_login_attempt_{attempt}.png")
            
            if attempt < max_retries:
                logging.info(f"将在 {2 * attempt} 秒后重试...")
                await page.wait_for_timeout(2000 * attempt)
            else:
                logging.error(f"❌ 登录失败，已重试 {max_retries} 次")
                raise Exception(f"登录失败: {e}")


async def check_and_login(page: Page, context: BrowserContext):
    """
    检查登录状态，如需要则自动登录
    
    检测条件：
    1. URL 包含 /login
    2. 页面存在登录表单元素
    3. 检测到 "登录" 或 "请登录" 文本
    
    Args:
        page: 页面对象
        context: 浏览器上下文
    """
    # 基本登录检测
    is_login_page = "/login" in page.url
    has_login_form = await page.locator("#username").count() > 0
    
    # 检测登录相关文本
    login_text_exists = False
    try:
        login_text_exists = await page.locator('text=/请登录|登录|Login/i').count() > 0
    except:
        pass
    
    # 检测是否被重定向到登录页
    page_title = ""
    try:
        page_title = await page.title()
    except:
        pass
    
    is_login_required = is_login_page or has_login_form or ("登录" in page_title and len(page_title) < 20)
    
    # 如果检测到需要登录
    if is_login_required:
        logging.warning("⚠️ 检测到登录已过期，正在重新登录...")
        await login_logic(page, context)
        return


async def ensure_logged_in(page: Page, context: BrowserContext, target_url: str = None):
    """
    确保已登录状态（先验证登录，再访问目标）
    
    适用场景：在执行关键操作前调用
    
    Args:
        page: 页面对象
        context: 浏览器上下文
        target_url: 目标URL（如果提供，登录后会跳转到此URL）
    """
    logging.info("🔐 验证登录状态...")
    
    # 第1步：访问首页检查登录状态
    try:
        await page.goto("https://hdhive.com/", wait_until="domcontentloaded", timeout=10000)
        await page.wait_for_timeout(1000)
    except:
        pass
    
    # 第2步：检查是否需要登录
    is_login_page = "/login" in page.url
    has_login_form = await page.locator("#username").count() > 0
    has_register_link = await page.locator('a[href="/register"]').count() > 0
    
    if is_login_page or has_login_form or has_register_link:
        logging.warning(f"⚠️ 检测到未登录状态，正在执行登录...")
        await login_logic(page, context)
        logging.info("✅ 登录验证完成")
    else:
        logging.info("✅ 已处于登录状态")
    
    # 第3步：如果指定了目标URL，访问目标页面
    if target_url:
        try:
            logging.info(f"� 访问目标页面: {target_url}")
            await page.goto(target_url, wait_until="domcontentloaded", timeout=15000)
            await page.wait_for_timeout(2000)  # 等待可能的延迟重定向
            
            # 检查是否又被重定向到登录页
            current_url = page.url
            if "/login" in current_url or await page.locator("#username").count() > 0:
                logging.warning(f"⚠️ 访问目标页面时被重定向到登录页: {current_url}")
                logging.warning(f"⚠️ 这可能是账号权限问题或目标页面需要特殊权限")
                raise Exception(f"无法访问目标页面，被重定向到: {current_url}")
            
            logging.info(f"✅ 成功访问目标页面: {current_url}")
            
        except Exception as e:
            logging.error(f"❌ 访问目标页面失败: {e}")
            raise
