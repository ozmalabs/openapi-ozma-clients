"""
GammaMock tests.

The centrepiece: the same async test function runs against both the real
ASGI server and the mock transport. If both pass, the mock is valid.
No hand-written stubs. No assumptions encoded separately from the spec.
"""
from __future__ import annotations

import pytest
import httpx

from tests.fixtures.item_app import app
from tenet.mock import GammaMock


# ---------------------------------------------------------------------------
# Shared test scenarios — transport-agnostic
# ---------------------------------------------------------------------------

async def _create_item(client: httpx.AsyncClient, title: str = "hello") -> int:
    r = await client.post("/items", json={"title": title, "body": ""})
    assert r.status_code == 200, r.text
    data = r.json()
    assert "id" in data
    assert data["status"] == "draft"
    return data["id"]


async def _run_full_lifecycle(client: httpx.AsyncClient) -> None:
    """Complete lifecycle: create → publish → archive."""
    item_id = await _create_item(client)

    r = await client.post(f"/items/{item_id}/publish")
    assert r.status_code == 200
    assert r.json()["status"] == "published"

    r = await client.post(f"/items/{item_id}/archive")
    assert r.status_code == 200
    assert r.json()["status"] == "archived"


async def _run_grammar_enforcement(client: httpx.AsyncClient) -> None:
    """Grammar violations are caught and explained."""
    item_id = await _create_item(client)

    # Can't archive from draft — requires published
    r = await client.post(f"/items/{item_id}/archive")
    assert r.status_code == 409
    body = r.json()
    assert body["violation"] == "state_violation"
    assert body["current_state"] == "draft"
    assert "published" in body["required_state"]
    assert "archive" in body["description"].lower() or "published" in body["description"]

    # Can't checkout without addToCart
    r = await client.post("/checkout")
    assert r.status_code == 409
    body = r.json()
    assert body["violation"] == "requires_prior"
    assert "addToCart" in body["missing_prior"]


async def _run_cart_checkout(client: httpx.AsyncClient) -> None:
    r = await client.post("/cart", json={"item": "widget"})
    assert r.status_code == 200

    r = await client.post("/checkout")
    assert r.status_code == 200


# ---------------------------------------------------------------------------
# The proof: same test, two transports
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_full_lifecycle_real_server():
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        await _run_full_lifecycle(c)


@pytest.mark.asyncio
async def test_full_lifecycle_mock():
    mock = GammaMock.from_app(app)
    async with httpx.AsyncClient(transport=mock, base_url="http://mock") as c:
        await _run_full_lifecycle(c)


@pytest.mark.asyncio
async def test_grammar_enforcement_real_server():
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        await _run_grammar_enforcement(c)


@pytest.mark.asyncio
async def test_grammar_enforcement_mock():
    mock = GammaMock.from_app(app)
    async with httpx.AsyncClient(transport=mock, base_url="http://mock") as c:
        await _run_grammar_enforcement(c)


@pytest.mark.asyncio
async def test_cart_checkout_real_server():
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        await _run_cart_checkout(c)


@pytest.mark.asyncio
async def test_cart_checkout_mock():
    mock = GammaMock.from_app(app)
    async with httpx.AsyncClient(transport=mock, base_url="http://mock") as c:
        await _run_cart_checkout(c)


# ---------------------------------------------------------------------------
# Mock-specific: schema-valid responses
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_mock_response_is_schema_valid():
    """Response bodies have the right shape — generated from the schema."""
    mock = GammaMock.from_app(app)
    async with httpx.AsyncClient(transport=mock, base_url="http://mock") as c:
        r = await c.post("/items", json={"title": "test item", "body": "some body"})
    body = r.json()
    assert isinstance(body["id"], int)
    assert body["title"] == "test item"
    assert body["status"] == "draft"


