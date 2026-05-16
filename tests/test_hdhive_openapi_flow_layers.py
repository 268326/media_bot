import unittest
from unittest.mock import AsyncMock, patch

from hdhive_openapi_flow import HDHiveOpenAPIFlowService
from hdhive_openapi_flow_search import HDHiveOpenAPISearchFlow
from hdhive_openapi_flow_symedia import HDHiveOpenAPISymediaFlow
from hdhive_openapi_flow_unlock import HDHiveOpenAPIUnlockFlow
from hdhive_openapi_state import HDHiveOpenAPIState
from hdhive_openapi_unlock_service import UnlockQueueNotice


class DummyUser:
    def __init__(self, user_id: int = 1):
        self.id = user_id


class DummyChat:
    def __init__(self, chat_id: int = 100):
        self.id = chat_id


class DummyEntity:
    def __init__(self, entity_type: str, offset: int = 0, length: int = 0, url: str | None = None):
        self.type = entity_type
        self.offset = offset
        self.length = length
        self.url = url


class DummyTask:
    def __init__(self):
        self.cancel_called = False

    def cancel(self):
        self.cancel_called = True


class DummyMessage:
    def __init__(self, text=None, *, caption=None, message_id=1, chat_id=100, user_id=1):
        self.text = text
        self.caption = caption
        self.message_id = message_id
        self.chat = DummyChat(chat_id)
        self.from_user = DummyUser(user_id)
        self.entities = []
        self.caption_entities = []
        self.edits = []
        self.replies = []
        self.photos = []
        self.reply_markup = None
        self.deleted = False

    async def reply(self, text, **kwargs):
        child = DummyMessage(text=text, message_id=self.message_id + len(self.replies) + 1, chat_id=self.chat.id, user_id=self.from_user.id)
        child.reply_kwargs = kwargs
        self.replies.append((text, kwargs, child))
        return child

    async def answer(self, text, **kwargs):
        return await self.reply(text, **kwargs)

    async def reply_photo(self, photo, caption=None, **kwargs):
        child = DummyMessage(text=caption, message_id=self.message_id + len(self.replies) + 1, chat_id=self.chat.id, user_id=self.from_user.id)
        child.photo = photo
        child.reply_kwargs = kwargs
        self.photos.append((photo, caption, kwargs, child))
        return child

    async def answer_photo(self, photo, caption=None, **kwargs):
        return await self.reply_photo(photo, caption=caption, **kwargs)

    async def edit_text(self, text, **kwargs):
        self.text = text
        self.edits.append((text, kwargs))
        return self

    async def edit_reply_markup(self, reply_markup=None):
        self.reply_markup = reply_markup
        return self

    async def delete(self):
        self.deleted = True


class DummyCallback:
    def __init__(self, data: str, message: DummyMessage | None = None, user_id: int = 1):
        self.data = data
        self.message = message or DummyMessage()
        self.from_user = DummyUser(user_id)
        self.answers = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))


class HDHiveOpenAPIStateTests(unittest.TestCase):
    def test_make_message_state_key(self):
        state = HDHiveOpenAPIState()
        msg = DummyMessage(message_id=42, chat_id=777)
        self.assertEqual(state.make_message_state_key(msg), "777:42")

    def test_trim_dict_cache_keeps_latest_items(self):
        state = HDHiveOpenAPIState()
        cache = {"a": 1, "b": 2, "c": 3}
        state.trim_dict_cache(cache, 2)
        self.assertEqual(list(cache.keys()), ["b", "c"])

    def test_save_tmdb_search_state_trims(self):
        state = HDHiveOpenAPIState()
        for idx in range(260):
            msg = DummyMessage(message_id=idx + 1, chat_id=1)
            state.save_tmdb_search_state(msg, [{"title": f"x{idx}"}], page=0)
        self.assertLessEqual(len(state.tmdb_search_state), 256)

    def test_cache_resource_websites_trims(self):
        state = HDHiveOpenAPIState()
        resources = [{"id": str(i), "website": "115"} for i in range(2050)]
        state.cache_resource_websites(resources)
        self.assertLessEqual(len(state.resource_website_cache), 2048)


class HDHiveOpenAPIUnlockFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_handle_unlock_required_auto_unlock_path(self):
        flow = HDHiveOpenAPIUnlockFlow(HDHiveOpenAPIState())
        flow.auto_unlock_threshold = 10
        flow.perform_unlock_and_handle_result = AsyncMock(return_value=True)
        wait_msg = DummyMessage()
        ok = await flow.handle_unlock_required(
            wait_msg=wait_msg,
            fallback_msg=DummyMessage(),
            resource_id="abc",
            user_id=1,
            result={"points": 5},
            website="115",
        )
        self.assertTrue(ok)
        flow.perform_unlock_and_handle_result.assert_awaited_once()

    async def test_handle_unlock_required_insufficient_points(self):
        flow = HDHiveOpenAPIUnlockFlow(HDHiveOpenAPIState())
        wait_msg = DummyMessage()
        with patch("hdhive_openapi_flow_unlock.get_user_points", new=AsyncMock(return_value=3)):
            ok = await flow.handle_unlock_required(
                wait_msg=wait_msg,
                fallback_msg=DummyMessage(),
                resource_id="abc",
                user_id=1,
                result={"points": 8},
                website="115",
            )
        self.assertTrue(ok)
        self.assertTrue(wait_msg.edits)
        self.assertIn("积分不足", wait_msg.edits[-1][0])

    async def test_fetch_download_link_and_handle_result_success_calls_link_handler(self):
        flow = HDHiveOpenAPIUnlockFlow(HDHiveOpenAPIState())
        flow.link_extracted_handler = AsyncMock()
        with patch("hdhive_openapi_flow_unlock.unlock_resource", new=AsyncMock(return_value={"full_url": "https://115.com/s/abc", "access_code": "1234"})):
            ok = await flow.fetch_download_link_and_handle_result(
                wait_msg=DummyMessage(),
                fallback_msg=DummyMessage(),
                resource_id="abc",
                user_id=1,
                website="115",
            )
        self.assertTrue(ok)
        flow.link_extracted_handler.assert_awaited_once()

    async def test_fetch_download_link_and_handle_result_missing_link(self):
        flow = HDHiveOpenAPIUnlockFlow(HDHiveOpenAPIState())
        flow.link_extracted_handler = AsyncMock()
        wait_msg = DummyMessage()
        with patch("hdhive_openapi_flow_unlock.unlock_resource", new=AsyncMock(return_value={})):
            ok = await flow.fetch_download_link_and_handle_result(
                wait_msg=wait_msg,
                fallback_msg=DummyMessage(),
                resource_id="abc",
                user_id=1,
                website="115",
            )
        self.assertFalse(ok)
        self.assertIn("提取失败", wait_msg.edits[-1][0])

    async def test_update_unlock_queue_notice_formats_message(self):
        flow = HDHiveOpenAPIUnlockFlow(HDHiveOpenAPIState())
        wait_msg = DummyMessage()
        notice = UnlockQueueNotice(
            resource_id="abc",
            queue_position=2,
            ahead_count=1,
            wait_seconds=120,
            queued_seconds=30,
            user_id=1,
        )
        await flow.update_unlock_queue_notice(wait_msg, notice, auto_unlock=False)
        self.assertIn("官方 429/Retry-After", wait_msg.edits[-1][0])


class HDHiveOpenAPISymediaFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_handle_link_extracted_non_115_message(self):
        flow = HDHiveOpenAPISymediaFlow(HDHiveOpenAPIState())
        wait_msg = DummyMessage()
        await flow.handle_link_extracted(wait_msg, "https://pan.baidu.com/s/abc", code="no", website="baidu")
        self.assertTrue(wait_msg.edits)
        self.assertIn("百度网盘", wait_msg.edits[-1][0])

    async def test_handle_link_extracted_115_starts_pending_task(self):
        flow = HDHiveOpenAPISymediaFlow(HDHiveOpenAPIState())
        wait_msg = DummyMessage(message_id=9, chat_id=7)

        def fake_create_task(coro):
            coro.close()
            return DummyTask()

        with patch("hdhive_openapi_flow_symedia.SA_URL", "https://symedia"), \
             patch("hdhive_openapi_flow_symedia.SA_PARENT_ID", "123"), \
             patch("hdhive_openapi_flow_symedia.SA_ENABLE_115_PUSH", True), \
             patch("hdhive_openapi_flow_symedia.asyncio.create_task", side_effect=fake_create_task):
            await flow.handle_link_extracted(wait_msg, "https://115.com/s/abc", code="1234", website="115", requester_user_id=1)

        self.assertEqual(len(flow.state.pending_sa_tasks), 1)
        pending = next(iter(flow.state.pending_sa_tasks.values()))
        self.assertEqual(pending["link"], "https://115.com/s/abc")

    async def test_cancel_latest_sa_task_picks_latest(self):
        flow = HDHiveOpenAPISymediaFlow(HDHiveOpenAPIState())
        older = DummyTask()
        newer = DummyTask()
        flow.state.pending_sa_tasks["1:1"] = {"link": "a", "task": older, "cancelled": False, "user_id": 1, "created_at": 1}
        flow.state.pending_sa_tasks["1:2"] = {"link": "b", "task": newer, "cancelled": False, "user_id": 1, "created_at": 2}
        msg = DummyMessage(user_id=1)
        await flow.cancel_latest_sa_task(msg)
        self.assertTrue(newer.cancel_called)
        self.assertFalse(older.cancel_called)
        self.assertIn("已取消最近一次自动添加任务", msg.replies[-1][0])

    async def test_handle_send_to_sa_callback_rejects_non_115(self):
        flow = HDHiveOpenAPISymediaFlow(HDHiveOpenAPIState())
        callback = DummyCallback("send_to_group:https://pan.baidu.com/s/abc")
        await flow.handle_send_to_sa_callback(callback)
        self.assertEqual(callback.answers[-1], ("❌ 仅支持115链接添加到Symedia", True))


class HDHiveOpenAPISearchFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_handle_search_input_routes_resource_tmdb_keyword(self):
        flow = HDHiveOpenAPISearchFlow(HDHiveOpenAPIState())
        flow.handle_resource_link = AsyncMock()
        flow.handle_tmdb_link = AsyncMock()
        flow.handle_keyword_search = AsyncMock()

        with patch("hdhive_openapi_flow_search.parse_hdhive_link", return_value={"type": "resource", "id": "abc", "resource_url": "u", "media_type": None}):
            await flow.handle_search_input(DummyMessage(text="x"), "x", "tv")
        flow.handle_resource_link.assert_awaited_once()

        with patch("hdhive_openapi_flow_search.parse_hdhive_link", return_value={"type": "tmdb", "id": "1", "resource_url": None, "media_type": "movie"}):
            await flow.handle_search_input(DummyMessage(text="x"), "x", "tv")
        flow.handle_tmdb_link.assert_awaited_once()

        with patch("hdhive_openapi_flow_search.parse_hdhive_link", return_value={"type": "none", "id": None, "resource_url": None, "media_type": None}):
            await flow.handle_search_input(DummyMessage(text="x"), "x", "tv")
        flow.handle_keyword_search.assert_awaited_once()

    async def test_handle_provider_filter_callback_expired(self):
        flow = HDHiveOpenAPISearchFlow(HDHiveOpenAPIState())
        callback = DummyCallback("pf:115", message=DummyMessage())
        await flow.handle_provider_filter_callback(callback)
        self.assertEqual(callback.answers[-1], ("列表已过期，请重新搜索", True))

    async def test_handle_provider_filter_callback_updates_message(self):
        state = HDHiveOpenAPIState()
        flow = HDHiveOpenAPISearchFlow(state)
        msg = DummyMessage(message_id=1, chat_id=1)
        state.resource_list_state["1:1"] = {
            "resources": [{"id": "a", "title": "Movie", "uploader": "u", "points": "免费", "website": "115", "tags": []}],
            "media_type": "movie",
            "title": None,
        }
        callback = DummyCallback("pf:115", message=msg)
        await flow.handle_provider_filter_callback(callback)
        self.assertTrue(msg.edits)

    async def test_handle_tmdb_page_callback_changes_page(self):
        state = HDHiveOpenAPIState()
        flow = HDHiveOpenAPISearchFlow(state)
        msg = DummyMessage(message_id=1, chat_id=1)
        state.tmdb_search_state["1:1"] = {"results": [{"title": "A"}, {"title": "B"}, {"title": "C"}, {"title": "D"}, {"title": "E"}, {"title": "F"}], "page": 0}
        callback = DummyCallback("tmdb_page:1", message=msg)
        await flow.handle_tmdb_page_callback(callback)
        self.assertEqual(state.tmdb_search_state["1:1"]["page"], 1)
        self.assertTrue(msg.edits)

    async def test_handle_direct_link_message_detects_resource(self):
        state = HDHiveOpenAPIState()
        flow = HDHiveOpenAPISearchFlow(state)
        flow.handle_resource_link = AsyncMock()
        text = "https://hdhive.com/resource/115/abcdef12"
        msg = DummyMessage(text=text)
        msg.entities = [DummyEntity("url", 0, len(text))]
        await flow.handle_direct_link_message(msg)
        flow.handle_resource_link.assert_awaited_once()

    async def test_handle_resource_callback_routes_to_unlock_handler(self):
        state = HDHiveOpenAPIState()
        flow = HDHiveOpenAPISearchFlow(state)
        flow.unlock_required_handler = AsyncMock(return_value=True)
        flow.fetch_result_handler = AsyncMock(return_value=False)
        callback = DummyCallback("movie_1:abc", message=DummyMessage())
        with patch("hdhive_openapi_flow_search.fetch_download_link", new=AsyncMock(return_value={"need_unlock": True, "points": 8, "website": "115"})):
            await flow.handle_resource_callback(callback)
        flow.unlock_required_handler.assert_awaited_once()

    async def test_handle_select_tmdb_callback_saves_resource_state(self):
        state = HDHiveOpenAPIState()
        flow = HDHiveOpenAPISearchFlow(state)
        msg = DummyMessage(message_id=1, chat_id=1)
        state.tmdb_search_state["1:1"] = {
            "results": [{"tmdb_id": 1, "media_type": "movie", "title": "Demo", "overview": "desc"}],
            "page": 0,
        }
        callback = DummyCallback("select_tmdb:0", message=msg)
        with patch("hdhive_openapi_flow_search.get_tmdb_details", new=AsyncMock(return_value={"title": "Demo", "media_type": "movie", "overview": "desc"})), \
             patch("hdhive_openapi_flow_search.get_resources_by_tmdb_id", new=AsyncMock(return_value=[{"id": "abc", "title": "Res", "uploader": "u", "points": "免费", "website": "115", "tags": []}])):
            await flow.handle_select_tmdb_callback(callback)
        self.assertTrue(state.resource_list_state)


class HDHiveOpenAPIAggregateFlowTests(unittest.TestCase):
    def test_aggregate_wires_subflows(self):
        service = HDHiveOpenAPIFlowService()
        self.assertIs(service.unlock.link_extracted_handler.__self__, service.symedia)
        self.assertIs(service.search.unlock_required_handler.__self__, service.unlock)
        self.assertIs(service.search.fetch_result_handler.__self__, service.unlock)


if __name__ == "__main__":
    unittest.main()
