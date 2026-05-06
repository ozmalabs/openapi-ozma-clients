"""Tests: requires_prior and forbidden_after enforcement."""
import pytest

from gamma_client.errors import ForbiddenAfterViolation, RequiresPriorViolation
from gamma_client.session import GammaSession


@pytest.mark.asyncio
async def test_checkout_without_cart_raises(session: GammaSession):
    """checkout requires_prior addToCart; calling checkout first is inadmissible."""
    with pytest.raises(RequiresPriorViolation) as exc_info:
        await session.call("checkout")

    err = exc_info.value
    assert err.operation_id == "checkout"
    assert "addToCart" in err.missing
    # Error explains WHY: which prior operations were not yet called
    assert "addToCart" in str(err)
    assert "checkout" in str(err)


@pytest.mark.asyncio
async def test_checkout_after_cart_succeeds(session: GammaSession):
    """After addToCart, checkout is admissible."""
    await session.call("addToCart", json={"item": "widget"})
    assert "addToCart" in session.called

    resp = await session.call("checkout")
    assert resp.status_code == 200
    assert "checkout" in session.called


@pytest.mark.asyncio
async def test_archive_forbidden_after_archive(session: GammaSession):
    """archiveItem is in its own forbidden_after — cannot archive twice."""
    resp = await session.call("createItem", json={"title": "once"})
    item_id = resp.json()["id"]
    rkey = f"item:{item_id}"
    session.set_state(rkey, "published")

    await session.call("archiveItem", path_params={"id": item_id}, resource_key=rkey)
    assert "archiveItem" in session.called

    # Second archive — blocked by forbidden_after before we even hit the server
    # (also blocked by requires_state since state is now "archived", but
    #  forbidden_after is checked first)
    with pytest.raises((ForbiddenAfterViolation, Exception)):
        await session.call("archiveItem", path_params={"id": item_id}, resource_key=rkey)


@pytest.mark.asyncio
async def test_delete_forbidden_after_delete(session: GammaSession):
    """deleteItem is in its own forbidden_after — cannot delete twice."""
    resp = await session.call("createItem", json={"title": "delete-me"})
    item_id = resp.json()["id"]
    rkey = f"item:{item_id}"
    session.set_state(rkey, "draft")

    await session.call("deleteItem", path_params={"id": item_id}, resource_key=rkey)
    assert "deleteItem" in session.called

    with pytest.raises(ForbiddenAfterViolation) as exc_info:
        await session.call("deleteItem", path_params={"id": item_id}, resource_key=rkey)

    err = exc_info.value
    assert err.operation_id == "deleteItem"
    assert err.blocked_by == "deleteItem"
    # Error explains WHY: which previously-called operation made this inadmissible
    assert "deleteItem" in str(err)
    assert "forbidden" in str(err).lower()


@pytest.mark.asyncio
async def test_called_set_tracks_operations(session: GammaSession):
    """Session accumulates called operationIds correctly."""
    assert len(session.called) == 0
    await session.call("addToCart", json={"item": "a"})
    await session.call("addToCart", json={"item": "b"})
    assert session.called == {"addToCart"}

    await session.call("checkout")
    assert "checkout" in session.called


@pytest.mark.asyncio
async def test_reset_clears_state(session: GammaSession):
    """reset() lets you re-run a sequence that would otherwise be blocked."""
    await session.call("addToCart", json={"item": "x"})
    await session.call("checkout")

    session.reset()
    assert len(session.called) == 0

    # Now checkout is blocked again (requires_prior addToCart)
    with pytest.raises(RequiresPriorViolation):
        await session.call("checkout")
