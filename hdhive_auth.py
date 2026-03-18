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
    resp = session.request(method.upper(), url, **kwargs)
    payload = _parse_json_response(resp)

    if resp.ok and payload.get("success", True):
        return payload

    raise OpenAPIError(
        resp.status_code,
        str(payload.get("code") or resp.status_code),
        str(payload.get("message") or f"HTTP {resp.status_code}"),
        str(payload.get("description") or ""),
    )

