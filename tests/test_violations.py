"""
Tests: violation error quality.

The core property: a Γ violation does not merely say THAT it failed.
It says WHY — the full graph state at the point of failure:
what was the current state, what was the grammar, which transition was attempted,
and what made it inadmissible.
"""
import pytest

from tenet.errors import (
    ForbiddenAfterViolation,
    GammaViolation,
    RequiresPriorViolation,
    RequiresStateViolation,
)
from tenet.session import GammaSession


@pytest.mark.asyncio
async def test_requires_state_error_reports_current_state(session: GammaSession):
    resp = await session.call("createItem", json={"title": "x"})
    item_id = resp.json()["id"]
    rkey = f"item:{item_id}"
    session.set_state(rkey, "archived")

    with pytest.raises(RequiresStateViolation) as exc_info:
        await session.call("publishItem", path_params={"id": item_id}, resource_key=rkey)

    err = exc_info.value
    # WHY: current state
    assert err.current_state == "archived"
    # WHY: what was required
    assert err.required_states == ["draft"]
    # WHY: which resource
    assert err.resource_key == rkey
    # WHY: which operation was attempted
    assert err.operation_id == "publishItem"
    # Human-readable string contains all of the above
    msg = str(err)
    assert "archived" in msg
    assert "draft" in msg
    assert rkey in msg
    assert "publishItem" in msg


@pytest.mark.asyncio
async def test_requires_prior_error_names_missing_ops(session: GammaSession):
    with pytest.raises(RequiresPriorViolation) as exc_info:
        await session.call("checkout")

    err = exc_info.value
    # WHY: which operations hadn't been called
    assert "addToCart" in err.missing
    # WHY: which operation was attempted
    assert err.operation_id == "checkout"
    msg = str(err)
    assert "checkout" in msg
    assert "addToCart" in msg


@pytest.mark.asyncio
async def test_forbidden_after_error_names_blocker(session: GammaSession):
    resp = await session.call("createItem", json={"title": "del"})
    item_id = resp.json()["id"]
    rkey = f"item:{item_id}"
    session.set_state(rkey, "draft")

    await session.call("deleteItem", path_params={"id": item_id}, resource_key=rkey)

    with pytest.raises(ForbiddenAfterViolation) as exc_info:
        await session.call("deleteItem", path_params={"id": item_id}, resource_key=rkey)

    err = exc_info.value
    # WHY: which previously-called operation made this inadmissible
    assert err.blocked_by == "deleteItem"
    assert err.operation_id == "deleteItem"
    msg = str(err)
    assert "deleteItem" in msg
    assert "forbidden" in msg.lower()


@pytest.mark.asyncio
async def test_gamma_violation_carries_spec(session: GammaSession):
    """The violation carries the OperationGamma so callers can inspect the full grammar."""
    with pytest.raises(RequiresPriorViolation) as exc_info:
        await session.call("checkout")

    err = exc_info.value
    assert err.gamma is not None
    assert err.gamma.operation_id == "checkout"
    assert err.gamma.requires_prior == ["addToCart"]


@pytest.mark.asyncio
async def test_no_violation_mode_records_without_raising(gamma_map: dict):
    """raise_on_violation=False records violations without raising."""
    transport = __import__("httpx").ASGITransport(app=__import__("tests.fixtures.item_app", fromlist=["app"]).app)
    async with GammaSession("http://test", gamma_map, raise_on_violation=False) as s:
        import httpx
        s._client = httpx.AsyncClient(transport=transport, base_url="http://test")

        await s.call("checkout")  # violates requires_prior — but no raise

        assert len(s.violations) == 1
        v = s.violations[0]
        assert isinstance(v, RequiresPriorViolation)
        assert v.operation_id == "checkout"
