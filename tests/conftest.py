"""
Shared fixtures.

Starts the reference FastAPI app in-process using httpx.AsyncClient + ASGITransport,
fetches the OpenAPI spec, and builds a fresh GammaSession per test.
"""
from __future__ import annotations

import pytest
import pytest_asyncio
import httpx

from tests.fixtures.item_app import app
from gamma_client.spec import parse_spec
from gamma_client.session import GammaSession


@pytest_asyncio.fixture
async def client() -> httpx.AsyncClient:
    """Raw httpx client against the ASGI app — no Γ enforcement."""
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


@pytest_asyncio.fixture
async def gamma_map(client: httpx.AsyncClient) -> dict:
    """Parse the OpenAPI spec and extract x-gamma."""
    resp = await client.get("/openapi.json")
    resp.raise_for_status()
    return parse_spec(resp.json())


@pytest_asyncio.fixture
async def session(gamma_map: dict) -> GammaSession:
    """Fresh GammaSession backed by the in-process ASGI app."""
    transport = httpx.ASGITransport(app=app)
    async with GammaSession(
        "http://test", gamma_map
    ) as s:
        # Patch the internal client to use ASGI transport
        s._client = httpx.AsyncClient(transport=transport, base_url="http://test")
        yield s
        await s._client.aclose()
