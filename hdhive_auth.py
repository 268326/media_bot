"""
HDHive Open API 认证与请求辅助模块
"""
from __future__ import annotations

import logging
from typing import Any

import requests

from config import HDHIVE_API_KEY, HDHIVE_OPEN_API_BASE_URL

logger = logging.getLogger(__name__)

OPEN_API_TIMEOUT = 30
OPEN_API_MAX_RETRIES = 3
OPEN_API_RETRY_BACKOFF_SECONDS = 1.5
OPEN_API_USER_AGENT = "MediaBot/1.0 (+https://hdhive.com)"


class OpenAPIError(RuntimeError):
    """HDHive Open API 请求错误。"""

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        description: str | None = None,
    ) -> None:
        self.status_code = status_code
        self.code = str(code or status_code)
        self.message = str(message or "")
        self.description = str(description or "")
        super().__init__(self.description or self.message or self.code)


def build_authenticated_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "X-API-Key": HDHIVE_API_KEY,
            "Accept": "application/json",
            "User-Agent": OPEN_API_USER_AGENT,
        }
    )
    return session


def _should_retry_http(status_code: int) -> bool:
    return status_code == 429 or 500 <= status_code < 600


def _parse_json_response(resp: requests.Response) -> dict[str, Any]:
    try:
        return resp.json()
    except ValueError as exc:
        raise OpenAPIError(
            resp.status_code,
            str(resp.status_code),
            f"接口返回非 JSON: {resp.text[:200]}",
        ) from exc


def request_open_api_json(
    session: requests.Session,
    method: str,
    path: str,
    **kwargs: Any,
) -> dict[str, Any]:
    kwargs.setdefault("timeout", OPEN_API_TIMEOUT)
    url = f"{HDHIVE_OPEN_API_BASE_URL}{path}"

    last_error: Exception | None = None
    for attempt in range(1, OPEN_API_MAX_RETRIES + 1):
        try:
            resp = session.request(method.upper(), url, **kwargs)
            payload = _parse_json_response(resp)

            if resp.ok and payload.get("success", True):
                return payload

            error = OpenAPIError(
                resp.status_code,
                str(payload.get("code") or resp.status_code),
                str(payload.get("message") or f"HTTP {resp.status_code}"),
                str(payload.get("description") or ""),
            )
            if attempt < OPEN_API_MAX_RETRIES and _should_retry_http(resp.status_code):
                wait_seconds = OPEN_API_RETRY_BACKOFF_SECONDS ** (attempt - 1)
                logger.warning(
                    "HDHive Open API 请求失败，%.1fs 后重试（%s/%s）: %s %s -> %s",
                    wait_seconds,
                    attempt,
                    OPEN_API_MAX_RETRIES,
                    method.upper(),
                    path,
                    error,
                )
                import time
                time.sleep(wait_seconds)
                last_error = error
                continue
            raise error
        except requests.RequestException as exc:
            last_error = exc
            if attempt < OPEN_API_MAX_RETRIES:
                wait_seconds = OPEN_API_RETRY_BACKOFF_SECONDS ** (attempt - 1)
                logger.warning(
                    "HDHive Open API 网络异常，%.1fs 后重试（%s/%s）: %s %s -> %s",
                    wait_seconds,
                    attempt,
                    OPEN_API_MAX_RETRIES,
                    method.upper(),
                    path,
                    exc,
                )
                import time
                time.sleep(wait_seconds)
                continue
            raise OpenAPIError(0, "network_error", "网络请求失败", str(exc)) from exc

    if last_error:
        raise last_error
    raise OpenAPIError(0, "unknown_error", "未知请求错误")

