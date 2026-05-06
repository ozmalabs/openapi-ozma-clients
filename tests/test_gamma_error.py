"""
Tests: GammaError returned from server — not raised, returned.

The server returns structured graph errors that explain WHY an operation
was inadmissible. No HTTPException anywhere in the fixture app.
"""
import pytest
import httpx

from tests.fixtures.item_app import app


@pytest.mark.asyncio
async def test_get_nonexistent_item_returns_404_with_why():
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        resp = await c.get("/items/99999")

    assert resp.status_code == 404
    body = resp.json()
    assert body["violation"] == "resource_not_found"
    assert "99999" in body["description"]
    assert "does not exist" in body["description"]


@pytest.mark.asyncio
async def test_publish_archived_item_returns_409_with_why():
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        # Create and immediately archive
        r = await c.post("/items", json={"title": "x"})
        item_id = r.json()["id"]
        await c.post(f"/items/{item_id}/publish")
        await c.post(f"/items/{item_id}/archive")

        # Now try to publish the archived item
        resp = await c.post(f"/items/{item_id}/publish")

    assert resp.status_code == 409
    body = resp.json()
    assert body["violation"] == "state_violation"
    assert body["current_state"] == "archived"
    assert "draft" in body["required_state"]
    # WHY: description names the operation, current state, and required states
    assert "publishItem" in body["description"]
    assert "archived" in body["description"]
    assert "draft" in body["description"]


@pytest.mark.asyncio
async def test_archive_draft_item_returns_409_with_why():
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        r = await c.post("/items", json={"title": "y"})
        item_id = r.json()["id"]

        resp = await c.post(f"/items/{item_id}/archive")

    assert resp.status_code == 409
    body = resp.json()
    assert body["violation"] == "state_violation"
    assert body["current_state"] == "draft"
    assert "published" in body["required_state"]


@pytest.mark.asyncio
async def test_delete_archived_item_returns_409_with_why():
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        r = await c.post("/items", json={"title": "z"})
        item_id = r.json()["id"]
        await c.post(f"/items/{item_id}/publish")
        await c.post(f"/items/{item_id}/archive")

        resp = await c.delete(f"/items/{item_id}")

    assert resp.status_code == 409
    body = resp.json()
    assert body["violation"] == "state_violation"
    assert body["current_state"] == "archived"


@pytest.mark.asyncio
async def test_gamma_error_body_is_structured_not_just_detail():
    """
    The response is a typed graph error — not {detail: "some string"}.
    It carries the full state needed to understand and recover.
    """
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        resp = await c.get("/items/0")

    body = resp.json()
    # Must have violation type — not just HTTP status
    assert "violation" in body
    # Must explain why — not just "not found"
    assert "description" in body
    # Must not use the HTTPException shape
    assert "detail" not in body
