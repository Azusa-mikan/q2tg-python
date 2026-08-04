from unittest.mock import patch

import pytest
from fastapi import WebSocketException, status

from src.config import config
from src.ws import verify_snowluma_token


@pytest.mark.asyncio
class TestWebSocketAuth:
    async def test_accepts_valid_bearer_token(self) -> None:
        with patch.object(config, "onebot_token", "secret"):
            await verify_snowluma_token("Bearer secret")

    @pytest.mark.parametrize(
        "authorization",
        [None, "secret", "Basic secret", "Bearer wrong"],
    )
    async def test_rejects_missing_or_invalid_bearer_token(
        self, authorization: str | None
    ) -> None:
        with patch.object(config, "onebot_token", "secret"):
            with pytest.raises(WebSocketException) as raised:
                await verify_snowluma_token(authorization)
            assert raised.value.code == status.WS_1008_POLICY_VIOLATION
