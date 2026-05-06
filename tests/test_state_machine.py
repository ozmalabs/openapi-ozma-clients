"""Tests: requires_state / produces_state enforcement."""
import pytest
import pytest_asyncio
import httpx

from gamma_client.errors import RequiresStateViolation
from gamma_client.session import GammaSession


@pytest.mark.asyncio
async def test_happy_path_draft_publish_archive(session: GammaSession):
    """Full lifecycle without violations."""
    resp = await session.call("createItem", json={"title": "hello"})
    assert resp.status_code == 200
    item_id = resp.json()["id"]
    rkey = f"item:{item_id}"

    # Seed state from server response (createItem produces_state="draft")
    session.set_state(rkey, "draft")

    resp = await session.call("publishItem", path_params={"id": item_id}, resource_key=rkey)
    assert resp.status_code == 200
    assert session.get_state(rkey) == "published"

    resp = await session.call("archiveItem", path_params={"id": item_id}, resource_key=rkey)
    assert resp.status_code == 200
    assert session.get_state(rkey) == "archived"


@pytest.mark.asyncio
async def test_cannot_archive_draft(session: GammaSession):
    """archiveItem requires_state=published; draft is inadmissible."""
    resp = await session.call("createItem", json={"title": "skip-publish"})
    item_id = resp.json()["id"]
    rkey = f"item:{item_id}"
    session.set_state(rkey, "draft")

    with pytest.raises(RequiresStateViolation) as exc_info:
        await session.call("archiveItem", path_params={"id": item_id}, resource_key=rkey)

    err = exc_info.value
    assert err.operation_id == "archiveItem"
    assert err.current_state == "draft"
    assert "published" in err.required_states
    # Error explains the WHY: what state was expected, what was found
    assert "draft" in str(err)
    assert "published" in str(err)
    assert rkey in str(err)


@pytest.mark.asyncio
async def test_cannot_publish_archived(session: GammaSession):
    """publishItem requires_state=draft; archived is inadmissible."""
    resp = await session.call("createItem", json={"title": "already-archived"})
    item_id = resp.json()["id"]
    rkey = f"item:{item_id}"
    session.set_state(rkey, "archived")

    with pytest.raises(RequiresStateViolation) as exc_info:
        await session.call("publishItem", path_params={"id": item_id}, resource_key=rkey)

    err = exc_info.value
    assert err.current_state == "archived"
    assert "draft" in err.required_states


@pytest.mark.asyncio
async def test_state_tracks_through_transitions(session: GammaSession):
    """produces_state updates the session state automatically after each call."""
    resp = await session.call("createItem", json={"title": "tracking"})
    item_id = resp.json()["id"]
    rkey = f"item:{item_id}"
    session.set_state(rkey, "draft")

    assert session.get_state(rkey) == "draft"
    await session.call("publishItem", path_params={"id": item_id}, resource_key=rkey)
    assert session.get_state(rkey) == "published"
    await session.call("archiveItem", path_params={"id": item_id}, resource_key=rkey)
    assert session.get_state(rkey) == "archived"


@pytest.mark.asyncio
async def test_no_violation_without_resource_key(session: GammaSession):
    """If no resource_key is given, requires_state is not checked (can't know the state)."""
    resp = await session.call("createItem", json={"title": "no-key"})
    item_id = resp.json()["id"]

    # No rkey — state constraint is not checked; server will return 409 but no Γ raise
    resp = await session.call("archiveItem", path_params={"id": item_id})
    assert resp.status_code == 409  # server rejects; Γ client did not pre-emptively raise
