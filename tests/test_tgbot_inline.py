import unittest
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, patch

from telegram import Message, MessageEntity, Update
from telegram.ext import ContextTypes, InlineQueryHandler

from src.tgbot.handlers import (
    INLINE_AT_CONTEXT_LIMIT,
    INLINE_AT_TTL,
    InlineAtContext,
    InlineAtMemberSnapshot,
    TGhandlers,
)


class TelegramInlineAtTests(unittest.IsolatedAsyncioTestCase):
    async def test_at_creates_user_bound_group_context(self) -> None:
        handler = TGhandlers()
        message = SimpleNamespace(reply_text=AsyncMock())
        update = cast(
            Update,
            SimpleNamespace(
                effective_message=message,
                effective_chat=SimpleNamespace(id=-820_001, type="supergroup"),
                effective_user=SimpleNamespace(id=820_002),
            ),
        )

        with patch(
            "src.tgbot.handlers.sql.get_q_group",
            new_callable=AsyncMock,
            return_value=820_003,
        ):
            await handler.at(update, cast(ContextTypes.DEFAULT_TYPE, SimpleNamespace()))

        message.reply_text.assert_awaited_once()
        text = message.reply_text.await_args.args[0]
        markup = message.reply_text.await_args.kwargs["reply_markup"]
        button = markup.inline_keyboard[0][0]
        self.assertEqual(text, "请选择需要 @ 的 OneBot 群成员")
        self.assertEqual(button.text, "选择群成员")
        self.assertTrue(button.switch_inline_query_current_chat.startswith("at "))
        self.assertTrue(button.switch_inline_query_current_chat.endswith(" "))
        token = button.switch_inline_query_current_chat.removeprefix("at ").strip()
        inline_context = handler._inline_at_contexts[token]
        self.assertEqual(inline_context.user_id, 820_002)
        self.assertEqual(inline_context.tg_chat_id, -820_001)
        self.assertEqual(inline_context.q_group_id, 820_003)

    async def test_inline_at_searches_members_and_reuses_snapshot(self) -> None:
        handler = TGhandlers()
        handler._inline_at_contexts["example-token"] = InlineAtContext(
            user_id=830_001,
            tg_chat_id=-830_002,
            q_group_id=830_003,
            expires_at=INLINE_AT_TTL + 100.0,
        )
        gateway = SimpleNamespace(
            get_group_member_list=AsyncMock(
                return_value=[
                    {
                        "user_id": 830_004,
                        "nickname": "Example Nickname",
                        "card": "Sample Card",
                    },
                    {
                        "user_id": 830_005,
                        "nickname": "Second Member",
                        "card": "",
                    },
                    {"user_id": 830_006, "nickname": "", "card": ""},
                ]
            )
        )
        answer = AsyncMock()

        with (
            patch("src.tgbot.handlers.q_gateway", gateway),
            patch(
                "src.tgbot.handlers.sql.get_id_show_enabled",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch("src.tgbot.handlers.time.monotonic", return_value=100.0),
        ):
            for query in (
                "at example-token sample",
                "at example-token nickname",
                "at example-token 830004",
            ):
                inline_query = SimpleNamespace(
                    query=query,
                    from_user=SimpleNamespace(id=830_001),
                    offset="",
                    answer=answer,
                )
                await handler.inline_at(
                    cast(Update, SimpleNamespace(inline_query=inline_query)),
                    cast(ContextTypes.DEFAULT_TYPE, SimpleNamespace()),
                )

        gateway.get_group_member_list.assert_awaited_once_with(830_003)
        self.assertEqual(answer.await_count, 3)
        await_args = answer.await_args
        assert await_args is not None
        results = await_args.args[0]
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].title, "Sample Card[830004]")
        self.assertEqual(
            results[0].input_message_content.message_text,
            "@\u2063Sample Card[830004]",
        )
        entity = results[0].input_message_content.entities[0]
        self.assertEqual(entity.type, MessageEntity.TEXT_LINK)
        self.assertEqual(entity.offset, 1)
        self.assertEqual(entity.length, 1)
        self.assertRegex(entity.url or "", r"^https://q2tg\.invalid/token/[A-Za-z0-9_-]{32}$")
        self.assertNotIn("830004", entity.url or "")
        urls = [
            call.args[0][0].input_message_content.entities[0].url
            for call in answer.await_args_list
        ]
        self.assertEqual(len(set(urls)), 1)
        self.assertEqual(await_args.kwargs["cache_time"], 0)
        self.assertTrue(await_args.kwargs["is_personal"])

    async def test_inline_at_rejects_other_user_and_expired_token(self) -> None:
        handler = TGhandlers()
        handler._inline_at_contexts["example-token"] = InlineAtContext(
            user_id=840_001,
            tg_chat_id=-840_002,
            q_group_id=840_003,
            expires_at=200.0,
        )
        gateway = SimpleNamespace(get_group_member_list=AsyncMock())

        async def query(user_id: int, now: float) -> AsyncMock:
            answer = AsyncMock()
            inline_query = SimpleNamespace(
                query="at example-token",
                from_user=SimpleNamespace(id=user_id),
                offset="",
                answer=answer,
            )
            with (
                patch("src.tgbot.handlers.q_gateway", gateway),
                patch("src.tgbot.handlers.time.monotonic", return_value=now),
            ):
                await handler.inline_at(
                    cast(Update, SimpleNamespace(inline_query=inline_query)),
                    cast(ContextTypes.DEFAULT_TYPE, SimpleNamespace()),
                )
            return answer

        answer = await query(840_004, 100.0)
        answer.assert_awaited_once_with([], cache_time=0, is_personal=True)
        answer = await query(840_001, 201.0)
        answer.assert_awaited_once_with([], cache_time=0, is_personal=True)
        gateway.get_group_member_list.assert_not_awaited()

    def test_inline_query_handler_is_registered(self) -> None:
        self.assertTrue(
            any(
                isinstance(handler, InlineQueryHandler)
                for handler in TGhandlers().get_handlers()
            )
        )

    async def test_inline_result_entity_restores_onebot_user_id(self) -> None:
        handler = TGhandlers()
        inline_context = InlineAtContext(
            user_id=850_003,
            tg_chat_id=-850_004,
            q_group_id=850_005,
            expires_at=200.0,
        )
        handler._inline_at_contexts["example-context"] = inline_context
        handler._inline_at_member_snapshots[850_005] = InlineAtMemberSnapshot(
            members=[{"user_id": 850_002, "nickname": "Sample Card", "card": ""}],
            expires_at=200.0,
        )
        token = handler._inline_at_selection_token(
            "example-context",
            inline_context,
            850_002,
        )
        text = "@\u2063Sample Card"
        message = cast(
            Message,
            SimpleNamespace(
                text=text,
                chat_id=-850_004,
                from_user=SimpleNamespace(id=850_003),
                via_bot=SimpleNamespace(id=850_001),
                entities=(
                    MessageEntity(
                        type=MessageEntity.TEXT_LINK,
                        offset=1,
                        length=1,
                        url=f"https://q2tg.invalid/token/{token}",
                    ),
                ),
            ),
        )

        with (
            patch("src.tgbot.handlers.time.monotonic", return_value=100.0),
            patch(
                "src.tgbot.handlers.sql.get_q_group",
                new_callable=AsyncMock,
                return_value=850_005,
            ),
        ):
            self.assertEqual(
                await handler._inline_at_user_id(message, 850_001),
                850_002,
            )

    async def test_inline_selection_survives_snapshot_eviction(self) -> None:
        handler = TGhandlers()
        inline_context = InlineAtContext(
            user_id=851_001,
            tg_chat_id=-851_002,
            q_group_id=851_003,
            expires_at=200.0,
        )
        handler._inline_at_contexts["example-context"] = inline_context
        token = handler._inline_at_selection_token(
            "example-context",
            inline_context,
            851_004,
        )
        text = "@\u2063Example Member"
        message = cast(
            Message,
            SimpleNamespace(
                text=text,
                chat_id=-851_002,
                from_user=SimpleNamespace(id=851_001),
                via_bot=SimpleNamespace(id=851_005),
                entities=(
                    MessageEntity(
                        type=MessageEntity.TEXT_LINK,
                        offset=1,
                        length=1,
                        url=f"https://q2tg.invalid/token/{token}",
                    ),
                ),
            ),
        )

        with (
            patch("src.tgbot.handlers.time.monotonic", return_value=100.0),
            patch(
                "src.tgbot.handlers.sql.get_q_group",
                new_callable=AsyncMock,
                return_value=851_003,
            ),
        ):
            self.assertEqual(
                await handler._inline_at_user_id(message, 851_005),
                851_004,
            )

    async def test_inline_result_rejects_malformed_user_id(self) -> None:
        text = "@\u2063Example Member"
        message = cast(
            Message,
            SimpleNamespace(
                text=text,
                chat_id=-875_001,
                from_user=SimpleNamespace(id=875_002),
                via_bot=SimpleNamespace(id=875_003),
                entities=(
                    MessageEntity(
                        type=MessageEntity.TEXT_LINK,
                        offset=1,
                        length=1,
                        url="https://q2tg.invalid/token/unknown?extra=value",
                    ),
                ),
            ),
        )

        self.assertIsNone(await TGhandlers()._inline_at_user_id(message, 875_003))

    async def test_inline_result_rejects_invalid_selection_context(self) -> None:
        handler = TGhandlers()
        inline_context = InlineAtContext(
            user_id=879_001,
            tg_chat_id=-879_002,
            q_group_id=879_003,
            expires_at=200.0,
        )
        handler._inline_at_contexts["example-context"] = inline_context
        handler._inline_at_member_snapshots[879_003] = InlineAtMemberSnapshot(
            members=[{"user_id": 879_004, "nickname": "Example Member", "card": ""}],
            expires_at=200.0,
        )
        token = handler._inline_at_selection_token(
            "example-context",
            inline_context,
            879_004,
        )
        text = "@\u2063Example Member"

        def message(*, user_id: int, chat_id: int, url: str) -> Message:
            return cast(
                Message,
                SimpleNamespace(
                    text=text,
                    chat_id=chat_id,
                    from_user=SimpleNamespace(id=user_id),
                    via_bot=SimpleNamespace(id=879_005),
                    entities=(
                        MessageEntity(
                            type=MessageEntity.TEXT_LINK,
                            offset=1,
                            length=1,
                            url=url,
                        ),
                    ),
                ),
            )

        cases = (
            message(
                user_id=879_006,
                chat_id=-879_002,
                url=f"https://q2tg.invalid/token/{token}",
            ),
            message(
                user_id=879_001,
                chat_id=-879_007,
                url=f"https://q2tg.invalid/token/{token}",
            ),
            message(
                user_id=879_001,
                chat_id=-879_002,
                url=f"https://q2tg.invalid/token/{token}?extra=value",
            ),
            message(
                user_id=879_001,
                chat_id=-879_002,
                url=f"https://q2tg.invalid/token/{token}#fragment",
            ),
            message(
                user_id=879_001,
                chat_id=-879_002,
                url=f"https://example.invalid/token/{token}",
            ),
        )

        with (
            patch("src.tgbot.handlers.time.monotonic", return_value=100.0),
            patch(
                "src.tgbot.handlers.sql.get_q_group",
                new_callable=AsyncMock,
                return_value=879_008,
            ),
        ):
            for candidate in cases:
                with self.subTest(url=candidate.entities[0].url):
                    self.assertIsNone(
                        await handler._inline_at_user_id(candidate, 879_005)
                    )

    async def test_inline_result_rejects_expired_selection(self) -> None:
        handler = TGhandlers()
        inline_context = InlineAtContext(
            user_id=881_001,
            tg_chat_id=-881_002,
            q_group_id=881_003,
            expires_at=100.0,
        )
        handler._inline_at_contexts["example-context"] = inline_context
        handler._inline_at_member_snapshots[881_003] = InlineAtMemberSnapshot(
            members=[{"user_id": 881_004, "nickname": "Example Member", "card": ""}],
            expires_at=100.0,
        )
        token = handler._inline_at_selection_token(
            "example-context",
            inline_context,
            881_004,
        )
        text = "@\u2063Example Member"
        message = cast(
            Message,
            SimpleNamespace(
                text=text,
                chat_id=-881_002,
                from_user=SimpleNamespace(id=881_001),
                via_bot=SimpleNamespace(id=881_005),
                entities=(
                    MessageEntity(
                        type=MessageEntity.TEXT_LINK,
                        offset=1,
                        length=1,
                        url=f"https://q2tg.invalid/token/{token}",
                    ),
                ),
            ),
        )

        with patch("src.tgbot.handlers.time.monotonic", return_value=101.0):
            self.assertIsNone(await handler._inline_at_user_id(message, 881_005))

    async def test_receive_text_enqueues_valid_inline_at(self) -> None:
        handler = TGhandlers()
        inline_context = InlineAtContext(
            user_id=876_003,
            tg_chat_id=-876_001,
            q_group_id=876_006,
            expires_at=200.0,
        )
        handler._inline_at_contexts["example-context"] = inline_context
        handler._inline_at_member_snapshots[876_006] = InlineAtMemberSnapshot(
            members=[{"user_id": 876_005, "nickname": "Sample Card", "card": ""}],
            expires_at=200.0,
        )
        token = handler._inline_at_selection_token(
            "example-context",
            inline_context,
            876_005,
        )
        text = "@\u2063Sample Card"
        message = SimpleNamespace(
            text=text,
            chat_id=-876_001,
            message_id=876_002,
            from_user=SimpleNamespace(
                id=876_003,
                full_name="Example Sender",
                is_bot=False,
            ),
            via_bot=SimpleNamespace(id=876_004),
            entities=(
                MessageEntity(
                    type=MessageEntity.TEXT_LINK,
                    offset=1,
                    length=1,
                    url=f"https://q2tg.invalid/token/{token}",
                ),
            ),
            forward_origin=None,
            reply_to_message=None,
        )
        update = cast(Update, SimpleNamespace(effective_message=message))
        context = cast(
            ContextTypes.DEFAULT_TYPE,
            SimpleNamespace(bot=SimpleNamespace(id=876_004)),
        )

        with (
            patch(
                "src.tgbot.handlers.sql.get_tg_forward_enabled",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch(
                "src.tgbot.handlers.sql.get_q_group",
                new_callable=AsyncMock,
                return_value=876_006,
            ),
            patch("src.tgbot.handlers.time.monotonic", return_value=100.0),
            patch("src.tgbot.handlers.telegram_forward_task", return_value="task") as task,
            patch(
                "src.tgbot.handlers.message_bus.put",
                new_callable=AsyncMock,
            ) as put,
        ):
            await handler.receive_message(update, context)

        forwarded = task.call_args.args[0]
        self.assertIsNone(forwarded.text)
        self.assertEqual(forwarded.at_user_id, 876_005)
        put.assert_awaited_once_with("task")

    async def test_receive_text_logs_and_blocks_invalid_inline_at(self) -> None:
        message = SimpleNamespace(
            text="@Sample Card",
            chat_id=-878_001,
            message_id=878_002,
            from_user=SimpleNamespace(
                id=878_003,
                full_name="Example Sender",
                is_bot=False,
            ),
            via_bot=SimpleNamespace(id=878_004),
            entities=(),
        )
        update = cast(Update, SimpleNamespace(effective_message=message))
        context = cast(
            ContextTypes.DEFAULT_TYPE,
            SimpleNamespace(bot=SimpleNamespace(id=878_004)),
        )

        with (
            patch(
                "src.tgbot.handlers.sql.get_tg_forward_enabled",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch("src.tgbot.handlers.baselog.error") as error,
            patch("src.tgbot.handlers.telegram_forward_task") as task,
            patch(
                "src.tgbot.handlers.message_bus.put",
                new_callable=AsyncMock,
            ) as put,
        ):
            await TGhandlers().receive_message(update, context)

        error.assert_called_once()
        self.assertIn("已阻止文本降级", error.call_args.args[0])
        task.assert_not_called()
        put.assert_not_awaited()

    def test_inline_entity_log_redacts_selection_token(self) -> None:
        token = "example-secret-selection-token"
        entity = MessageEntity(
            type=MessageEntity.TEXT_LINK,
            offset=0,
            length=10,
            url=f"https://q2tg.invalid/token/{token}",
        )

        logged = TGhandlers._inline_at_entity_log(entity)

        self.assertNotIn(token, repr(logged))
        self.assertIn("<sha256:", str(logged["url"]))

    def test_inline_member_name_reuses_onebot_user_name(self) -> None:
        self.assertEqual(
            TGhandlers._inline_member_name({"card": "", "nickname": ""}),
            "OneBot 用户",
        )

    async def test_at_contexts_have_capacity_limit(self) -> None:
        handler = TGhandlers()
        for index in range(INLINE_AT_CONTEXT_LIMIT):
            handler._inline_at_contexts[f"old-{index}"] = InlineAtContext(
                user_id=880_001,
                tg_chat_id=-880_002,
                q_group_id=880_003,
                expires_at=200.0,
            )
        message = SimpleNamespace(reply_text=AsyncMock())
        update = cast(
            Update,
            SimpleNamespace(
                effective_message=message,
                effective_chat=SimpleNamespace(id=-880_002, type="supergroup"),
                effective_user=SimpleNamespace(id=880_001),
            ),
        )

        with (
            patch("src.tgbot.handlers.time.monotonic", return_value=100.0),
            patch(
                "src.tgbot.handlers.sql.get_q_group",
                new_callable=AsyncMock,
                return_value=880_003,
            ),
        ):
            await handler.at(update, cast(ContextTypes.DEFAULT_TYPE, SimpleNamespace()))

        self.assertEqual(len(handler._inline_at_contexts), INLINE_AT_CONTEXT_LIMIT)
        self.assertNotIn("old-0", handler._inline_at_contexts)


if __name__ == "__main__":
    unittest.main()
