"""Local identity API contract."""

import pytest
from httpx import ASGITransport, AsyncClient

from app.application.local_user import build_local_user
from app.config import Settings
from app.main import app


@pytest.mark.asyncio
async def test_current_user_endpoint_exposes_local_identity_without_login() -> None:
    app.state.local_user = build_local_user(
        Settings(single_user_display_name="训练者", _env_file=None)
    )
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get("/api/v1/users/me")
    finally:
        app.state.local_user = None

    assert response.status_code == 200
    assert response.json()["display_name"] == "训练者"
    assert response.json()["email"] == "demo@fitweek.local"
