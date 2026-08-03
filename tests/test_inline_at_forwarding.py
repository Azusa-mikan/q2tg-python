import unittest
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, patch

from src.forwarding import forward_telegram_to_onebot
from src.messages import TelegramMessage
from src.qbot import QGateway


class InlineAtForwardingTests(unittest.IsolatedAsyncioTestCase):
    async def test_inline_at_is_sent_as_onebot_at_segment(self) -> None:
        message = TelegramMessage(
            message_ids=(860_001,),
            group_id=-860_002,
            user_id=860_003,
            sender_name="Example Sender",
            text=None,
            at_user_id=860_004,
        )
        gateway = SimpleNamespace(send_group_message=AsyncMock(return_value=860_005))

        with (
            patch(
                "src.forwarding.sql.get_q_group",
                new_callable=AsyncMock,
                return_value=860_006,
            ),
            patch(
                "src.forwarding.sql.get_tg_forward_enabled",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch(
                "src.forwarding.sql.set_message_mapping",
                new_callable=AsyncMock,
            ),
        ):
            await forward_telegram_to_onebot(message, cast(QGateway, gateway))

        gateway.send_group_message.assert_awaited_once_with(
            group_id=860_006,
            message=[
                {"type": "text", "data": {"text": "Example Sender:\n"}},
                {"type": "at", "data": {"qq": "860004"}},
            ],
        )


if __name__ == "__main__":
    unittest.main()
