"""
轻量会话管理器（接口模式）
保留原有调用接口，但不再维护浏览器会话。
"""
import logging


class SessionManager:
    """接口模式下的空实现，兼容旧调用路径。"""

    async def start(self):
        logging.info("✅ SessionManager(HTTP-only) 已启动")

    async def stop(self):
        logging.info("🛑 SessionManager(HTTP-only) 已停止")

    async def close_session(self, session_id: str):
        _ = session_id
        return

    def get_session_count(self) -> int:
        return 0


session_manager = SessionManager()
