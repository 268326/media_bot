"""
每日签到服务模块
使用浏览器模拟点击用户菜单中的“每日签到”
"""
import logging
import re

from playwright.async_api import async_playwright

from browser import get_browser_context, ensure_logged_in
from config import BROWSER_TIMEOUT, SHORT_WAIT, MEDIUM_WAIT


def _extract_points_from_html(content: str) -> int | None:
    """从页面内容中提取积分字段"""
    patterns = [
        r'\\"points\\":\s*(\d+)',
        r'"points"\s*:\s*(\d+)',
        r'"points"\s*:\s*"(\d+)"',
        r'\\u0022points\\u0022\s*:\s*(\d+)',
        r'points["\s]*:["\s]*(\d+)',
    ]
    for pattern in patterns:
        match = re.search(pattern, content)
        if match:
            return int(match.group(1))
    return None


async def _read_points(page) -> int | None:
    """读取当前页面中的积分数据"""
    try:
        html = await page.content()
        return _extract_points_from_html(html)
    except Exception:
        return None


async def daily_check_in() -> dict:
    """
    执行每日签到

    Returns:
        dict: {
            "success": bool,
            "already_checked_in": bool,
            "message": str,
            "before_points": int | None,
            "after_points": int | None
        }
    """
    async with async_playwright() as p:
        browser, context = await get_browser_context(p)
        page = await context.new_page()

        try:
            home_url = "https://hdhive.com/"
            await page.goto(home_url, wait_until="domcontentloaded", timeout=BROWSER_TIMEOUT)
            await ensure_logged_in(page, context, home_url)
            await page.wait_for_timeout(MEDIUM_WAIT)

            before_points = await _read_points(page)

            # 打开用户菜单（你的 DOM 指向 user-menu-button）
            menu_btn = page.locator("#user-menu-button")
            if await menu_btn.count() == 0:
                menu_btn = page.locator('button[aria-controls*="menu"], button[aria-haspopup="true"]')
            if await menu_btn.count() == 0:
                return {
                    "success": False,
                    "already_checked_in": False,
                    "message": "未找到用户菜单按钮",
                    "before_points": before_points,
                    "after_points": None,
                }

            await menu_btn.first.click()
            await page.wait_for_timeout(SHORT_WAIT)
            await page.wait_for_selector('ul[role="menu"]', timeout=5000)

            # 点击菜单中的“每日签到”
            checkin_item = page.locator('ul[role="menu"] li[role="menuitem"]:has-text("每日签到")')
            if await checkin_item.count() == 0:
                checkin_item = page.locator('ul[role="menu"] li[role="menuitem"]:has-text("签到")')
            if await checkin_item.count() == 0:
                return {
                    "success": False,
                    "already_checked_in": False,
                    "message": "未找到“每日签到”菜单项",
                    "before_points": before_points,
                    "after_points": None,
                }

            await checkin_item.first.click()
            await page.wait_for_timeout(MEDIUM_WAIT)

            # 读取提示文本（Snackbar/Alert）
            hint_text = ""
            alert = page.locator('[role="alert"]')
            if await alert.count() > 0:
                try:
                    hint_text = (await alert.first.inner_text()).strip()
                except Exception:
                    hint_text = ""

            if not hint_text:
                # fallback: 从页面中抓取关键词
                body_text = await page.locator("body").inner_text()
                if "今日已签到" in body_text:
                    hint_text = "今日已签到"
                elif "签到成功" in body_text:
                    hint_text = "签到成功"

            after_points = await _read_points(page)

            already = ("已签到" in hint_text) if hint_text else False
            success = already or ("签到成功" in hint_text) or (after_points is not None and before_points is not None and after_points >= before_points)

            if not hint_text:
                hint_text = "签到请求已发送"

            return {
                "success": success,
                "already_checked_in": already,
                "message": hint_text,
                "before_points": before_points,
                "after_points": after_points,
            }

        except Exception as e:
            logging.error(f"❌ 每日签到失败: {e}")
            try:
                await page.screenshot(path="error_daily_checkin.png")
            except Exception:
                pass
            return {
                "success": False,
                "already_checked_in": False,
                "message": str(e),
                "before_points": None,
                "after_points": None,
            }
        finally:
            await browser.close()
