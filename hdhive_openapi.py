from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Optional


class HDHiveOpenAPIError(Exception):
    def __init__(self, code: str, message: str, description: str = "") -> None:
        super().__init__(description or message or code)
        self.code = code
        self.message = message
        self.description = description


@dataclass
class HDHiveClient:
    base_url: str
    api_key: str
    access_token: Optional[str] = None
    timeout: int = 30

    def with_access_token(self, token: str) -> "HDHiveClient":
        self.access_token = token
        return self

    def ping(self) -> dict[str, Any]:
        return self._request("GET", "/api/open/ping")

    def get_quota(self) -> dict[str, Any]:
        return self._request("GET", "/api/open/quota")

    def get_usage_today(self) -> dict[str, Any]:
        return self._request("GET", "/api/open/usage/today")

    def query_resources(self, media_type: str, tmdb_id: str) -> dict[str, Any]:
        path = "/api/open/resources/{}/{}".format(
            urllib.parse.quote(media_type, safe=""),
            urllib.parse.quote(tmdb_id, safe=""),
        )
        return self._request("GET", path)

    def unlock_resource(self, slug: str) -> dict[str, Any]:
        return self._request("POST", "/api/open/resources/unlock", {"slug": slug})

    def _request(self, method: str, path: str, body: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        if not self.base_url:
            raise ValueError("base_url is required")
        if not self.api_key:
            raise ValueError("api_key is required")

        url = self.base_url.rstrip("/") + path
        payload = None
        headers = {
            "X-API-Key": self.api_key,
            "Accept": "application/json",
        }
        if self.access_token:
            headers["Authorization"] = f"Bearer {self.access_token}"
        if body is not None:
            payload = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"

        request = urllib.request.Request(url, data=payload, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8")
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                raise HDHiveOpenAPIError(str(exc.code), exc.reason, raw) from exc
            raise HDHiveOpenAPIError(
                str(data.get("code", exc.code)),
                str(data.get("message", exc.reason)),
                str(data.get("description", "")),
            ) from exc
