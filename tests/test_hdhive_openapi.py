import asyncio
import unittest
from unittest.mock import patch

from checkin_service import _daily_check_in_sync
from hdhive_openapi_adapter import OpenAPIError, _normalize_open_api_path, _error_from_payload
from hdhive_openapi_api import _normalize_resource, _extract_user_points, fetch_download_link


class DummyClient:
    def __init__(self, responses):
        self._responses = list(responses)
        self.checkin_calls = []
        self.share_detail_calls = []

    def get_me(self):
        if not self._responses:
            raise AssertionError("no response left")
        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def checkin(self, *, is_gambler=False):
        self.checkin_calls.append(bool(is_gambler))
        if not self._responses:
            raise AssertionError("no response left")
        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def get_share_detail(self, slug):
        self.share_detail_calls.append(str(slug))
        if not self._responses:
            raise AssertionError("no response left")
        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class DummySession:
    def __init__(self, client):
        self.client = client

    def __enter__(self):
        return self.client

    def __exit__(self, exc_type, exc, tb):
        return False


class HDHiveOfficialOpenAPITests(unittest.TestCase):
    def test_official_sdk_adapter_normalizes_paths(self):
        self.assertEqual(_normalize_open_api_path("/resources/movie/550"), "/api/open/resources/movie/550")
        self.assertEqual(_normalize_open_api_path("/api/open/resources/movie/550"), "/api/open/resources/movie/550")

    def test_normalize_resource_preserves_pan_type_and_tags(self):
        resource = {
            "slug": "abc123",
            "title": "Test Movie",
            "pan_type": "115",
            "share_size": "12GB",
            "video_resolution": ["4K"],
            "source": ["WEB-DL"],
            "subtitle_language": ["简中"],
            "subtitle_type": ["外挂字幕"],
            "unlock_points": 10,
            "user": {"nickname": "Uploader"},
        }
        normalized = _normalize_resource(resource)
        self.assertEqual(normalized["id"], "abc123")
        self.assertEqual(normalized["title"], "Test Movie")
        self.assertEqual(normalized["uploader"], "Uploader")
        self.assertEqual(normalized["website"], "115")
        self.assertEqual(normalized["points"], "10积分")
        self.assertIn("4K", normalized["tags"])
        self.assertIn("WEB-DL", normalized["tags"])
        self.assertIn("12GB", normalized["tags"])

    def test_extract_user_points_from_me_payload(self):
        payload = {
            "success": True,
            "data": {
                "user_meta": {
                    "points": "321",
                }
            },
        }
        self.assertEqual(_extract_user_points(payload), 321)

    def test_fetch_download_link_requires_unlock_when_documented_fields_say_locked(self):
        share_payload = {
            "success": True,
            "data": {
                "slug": "abc123",
                "pan_type": "115",
                "unlock_points": 8,
                "is_unlocked": False,
            },
        }
        client = DummyClient([share_payload])
        with patch("hdhive_openapi_api.build_authenticated_client_context", return_value=DummySession(client)):
            result = asyncio.run(fetch_download_link("abc123", user_id=1))
        self.assertEqual(result["need_unlock"], True)
        self.assertEqual(result["points"], 8)
        self.assertEqual(result["website"], "115")

    def test_fetch_download_link_returns_unlocked_when_documented_fields_say_free(self):
        share_payload = {
            "success": True,
            "data": {
                "slug": "abc123",
                "pan_type": "115",
                "unlock_points": 0,
                "is_unlocked": False,
            },
        }
        client = DummyClient([share_payload])
        with patch("hdhive_openapi_api.build_authenticated_client_context", return_value=DummySession(client)):
            result = asyncio.run(fetch_download_link("abc123", user_id=1))
        self.assertEqual(result["need_unlock"], False)
        self.assertEqual(result["already_unlocked"], False)

    def test_fetch_download_link_404_returns_none(self):
        client = DummyClient([OpenAPIError(404, "404", "not found")])
        with patch("hdhive_openapi_api.build_authenticated_client_context", return_value=DummySession(client)):
            result = asyncio.run(fetch_download_link("missing", user_id=1))
        self.assertIsNone(result)

    def test_fetch_download_link_403_bubbles_up(self):
        client = DummyClient([OpenAPIError(403, "OPENAPI_USER_REQUIRED", "user required")])
        with patch("hdhive_openapi_api.build_authenticated_client_context", return_value=DummySession(client)):
            with self.assertRaises(OpenAPIError) as ctx:
                asyncio.run(fetch_download_link("abc123", user_id=1))
        self.assertEqual(ctx.exception.status_code, 403)
        self.assertEqual(ctx.exception.code, "OPENAPI_USER_REQUIRED")

    def test_fetch_download_link_402_bubbles_up(self):
        client = DummyClient([OpenAPIError(402, "INSUFFICIENT_POINTS", "points not enough")])
        with patch("hdhive_openapi_api.build_authenticated_client_context", return_value=DummySession(client)):
            with self.assertRaises(OpenAPIError) as ctx:
                asyncio.run(fetch_download_link("abc123", user_id=1))
        self.assertEqual(ctx.exception.status_code, 402)
        self.assertEqual(ctx.exception.code, "INSUFFICIENT_POINTS")

    def test_error_from_payload_keeps_429_retry_after(self):
        class Headers(dict):
            def get(self, key, default=None):
                return super().get(key, default)

        err = _error_from_payload(
            status_code=429,
            payload={
                "code": "RATE_LIMIT_EXCEEDED",
                "message": "too many requests",
                "description": "cooldown",
                "retry_after_seconds": 120,
                "limit_scope": "user",
                "limit_scope_label": "授权用户",
            },
            headers=Headers({"Retry-After": "120"}),
        )
        self.assertEqual(err.status_code, 429)
        self.assertEqual(err.code, "RATE_LIMIT_EXCEEDED")
        self.assertEqual(err.retry_after_seconds, 120)
        self.assertEqual(err.limit_scope, "user")
        self.assertEqual(err.limit_scope_label, "授权用户")

    def test_daily_checkin_marks_already_checked_in_when_checked_in_false(self):
        before_payload = {"data": {"user_meta": {"points": 100}}}
        checkin_payload = {
            "success": True,
            "message": "今日已签到",
            "data": {
                "checked_in": False,
                "message": "今日已签到",
            },
        }
        after_payload = {"data": {"user_meta": {"points": 100}}}
        client = DummyClient([before_payload, checkin_payload, after_payload])

        with patch("checkin_service.build_authenticated_client_context", return_value=DummySession(client)):
            result = _daily_check_in_sync()

        self.assertTrue(result["success"])
        self.assertTrue(result["already_checked_in"])
        self.assertEqual(result["before_points"], 100)
        self.assertEqual(result["after_points"], 100)
        self.assertEqual(result["message"], "今日已签到")

    def test_daily_checkin_tolerates_points_read_failure(self):
        before_error = OpenAPIError(403, "VIP_REQUIRED", "vip required")
        checkin_payload = {
            "success": True,
            "message": "签到成功",
            "data": {
                "checked_in": True,
                "message": "签到成功",
            },
        }
        after_payload = {"data": {"user_meta": {"points": 888}}}
        client = DummyClient([before_error, checkin_payload, after_payload])

        with patch("checkin_service.build_authenticated_client_context", return_value=DummySession(client)):
            result = _daily_check_in_sync()

        self.assertTrue(result["success"])
        self.assertFalse(result["already_checked_in"])
        self.assertIsNone(result["before_points"])
        self.assertEqual(result["after_points"], 888)


if __name__ == "__main__":
    unittest.main()
