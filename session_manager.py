"""
浏览器会话管理器
负责管理和复用浏览器会话，避免频繁创建/销毁
"""
import asyncio
import logging
import time
from typing import Dict, Optional, Tuple
from playwright.async_api import async_playwright, Browser, BrowserContext, Page, Playwright

from browser import get_browser_context


class BrowserSession:
    """单个浏览器会话"""
    
    def __init__(self, browser: Browser, context: BrowserContext, page: Page):
        self.browser = browser
        self.context = context
        self.page = page
        self.created_at = time.time()
        self.last_used = time.time()
        self.is_closing = False
    
    def touch(self):
        """更新最后使用时间"""
        self.last_used = time.time()
    
    def age(self) -> float:
        """返回会话年龄（秒）"""
        return time.time() - self.last_used
    
    async def close(self):
        """关闭会话"""
        if self.is_closing:
            return
        
        self.is_closing = True
        try:
            if self.page and not self.page.is_closed():
                await self.page.close()
            if self.context:
                await self.context.close()
            if self.browser and self.browser.is_connected():
                await self.browser.close()
            logging.info("🗑️ 浏览器会话已关闭")
        except Exception as e:
            logging.warning(f"⚠️ 关闭会话时出错: {e}")


class SessionManager:
    """浏览器会话管理器（单例）"""
    
    _instance = None
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance
    
    def __init__(self):
        if self._initialized:
            return
        
        self._initialized = True
        self.sessions: Dict[str, BrowserSession] = {}  # session_id -> BrowserSession
        self.playwright: Optional[Playwright] = None
        self.cleanup_task = None
        self.max_session_age = 300  # 5分钟未使用自动关闭
        self.max_sessions = 10  # 最大并发会话数
        
        logging.info("✅ SessionManager 初始化完成")
    
    async def start(self):
        """启动管理器"""
        if not self.playwright:
            self.playwright = await async_playwright().start()
            logging.info("🚀 Playwright 已启动")
        
        # 启动清理任务
        if not self.cleanup_task:
            self.cleanup_task = asyncio.create_task(self._cleanup_loop())
            logging.info("🧹 会话清理任务已启动")
    
    async def stop(self):
        """停止管理器"""
        # 取消清理任务
        if self.cleanup_task:
            self.cleanup_task.cancel()
            try:
                await self.cleanup_task
            except asyncio.CancelledError:
                pass
        
        # 关闭所有会话
        for session_id in list(self.sessions.keys()):
            await self.close_session(session_id)
        
        # 停止 Playwright
        if self.playwright:
            await self.playwright.stop()
            self.playwright = None
        
        logging.info("🛑 SessionManager 已停止")
    
    async def _cleanup_loop(self):
        """定期清理过期会话"""
        while True:
            try:
                await asyncio.sleep(60)  # 每分钟检查一次
                await self._cleanup_expired_sessions()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logging.error(f"❌ 清理任务出错: {e}")
    
    async def _cleanup_expired_sessions(self):
        """清理过期的会话"""
        expired_ids = []
        
        for session_id, session in self.sessions.items():
            if session.age() > self.max_session_age:
                expired_ids.append(session_id)
        
        for session_id in expired_ids:
            logging.info(f"⏰ 会话 {session_id[:8]}... 已过期，正在关闭")
            await self.close_session(session_id)
    
    async def create_session(self, session_id: str) -> BrowserSession:
        """
        创建新会话
        
        Args:
            session_id: 会话ID（通常是 user_id:resource_id）
            
        Returns:
            BrowserSession: 新创建的会话
        """
        # 检查是否已存在
        if session_id in self.sessions:
            logging.info(f"♻️ 复用现有会话: {session_id[:20]}...")
            self.sessions[session_id].touch()
            return self.sessions[session_id]
        
        # 检查会话数限制
        if len(self.sessions) >= self.max_sessions:
            logging.warning(f"⚠️ 会话数达到上限 ({self.max_sessions})，清理最旧的会话")
            await self._cleanup_oldest_session()
        
        # 创建新会话
        logging.info(f"🆕 创建新浏览器会话: {session_id[:20]}...")
        
        if not self.playwright:
            await self.start()
        
        browser, context = await get_browser_context(self.playwright)
        page = await context.new_page()
        
        session = BrowserSession(browser, context, page)
        self.sessions[session_id] = session
        
        logging.info(f"✅ 会话已创建 (当前活跃: {len(self.sessions)})")
        return session
    
    async def _cleanup_oldest_session(self):
        """清理最旧的会话"""
        if not self.sessions:
            return
        
        oldest_id = min(self.sessions.keys(), 
                       key=lambda k: self.sessions[k].last_used)
        await self.close_session(oldest_id)
    
    async def get_session(self, session_id: str) -> Optional[BrowserSession]:
        """
        获取会话（如果存在）
        
        Args:
            session_id: 会话ID
            
        Returns:
            BrowserSession | None: 会话对象或None
        """
        session = self.sessions.get(session_id)
        if session:
            session.touch()
        return session
    
    async def close_session(self, session_id: str):
        """
        关闭指定会话
        
        Args:
            session_id: 会话ID
        """
        session = self.sessions.pop(session_id, None)
        if session:
            await session.close()
            logging.info(f"🗑️ 会话已关闭: {session_id[:20]}... (剩余: {len(self.sessions)})")
    
    def get_session_count(self) -> int:
        """获取当前活跃会话数"""
        return len(self.sessions)


# 全局单例实例
session_manager = SessionManager()
