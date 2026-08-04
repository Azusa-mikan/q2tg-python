from datetime import UTC, datetime

from telegram import (
    Chat,
    MessageOriginChannel,
    MessageOriginChat,
    MessageOriginHiddenUser,
    MessageOriginUser,
    User,
)

from src.tgbot.handlers import forward_origin_name


class TestForwardOrigin:
    def setup_method(self) -> None:
        self.date = datetime.now(UTC)

    def test_known_user_uses_full_name(self) -> None:
        origin = MessageOriginUser(
            date=self.date,
            sender_user=User(id=1, first_name="Alice", last_name="Smith", is_bot=False),
        )
        assert forward_origin_name(origin) == "Alice Smith"

    def test_hidden_user_uses_visible_forward_name(self) -> None:
        origin = MessageOriginHiddenUser(
            date=self.date,
            sender_user_name="Hidden User",
        )
        assert forward_origin_name(origin) == "Hidden User"

    def test_chat_uses_title(self) -> None:
        origin = MessageOriginChat(
            date=self.date,
            sender_chat=Chat(id=-1, type="group", title="Source Group"),
        )
        assert forward_origin_name(origin) == "Source Group"

    def test_channel_uses_title(self) -> None:
        origin = MessageOriginChannel(
            date=self.date,
            chat=Chat(id=-2, type="channel", title="Source Channel"),
            message_id=10,
        )
        assert forward_origin_name(origin) == "Source Channel"
