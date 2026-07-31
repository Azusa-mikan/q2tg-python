import unittest
from unittest.mock import patch

from fastapi import WebSocketException, status

from src.config import config
from src.ws import verify_snowluma_token


class WebSocketAuthTests(unittest.IsolatedAsyncioTestCase):
    async def test_accepts_valid_bearer_token(self) -> None:
        with patch.object(config, "onebot_token", "secret"):
            await verify_snowluma_token("Bearer secret")

    async def test_rejects_missing_or_invalid_bearer_token(self) -> None:
        with patch.object(config, "onebot_token", "secret"):
            for authorization in (None, "secret", "Basic secret", "Bearer wrong"):
                with self.subTest(authorization=authorization):
                    with self.assertRaises(WebSocketException) as raised:
                        await verify_snowluma_token(authorization)
                    self.assertEqual(raised.exception.code, status.WS_1008_POLICY_VIOLATION)


if __name__ == "__main__":
    unittest.main()
