"""Tests: x-gamma is present in the spec and parsed correctly."""
import pytest
import pytest_asyncio
import httpx

from tests.fixtures.item_app import app
from gamma_client.spec import parse_spec


@pytest.mark.asyncio
async def test_gamma_present_in_openapi():
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        resp = await c.get("/openapi.json")
    schema = resp.json()
    publish_op = schema["paths"]["/items/{id}/publish"]["post"]
    assert "x-gamma" in publish_op, "publishItem should have x-gamma"
    gamma = publish_op["x-gamma"]
    assert gamma["requires_state"] == ["draft"]
    assert gamma["produces_state"] == "published"


@pytest.mark.asyncio
async def test_parse_spec_extracts_all_constrained_ops():
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        resp = await c.get("/openapi.json")
    gamma_map = parse_spec(resp.json())

    assert "publishItem" in gamma_map
    assert "archiveItem" in gamma_map
    assert "deleteItem" in gamma_map
    assert "checkout" in gamma_map
    assert "createItem" in gamma_map


@pytest.mark.asyncio
async def test_publish_item_gamma():
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        resp = await c.get("/openapi.json")
    gamma_map = parse_spec(resp.json())

    g = gamma_map["publishItem"]
    assert g.requires_state == ["draft"]
    assert g.produces_state == "published"
    assert g.method == "post"
    assert g.path == "/items/{id}/publish"


@pytest.mark.asyncio
async def test_archive_item_gamma():
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        resp = await c.get("/openapi.json")
    gamma_map = parse_spec(resp.json())

    g = gamma_map["archiveItem"]
    assert g.requires_state == ["published"]
    assert g.produces_state == "archived"
    assert g.forbidden_after == ["archiveItem"]


@pytest.mark.asyncio
async def test_checkout_requires_prior():
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        resp = await c.get("/openapi.json")
    gamma_map = parse_spec(resp.json())

    g = gamma_map["checkout"]
    assert g.requires_prior == ["addToCart"]


@pytest.mark.asyncio
async def test_create_item_has_postcondition():
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        resp = await c.get("/openapi.json")
    gamma_map = parse_spec(resp.json())

    g = gamma_map["createItem"]
    assert len(g.postconditions) == 1
    assert g.postconditions[0].effect == "resource_exists"
    assert g.postconditions[0].produces_state == "draft"
