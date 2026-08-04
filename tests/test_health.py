import httpx
import pytest

from src.api import fapp


@pytest.mark.asyncio
class TestHealth:
    async def test_health_endpoint(self) -> None:
        transport = httpx.ASGITransport(app=fapp)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
        ) as client:
            response = await client.get("/healthz")

        assert response.status_code == 200
        assert response.json() == {"status": "ok"}
