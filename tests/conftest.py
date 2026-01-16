import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from ls_py_handler.main import app, lifespan


@pytest_asyncio.fixture
async def client():
    """
    Fixture that creates an async test client with lifespan properly managed.
    
    The lifespan context manager must be entered manually for tests because
    AsyncClient does not trigger it automatically. This ensures app.state.s3_client
    is initialized before any tests run.
    """
    async with lifespan(app):
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test"
        ) as client:
            yield client
