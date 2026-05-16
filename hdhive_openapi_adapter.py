"""
HDHive 官方 Python SDK 适配层。

约束：
- 仅按官方 SDK 与官方 OpenAPI 文档实现。
- 固定官方站点 https://hdhive.com ，业务接口统一走 /api/open。
- 不再兼容旧版自定义地址、旧版会话模式、旧版环境变量别名。
"""
from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from typing import Any, Iterator

from config import HDHIVE_ACCESS_TOKEN, HDHIVE_API_KEY
from hdhive_openapi import HDHiveClient

logger = logging.getLogger(__name__)

OPEN_API_TIMEOUT = 30
OPEN_API_MAX_RETRIES = 3
OPEN_API_RETRY_BACKOFF_SECONDS = 1.5
OPEN_API_USER_AGENT = "MediaBot/1.0 (+https://hdhive.com)"
HDHIVE_OFFICIAL_BASE_URL = "https://hdhive.com"

_RATE_LIMIT_ERROR_CODES = {
    "RATE_LIMIT_EXCEEDED",
    "OPENAPI_COOLDOWN",
    "APP_RATE_LIMIT_EXCEEDED",
    "APP_BURST_LIMIT_EXCEEDED",
    "APP_IP_RATE_LIMIT_EXCEEDED",
    "ENDPOINT_GROUP_RATE_LIMIT_EXCEEDED",
    "USER_RATE_LIMIT_EXCEEDED",
    "APP_DAILY_QUOTA_EXCEEDED",
    "USER_DAILY_QUOTA_EXCEEDED",
}


class OpenAPIError(RuntimeError):
    """HDHive Open API 请求错误。"""

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        description: str | None = None,
        *,
        retry_after_seconds: int | None = None,
        limit_scope: str | None = None,
        limit_scope_label: str | None = None,
    ) -> None:
        self.status_code = int(status_code or 0)
        self.status = self.status_code
        self.code = str(code or status_code or "unknown_error")
        self.message = str(message or "")
        self.description = str(description or "")
        self.retry_after_seconds = retry_after_seconds
        self.limit_scope = str(limit_scope or "")
        self.limit_scope_label = str(limit_scope_label or "")
        super().__init__(self.description or self.message or self.code)


def _normalize_open_api_path(path: str) -> str:
    text = str(path or "").strip()
    if not text:
        raise ValueError("path is required")
    if not text.startswith("/"):
        text = f"/{text}"
    if text.startswith("/api/"):
        return text
    return f"/api/open{text}"


def _safe_int(value: Any) -> int | None:
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _extract_retry_after(headers: Any, payload: dict[str, Any] | None) -> int | None:
    if headers is not None:
        header_value = None
        if hasattr(headers, "get"):
            header_value = headers.get("Retry-After") or headers.get("retry-after")
        retry_after = _safe_int(header_value)
        if retry_after is not None:
            return retry_after
    if isinstance(payload, dict):
        retry_after = _safe_int(payload.get("retry_after_seconds"))
        if retry_after is not None:
            return retry_after
    return None


def _error_from_payload(
    *,
    status_code: int,
    payload: dict[str, Any] | None,
    headers: Any = None,
    fallback_message: str = "",
    fallback_description: str = "",
) -> OpenAPIError:
    data = payload if isinstance(payload, dict) else {}
    return OpenAPIError(
        status_code=status_code,
        code=str(data.get("code") or status_code or "unknown_error"),
        message=str(data.get("message") or fallback_message or f"HTTP {status_code}"),
        description=str(data.get("description") or fallback_description or ""),
        retry_after_seconds=_extract_retry_after(headers, data),
        limit_scope=str(data.get("limit_scope") or ""),
        limit_scope_label=str(data.get("limit_scope_label") or ""),
    )


def _should_retry_http(error: OpenAPIError) -> bool:
    if error.status_code == 429:
        return True
    if 500 <= error.status_code < 600:
        return True
    if error.code in _RATE_LIMIT_ERROR_CODES:
        return True
    return False


def _compute_retry_wait_seconds(error: OpenAPIError, attempt: int) -> float:
    retry_after = error.retry_after_seconds
    if retry_after is not None and retry_after > 0:
        return float(retry_after)
    return float(OPEN_API_RETRY_BACKOFF_SECONDS ** (attempt - 1))