@pytest.mark.asyncio
async def test_mock_get_returns_stored_resource():
    """GET after POST returns the stored resource, not a fresh synthetic one."""
    mock = GammaMock.from_app(app)
    async with httpx.AsyncClient(transport=mock, base_url="http://mock") as c:
        r = await c.post("/items", json={"title": "stored title", "body": ""})
        item_id = r.json()["id"]

        r = await c.get(f"/items/{item_id}")
        assert r.status_code == 200
        assert r.json()["title"] == "stored title"
        assert r.json()["id"] == item_id


@pytest.mark.asyncio
async def test_mock_state_reflected_in_response():
    """produces_state is reflected in the response body's status field."""
    mock = GammaMock.from_app(app)
    async with httpx.AsyncClient(transport=mock, base_url="http://mock") as c:
        r = await c.post("/items", json={"title": "x", "body": ""})
        item_id = r.json()["id"]

        r = await c.post(f"/items/{item_id}/publish")
        assert r.json()["status"] == "published"

        r = await c.post(f"/items/{item_id}/archive")
        assert r.json()["status"] == "archived"


@pytest.mark.asyncio
async def test_mock_delete_returns_confirmation():
    mock = GammaMock.from_app(app)
    async with httpx.AsyncClient(transport=mock, base_url="http://mock") as c:
        r = await c.post("/items", json={"title": "delete me", "body": ""})
        item_id = r.json()["id"]

        r = await c.delete(f"/items/{item_id}")
        assert r.status_code == 200
        assert r.json()["deleted"] == item_id


# ---------------------------------------------------------------------------
# Mock-specific: session inspection and reset
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_mock_tracks_called_operations():
    mock = GammaMock.from_app(app)
    async with httpx.AsyncClient(transport=mock, base_url="http://mock") as c:
        await c.post("/items", json={"title": "x", "body": ""})
        await c.post("/cart", json={"item": "y"})

    assert "createItem" in mock.called()
    assert "addToCart" in mock.called()


@pytest.mark.asyncio
async def test_mock_reset_clears_state():
    mock = GammaMock.from_app(app)
    async with httpx.AsyncClient(transport=mock, base_url="http://mock") as c:
        await c.post("/cart", json={"item": "x"})
        await c.post("/checkout")

    mock.reset()
    assert mock.called() == set()

    # After reset, checkout is blocked again
    async with httpx.AsyncClient(transport=mock, base_url="http://mock") as c:
        r = await c.post("/checkout")
    assert r.status_code == 409


@pytest.mark.asyncio
async def test_mock_from_spec_file(tmp_path):
    """Mock can be built from a spec file — no app needed."""
    import json as json_module
    from tenet.spec import load_spec_file

    spec_path = tmp_path / "openapi.json"
    spec_path.write_text(json_module.dumps(app.openapi()))

    mock = GammaMock.from_spec(load_spec_file(spec_path))
    async with httpx.AsyncClient(transport=mock, base_url="http://mock") as c:
        r = await c.post("/items", json={"title": "from file", "body": ""})
    assert r.status_code == 200
    assert r.json()["status"] == "draft"


# ---------------------------------------------------------------------------
# Mock-specific: unknown routes
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_mock_returns_404_for_unknown_route():
    mock = GammaMock.from_app(app)
    async with httpx.AsyncClient(transport=mock, base_url="http://mock") as c:
        r = await c.get("/nonexistent/route")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Mock-specific: forbidden_after enforced
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_mock_forbidden_after_archive():
    """archiveItem is in its own forbidden_after — second call blocked."""
    mock = GammaMock.from_app(app)
    async with httpx.AsyncClient(transport=mock, base_url="http://mock") as c:
        r = await c.post("/items", json={"title": "x", "body": ""})
        item_id = r.json()["id"]

        await c.post(f"/items/{item_id}/publish")
        r = await c.post(f"/items/{item_id}/archive")
        assert r.status_code == 200

        # Second archive — forbidden
        r = await c.post(f"/items/{item_id}/archive")
        assert r.status_code == 409
        assert r.json()["violation"] == "forbidden_after"
        assert r.json()["blocked_by"] == "archiveItem"
