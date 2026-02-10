"""
HDHive HTTP 认证模块
负责 token 读取、过期判断、接口登录、auth.json 持久化。
"""
from __future__ import annotations

import base64
import json
import logging
import os
import threading
import time
from datetime import datetime, timezone

import requests

from config import (
    COOKIE_FILE,
    HDHIVE_ACTION_LOGIN,
    HDHIVE_BASE_URL,
    HDHIVE_LOGIN_RETRIES,
    HDHIVE_LOGIN_TIMEOUT,
    HDHIVE_PASS,
    HDHIVE_TOKEN,
    HDHIVE_USER,
)

logger = logging.getLogger(__name__)

LOGIN_STATE_TREE = (
    "%5B%22%22%2C%7B%22children%22%3A%5B%22(auth)%22%2C%7B%22children%22%3A%5B%22login%22%2C%7B%22children%22%3A%5B%22__PAGE__%22%2C%7B%7D%2Cnull%2Cnull%5D%7D%2Cnull%2Cnull%5D%7D%2Cnull%2Cnull%5D%7D%2Cnull%2Cnull%2Ctrue%5D"
)
MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_7 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.7.3 Mobile/15E148 Safari/604.1"
)

_auth_lock = threading.Lock()


def _decode_jwt_payload(token: str) -> dict:
    try:
        parts = token.split(".")
        if len(parts) < 2:
            return {}
        payload = parts[1]
        payload += "=" * (-len(payload) % 4)
        raw = base64.urlsafe_b64decode(payload.encode("utf-8"))
        return json.loads(raw.decode("utf-8"))
    except Exception:
        return {}


def _token_expired(token: str, skew_seconds: int = 60) -> bool:
    payload = _decode_jwt_payload(token)
    exp = payload.get("exp")
    if not isinstance(exp, int):
        return False
    return exp <= int(time.time()) + skew_seconds


def _read_token_from_auth_json() -> str:
    if not os.path.exists(COOKIE_FILE):
        return ""
    if os.path.getsize(COOKIE_FILE) == 0:
        return ""
    try:
        with open(COOKIE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        for c in data.get("cookies", []):
            if c.get("domain") == "hdhive.com" and c.get("name") == "token":
                token = str(c.get("value") or "").strip()
                if token:
                    return token
    except json.JSONDecodeError as e:
        logger.warning("读取 auth.json token 失败: %s", e)
    except Exception as e:
        logger.warning("读取 auth.json token 失败: %s", e)
    return ""


def _save_token_to_auth_json(token: str, max_age: int = 7 * 24 * 3600) -> None:
    now = int(time.time())
    expires = now + max_age
    data = {"cookies": [], "origins": []}
    if os.path.exists(COOKIE_FILE):
        try:
            with open(COOKIE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            pass

    cookies = data.get("cookies", [])
    if not isinstance(cookies, list):
        cookies = []

    replaced = False
    for c in cookies:
        if c.get("domain") == "hdhive.com" and c.get("name") == "token":
            c["value"] = token
            c["path"] = "/"
            c["expires"] = float(expires)
            c["httpOnly"] = True
            c["secure"] = False
            c["sameSite"] = "Lax"
            replaced = True
            break

    if not replaced:
        cookies.append(
            {
                "name": "token",
                "value": token,
                "domain": "hdhive.com",
                "path": "/",
                "expires": float(expires),
                "httpOnly": True,
                "secure": False,
                "sameSite": "Lax",
            }
        )

    data["cookies"] = cookies
    with open(COOKIE_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)


def _extract_session_token(session: requests.Session) -> str:
    for c in session.cookies:
        if c.domain == "hdhive.com" and c.name == "token":
            return c.value
    return ""


def _login_via_action(session: requests.Session) -> str:
    if not HDHIVE_USER or not HDHIVE_PASS:
        raise RuntimeError("缺少 HDHIVE_USER/HDHIVE_PASS，无法自动登录")

    headers = {
        "accept": "text/x-component",
        "content-type": "text/plain;charset=UTF-8",
        "origin": HDHIVE_BASE_URL,
        "referer": f"{HDHIVE_BASE_URL}/login?redirect=/",
        "next-action": HDHIVE_ACTION_LOGIN,
        "next-router-state-tree": LOGIN_STATE_TREE,
        "user-agent": MOBILE_UA,
    }
    payload = [{"username": HDHIVE_USER, "password": HDHIVE_PASS}, "/"]
    retries = max(1, HDHIVE_LOGIN_RETRIES)
    timeout = max(10, HDHIVE_LOGIN_TIMEOUT)
    last_error: Exception | None = None
    r = None
    for attempt in range(1, retries + 1):
        try:
            r = session.post(
                f"{HDHIVE_BASE_URL}/login?redirect=/",
                headers=headers,
                data=json.dumps(payload),
                timeout=timeout,
                allow_redirects=False,
            )
            if r.status_code not in (200, 303):
                raise RuntimeError(f"登录失败: http={r.status_code}")
            break
        except Exception as e:
            last_error = e
            if attempt < retries:
                logger.warning("登录接口失败，准备重试 %s/%s: %s", attempt, retries, e)
                time.sleep(min(2 * attempt, 5))
            else:
                raise RuntimeError(f"登录接口失败，已重试{retries}次: {e}")

    token = _extract_session_token(session)
    if not token:
        # fallback: requests 可能未解析 cookie，手动从响应头取
        set_cookie = (r.headers.get("set-cookie", "") if r is not None else "")
        marker = "token="
        idx = set_cookie.find(marker)
        if idx >= 0:
            token = set_cookie[idx + len(marker):].split(";", 1)[0].strip()
            if token:
                session.cookies.set("token", token, domain="hdhive.com")

    if not token:
        raise RuntimeError("登录响应未返回 token")

    _save_token_to_auth_json(token)
    payload = _decode_jwt_payload(token)
    exp = payload.get("exp")
    exp_text = (
        datetime.fromtimestamp(exp, tz=timezone.utc).isoformat() if isinstance(exp, int) else "unknown"
    )
    logger.info("✅ HDHive接口登录成功，token已更新，exp_utc=%s", exp_text)
    return token


def get_valid_token(force_refresh: bool = False) -> str:
    with _auth_lock:
        token = ""
        if not force_refresh:
            token = (HDHIVE_TOKEN or "").strip() or _read_token_from_auth_json()
            if token and not _token_expired(token):
                return token
        session = requests.Session()
        session.headers.update({"user-agent": MOBILE_UA})
        if token:
            session.cookies.set("token", token, domain="hdhive.com")
        return _login_via_action(session)


def build_authenticated_session(force_refresh: bool = False) -> requests.Session:
    token = get_valid_token(force_refresh=force_refresh)
    s = requests.Session()
    s.headers.update({"user-agent": MOBILE_UA})
    s.cookies.set("token", token, domain="hdhive.com")
    return s
