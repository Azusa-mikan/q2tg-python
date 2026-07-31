import unittest

import httpx

from src.api import fapp


class HealthTests(unittest.IsolatedAsyncioTestCase):
    async def test_health_endpoint(self) -> None:
        transport = httpx.ASGITransport(app=fapp)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
        ) as client:
            response = await client.get("/healthz")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})


if __name__ == "__main__":
    unittest.main()