class MediaBotHDHiveClient(HDHiveClient):
    """基于官方 SDK 的生产级客户端扩展。"""

    def with_access_token(self, token: str | None) -> "MediaBotHDHiveClient":
        self.access_token = str(token or "").strip() or None
        return self

    def get_me(self) -> dict[str, Any]:
        return self._request("GET", "/api/open/me")

    def checkin(self, *, is_gambler: bool = False) -> dict[str, Any]:
        return self._request("POST", "/api/open/checkin", {"is_gambler": bool(is_gambler)})

    def get_share_detail(self, slug: str) -> dict[str, Any]:
        path = "/api/open/shares/{}".format(urllib.parse.quote(str(slug or "").strip(), safe=""))
        return self._request("GET", path)

    def check_resource(self, url: str) -> dict[str, Any]:
        return self._request("POST", "/api/open/check/resource", {"url": str(url or "")})

    def get_usage(self, *, start_date: str | None = None, end_date: str | None = None) -> dict[str, Any]:
        params: list[tuple[str, str]] = []
        if start_date:
            params.append(("start_date", str(start_date)))
        if end_date:
            params.append(("end_date", str(end_date)))
        path = "/api/open/usage"
        if params:
            path = f"{path}?{urllib.parse.urlencode(params)}"
        return self._request("GET", path)

    def _request(self, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        if not self.base_url:
            raise ValueError("base_url is required")
        if not self.api_key:
            raise ValueError("api_key is required")

        normalized_path = _normalize_open_api_path(path)
        url = self.base_url.rstrip("/") + normalized_path
        payload = None
        headers = {
            "X-API-Key": self.api_key,
            "Accept": "application/json",
            "User-Agent": OPEN_API_USER_AGENT,
        }
        if self.access_token:
            headers["Authorization"] = f"Bearer {self.access_token}"
        if body is not None:
            payload = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"

        last_error: Exception | None = None
        for attempt in range(1, OPEN_API_MAX_RETRIES + 1):
            request = urllib.request.Request(url, data=payload, headers=headers, method=method.upper())
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    raw = response.read().decode("utf-8", errors="replace")
                    try:
                        data = json.loads(raw)
                    except json.JSONDecodeError as exc:
                        raise OpenAPIError(
                            response.getcode(),
                            str(response.getcode()),
                            "接口返回非 JSON",
                            raw[:200],
                        ) from exc

                    if data.get("success", True):
                        return data

                    error = _error_from_payload(
                        status_code=response.getcode(),
                        payload=data,
                        headers=response.headers,
                        fallback_message=f"HTTP {response.getcode()}",
                    )
            except urllib.error.HTTPError as exc:
                raw = exc.read().decode("utf-8", errors="replace")
                payload_data: dict[str, Any] | None = None
                try:
                    parsed = json.loads(raw)
                    if isinstance(parsed, dict):
                        payload_data = parsed
                except json.JSONDecodeError:
                    payload_data = None
                error = _error_from_payload(
                    status_code=exc.code,
                    payload=payload_data,
                    headers=exc.headers,
                    fallback_message=str(exc.reason or f"HTTP {exc.code}"),
                    fallback_description="" if payload_data is not None else raw[:300],
                )
            except OpenAPIError:
                raise
            except urllib.error.URLError as exc:
                last_error = OpenAPIError(0, "network_error", "网络请求失败", str(exc.reason or exc))
                if attempt < OPEN_API_MAX_RETRIES:
                    wait_seconds = float(OPEN_API_RETRY_BACKOFF_SECONDS ** (attempt - 1))
                    logger.warning(
                        "HDHive Open API 网络异常，%.1fs 后重试（%s/%s）: %s %s -> %s",
                        wait_seconds,
                        attempt,
                        OPEN_API_MAX_RETRIES,
                        method.upper(),
                        normalized_path,
                        last_error,
                    )
                    time.sleep(wait_seconds)
                    continue
                raise last_error from exc
            except Exception as exc:
                raise OpenAPIError(0, "unknown_error", "未知请求错误", str(exc)) from exc

            last_error = error
            if attempt < OPEN_API_MAX_RETRIES and _should_retry_http(error):
                wait_seconds = _compute_retry_wait_seconds(error, attempt)
                logger.warning(
                    "HDHive Open API 请求失败，%.1fs 后重试（%s/%s）: %s %s -> %s [code=%s limit_scope=%s]",
                    wait_seconds,
                    attempt,
                    OPEN_API_MAX_RETRIES,
                    method.upper(),
                    normalized_path,
                    error,
                    error.code,
                    error.limit_scope or "-",
                )
                time.sleep(wait_seconds)
                continue
            raise error

        if last_error:
            raise last_error
        raise OpenAPIError(0, "unknown_error", "未知请求错误")


def build_authenticated_client(access_token: str | None = None) -> MediaBotHDHiveClient:
    token = HDHIVE_ACCESS_TOKEN if access_token is None else access_token
    client = MediaBotHDHiveClient(
        base_url=HDHIVE_OFFICIAL_BASE_URL,
        api_key=HDHIVE_API_KEY,
        timeout=OPEN_API_TIMEOUT,
    )
    if token:
        client.with_access_token(token)
    return client


@contextmanager
def build_authenticated_client_context(access_token: str | None = None) -> Iterator[MediaBotHDHiveClient]:
    yield build_authenticated_client(access_token=access_token)
