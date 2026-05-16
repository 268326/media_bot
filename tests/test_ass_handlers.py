import asyncio
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import handlers
from ass_formatter import format_rescan_running, format_mux_running
from ass_mux_pipeline import MuxProgressEvent


class DummyUser:
    def __init__(self, user_id: int = 1, username: str = "tester"):
        self.id = user_id
        self.username = username
        self.is_bot = False


class DummyChat:
    def __init__(self, chat_id: int = 100):
        self.id = chat_id


class DummyBot:
    def __init__(self):
        self.edits = []

    async def edit_message_text(self, text=None, chat_id=None, message_id=None, reply_markup=None, parse_mode=None):
        self.edits.append(
            {
                "text": text,
                "chat_id": chat_id,
                "message_id": message_id,
                "reply_markup": reply_markup,
                "parse_mode": parse_mode,
            }
        )


class DummyMessage:
    def __init__(self, text=None, *, message_id: int = 1, chat_id: int = 100, user_id: int = 1, bot=None):
        self.text = text
        self.caption = None
        self.message_id = message_id
        self.chat = DummyChat(chat_id)
        self.from_user = DummyUser(user_id)
        self.bot = bot or DummyBot()
        self.reply_calls = []
        self.edit_calls = []
        self.reply_to_message = None

    async def reply(self, text, **kwargs):
        child = DummyMessage(
            text=text,
            message_id=self.message_id + len(self.reply_calls) + 1,
            chat_id=self.chat.id,
            user_id=self.from_user.id,
            bot=self.bot,
        )
        self.reply_calls.append((text, kwargs, child))
        return child

    async def answer(self, text, **kwargs):
        return await self.reply(text, **kwargs)

    async def edit_text(self, text, **kwargs):
        self.text = text
        self.edit_calls.append((text, kwargs))
        return self


class DummyCallback:
    def __init__(self, data: str, *, message: DummyMessage | None = None, user_id: int = 1, bot=None):
        self.data = data
        self.message = message or DummyMessage(bot=bot)
        self.from_user = DummyUser(user_id)
        self.bot = bot or self.message.bot
        self.answers = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))


class AssHandlerRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def test_cmd_ass_replies_with_menu(self):
        message = DummyMessage(text="/ass")
        keyboard = object()
        with patch("handlers.check_user_permission", new=AsyncMock(return_value=True)), \
             patch.object(handlers.ass_service, "build_mux_menu", new=AsyncMock(return_value=("menu text", keyboard))):
            await handlers.cmd_ass(message)

        self.assertEqual(len(message.reply_calls), 1)
        text, kwargs, _child = message.reply_calls[0]
        self.assertEqual(text, "menu text")
        self.assertIs(kwargs["reply_markup"], keyboard)
        self.assertEqual(kwargs["parse_mode"], "HTML")

    async def test_callback_ass_menu_mux_start_binds_panel_message(self):
        bot = DummyBot()
        message = DummyMessage(message_id=7, bot=bot)
        callback = DummyCallback(f"{handlers.ASS_MENU_PREFIX}mux_start", message=message, bot=bot)
        start_mux_session = AsyncMock()
        with patch("handlers.check_callback_permission", new=AsyncMock(return_value=True)), \
             patch.object(handlers.ass_service, "start_mux_session", new=start_mux_session), \
             patch.object(handlers.ass_service, "build_mux_panel_text", new=AsyncMock(return_value="panel text")), \
             patch.object(handlers.ass_service, "build_mux_plan_keyboard", new=Mock(return_value="panel kb")), \
             patch.object(handlers.ass_service, "bind_mux_message_ids", new=Mock()) as bind_mock, \
             patch("handlers.sync_ass_mux_view", new=AsyncMock()) as sync_mock:
            await handlers.callback_ass_menu(callback)

        start_mux_session.assert_awaited_once_with(chat_id=100, owner_user_id=1, mode="auto")
        self.assertEqual(message.edit_calls[-1][0], "panel text")
        bind_mock.assert_called_once_with(100, 1, panel_message_id=7)
        sync_mock.assert_awaited_once()

    async def test_callback_ass_mux_prompt_group_uses_current_service_api(self):
        message = DummyMessage(message_id=11)
        callback = DummyCallback(f"{handlers.ASS_MUX_PREFIX}prompt_group", message=message)
        session = SimpleNamespace(awaiting_message_id=99)
        with patch("handlers.check_callback_permission", new=AsyncMock(return_value=True)), \
             patch.object(handlers.ass_service, "ensure_mux_owner", new=Mock(return_value=True)), \
             patch.object(handlers.ass_service, "get_mux_session", new=Mock(return_value=session)), \
             patch.object(handlers.ass_service, "set_mux_prompt", new=Mock()) as prompt_mock, \
             patch.object(handlers.ass_service, "clear_mux_inline_notice", new=Mock()) as clear_mock, \
             patch("handlers.sync_ass_mux_view", new=AsyncMock()) as sync_mock:
            await handlers.callback_ass_mux(callback)

        prompt_mock.assert_called_once_with(100, 1, field="default_group", message_id=99)
        clear_mock.assert_called_once_with(100, 1)
        sync_mock.assert_awaited_once()

    async def test_callback_ass_mux_refresh_rebuilds_plan(self):
        message = DummyMessage(message_id=12)
        callback = DummyCallback(f"{handlers.ASS_MUX_PREFIX}refresh", message=message)
        with patch("handlers.check_callback_permission", new=AsyncMock(return_value=True)), \
             patch.object(handlers.ass_service, "ensure_mux_owner", new=Mock(return_value=True)), \
             patch.object(handlers.ass_service, "set_mux_inline_notice", new=Mock()) as notice_mock, \
             patch.object(handlers.ass_service, "rebuild_mux_plan", new=AsyncMock()) as rebuild_mock, \
             patch("handlers.sync_ass_mux_view", new=AsyncMock()) as sync_mock:
            await handlers.callback_ass_mux(callback)

        self.assertEqual(message.edit_calls[0][0], format_rescan_running())
        rebuild_mock.assert_awaited_once_with(100, 1)
        self.assertEqual(notice_mock.call_count, 2)
        sync_mock.assert_awaited_once()

    async def test_callback_ass_mux_open_add_sub_picker_edits_picker_view(self):
        message = DummyMessage(message_id=13)
        callback = DummyCallback(f"{handlers.ASS_MUX_PREFIX}open_add_sub_picker:3", message=message)
        with patch("handlers.check_callback_permission", new=AsyncMock(return_value=True)), \
             patch.object(handlers.ass_service, "ensure_mux_owner", new=Mock(return_value=True)), \
             patch.object(handlers.ass_service, "prepare_mux_add_sub_picker", new=Mock()) as prepare_mock, \
             patch.object(handlers.ass_service, "clear_mux_inline_notice", new=Mock()), \
             patch.object(handlers.ass_service, "format_mux_add_sub_picker", new=Mock(return_value="picker text")), \
             patch.object(handlers.ass_service, "build_mux_add_sub_picker_keyboard", new=Mock(return_value="picker kb")):
            await handlers.callback_ass_mux(callback)

        prepare_mock.assert_called_once_with(100, 1, 3)
        self.assertEqual(message.edit_calls[-1][0], "picker text")
        self.assertEqual(message.edit_calls[-1][1]["reply_markup"], "picker kb")

    async def test_callback_ass_mux_confirm_add_sub_updates_item_view(self):
        message = DummyMessage(message_id=14)
        callback = DummyCallback(f"{handlers.ASS_MUX_PREFIX}confirm_add_sub:2", message=message)
        with patch("handlers.check_callback_permission", new=AsyncMock(return_value=True)), \
             patch.object(handlers.ass_service, "ensure_mux_owner", new=Mock(return_value=True)), \
             patch.object(handlers.ass_service, "confirm_mux_add_sub_candidates", new=Mock(return_value="added")), \
             patch.object(handlers.ass_service, "set_mux_inline_notice", new=Mock()) as notice_mock, \
             patch.object(handlers.ass_service, "format_mux_item_detail", new=Mock(return_value="detail text")), \
             patch.object(handlers.ass_service, "build_mux_item_keyboard", new=Mock(return_value="detail kb")):
            await handlers.callback_ass_mux(callback)

        notice_mock.assert_called_once_with(100, 1, "added")
        self.assertEqual(message.edit_calls[-1][0], "detail text")
        self.assertEqual(message.edit_calls[-1][1]["reply_markup"], "detail kb")

    async def test_callback_ass_mux_run_now_uses_run_mux_and_progress_pump(self):
        bot = DummyBot()
        message = DummyMessage(message_id=15, bot=bot)
        callback = DummyCallback(f"{handlers.ASS_MUX_PREFIX}run_now", message=message, bot=bot)
        session = SimpleNamespace(
            plan=SimpleNamespace(items=[SimpleNamespace(subs=[1]), SimpleNamespace(subs=[1])]),
            dry_run=False,
        )
        with patch("handlers.check_callback_permission", new=AsyncMock(return_value=True)), \
             patch.object(handlers.ass_service, "ensure_mux_owner", new=Mock(return_value=True)), \
             patch.object(handlers.ass_service, "get_mux_session", new=Mock(return_value=session)), \
             patch.object(handlers.ass_service, "count_mux_executable_items", new=Mock(return_value=2)), \
             patch.object(handlers.ass_service, "run_mux", new=AsyncMock(return_value=(True, "mux done"))) as run_mock:
            await handlers.callback_ass_mux(callback)

        self.assertEqual(message.edit_calls[0][0], format_mux_running(processed=0, total=2, dry_run=False))
        self.assertEqual(message.edit_calls[-1][0], "mux done")
        run_mock.assert_awaited_once()

    async def test_pump_ass_mux_progress_uses_current_file_field(self):
        bot = DummyBot()
        session = SimpleNamespace(
            plan=SimpleNamespace(items=[SimpleNamespace(subs=[1]), SimpleNamespace(subs=[1])]),
            dry_run=True,
        )
        queue: asyncio.Queue[MuxProgressEvent | None] = asyncio.Queue()
        await queue.put(MuxProgressEvent(processed=1, total=2, current_file="demo.mkv"))
        await queue.put(None)

        await handlers.pump_ass_mux_progress(bot, 100, 50, session, queue)

        self.assertEqual(len(bot.edits), 1)
        self.assertIn("demo.mkv", bot.edits[0]["text"])
        self.assertIn("DRY-RUN", bot.edits[0]["text"])

    def test_handlers_source_has_no_removed_ass_service_api_calls(self):
        source = Path(handlers.__file__).read_text(encoding="utf-8")
        removed_api_names = [
            "execute_mux_session(",
            "close_mux_session(",
            "rescan_mux_session(",
            "set_mux_awaiting_field(",
            "set_mux_edit_item_index(",
            "set_mux_edit_page(",
            "clear_mux_edit_item(",
            "begin_mux_add_subtitle(",
            "cancel_mux_add_subtitle(",
            "set_mux_add_subtitle_page(",
            "pick_mux_add_subtitle_candidate(",
            "remove_mux_item_subtitle(",
            "begin_mux_edit_sub_field(",
            "toggle_mux_dry_run(",
            "toggle_mux_delete_subs(",
        ]
        for name in removed_api_names:
            with self.subTest(name=name):
                self.assertNotIn(name, source)
        self.assertNotIn('level="persistent"', source)


if __name__ == "__main__":
    unittest.main()
