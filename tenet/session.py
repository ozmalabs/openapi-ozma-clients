"""
GammaSession — an httpx-based HTTP session that enforces x-gamma constraints.

Tracks:
- which operationIds have been called this session (for requires_prior / forbidden_after)
- resource states keyed by resource URL or explicit key (for requires_state / produces_state)

Usage::

    from tenet.session import GammaSession
    from tenet.spec import parse_spec, load_spec_url

    spec_raw = load_spec_url("http://localhost:8000/openapi.json")
    gamma_map = parse_spec(spec_raw)

    async with GammaSession("http://localhost:8000", gamma_map) as session:
        await session.call("createItem", json={"title": "hello"})
        await session.call("publishItem", path_params={"id": 1})
        await session.call("archiveItem", path_params={"id": 1})

        # This would raise RequiresPriorViolation:
        # await session.call("archiveItem", path_params={"id": 1})
"""
from __future__ import annotations

import re
from typing import Any

import httpx

from tenet.errors import (
    ForbiddenAfterViolation,
    GammaViolation,
    RequiresPriorViolation,
    RequiresStateViolation,
)
from tenet.spec import OperationGamma


class GammaSession:
    """
    Wraps httpx.AsyncClient with Γ enforcement.

    Parameters
    ----------
    base_url:
        Root URL of the API (e.g. "http://localhost:8000").
    gamma_map:
        Output of parse_spec() — operationId → OperationGamma.
    raise_on_violation:
        If False, violations are recorded in .violations but not raised.
        Default True.
    """

    def __init__(
        self,
        base_url: str,
        gamma_map: dict[str, OperationGamma],
        *,
        raise_on_violation: bool = True,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.gamma_map = gamma_map
        self.raise_on_violation = raise_on_violation

        # Session state
        self.called: set[str] = set()
        # resource_states: maps resource_key → current state string
        # resource_key is either an explicit key passed to call() or the
        # resolved URL of the resource.
        self.resource_states: dict[str, str] = {}
        self.violations: list[GammaViolation] = []

        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> "GammaSession":
        self._client = httpx.AsyncClient(base_url=self.base_url)
        return self

    async def __aexit__(self, *args: Any) -> None:
        if self._client:
            await self._client.aclose()

    # ------------------------------------------------------------------
    # Public call interface
    # ------------------------------------------------------------------

    async def call(
        self,
        operation_id: str,
        *,
        path_params: dict[str, Any] | None = None,
        resource_key: str | None = None,
        json: Any = None,
        data: Any = None,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        """
        Make an API call by operationId, enforcing Γ before the request.

        Parameters
        ----------
        operation_id:
            The operationId from the OpenAPI spec.
        path_params:
            Dict of path parameter substitutions (e.g. {"id": 42}).
        resource_key:
            Explicit state-tracking key for this resource. If None, the
            resolved URL is used.
        """
        gamma = self.gamma_map.get(operation_id)

        if gamma is not None:
            self._check_constraints(operation_id, gamma, resource_key)

        url = self._resolve_url(operation_id, path_params)
        method = gamma.method if gamma else "get"
        rkey = resource_key or url

        assert self._client is not None, "use GammaSession as async context manager"
        response = await self._client.request(
            method.upper(),
            url,
            json=json,
            data=data,
            params=params,
            headers=headers,
        )

        # Post-call: update session state
        self.called.add(operation_id)
        if gamma and gamma.produces_state:
            self.resource_states[rkey] = gamma.produces_state

        return response

    # ------------------------------------------------------------------
    # Constraint checking
    # ------------------------------------------------------------------

    def _check_constraints(
        self,
        operation_id: str,
        gamma: OperationGamma,
        resource_key: str | None,
    ) -> None:
        # requires_prior: all listed operationIds must have been called
        if gamma.requires_prior:
            missing = [op for op in gamma.requires_prior if op not in self.called]
            if missing:
                v = RequiresPriorViolation(operation_id, missing, gamma)
                self._handle_violation(v)

        # forbidden_after: if any listed operationId was called, this op is blocked
        if gamma.forbidden_after:
            for blocker in gamma.forbidden_after:
                if blocker in self.called:
                    v = ForbiddenAfterViolation(operation_id, blocker, gamma)
                    self._handle_violation(v)
                    break

        # requires_state: resource must be in one of the listed states
        if gamma.requires_state and resource_key is not None:
            current = self.resource_states.get(resource_key)
            if current not in gamma.requires_state:
                v = RequiresStateViolation(
                    operation_id,
                    resource_key,
                    current,
                    gamma.requires_state,
                    gamma,
                )
                self._handle_violation(v)

    def _handle_violation(self, v: GammaViolation) -> None:
        self.violations.append(v)
        if self.raise_on_violation:
            raise v

    # ------------------------------------------------------------------
    # URL resolution
    # ------------------------------------------------------------------

    def _resolve_url(
        self,
        operation_id: str,
        path_params: dict[str, Any] | None,
    ) -> str:
        gamma = self.gamma_map.get(operation_id)
        if gamma is None:
            raise KeyError(
                f"operationId {operation_id!r} not found in spec gamma map. "
                "Is x-gamma present on this operation?"
            )
        path = gamma.path
        if path_params:
            for k, v in path_params.items():
                path = re.sub(r"\{" + re.escape(k) + r"\}", str(v), path)
        return path

    # ------------------------------------------------------------------
    # State management helpers
    # ------------------------------------------------------------------

    def set_state(self, resource_key: str, state: str) -> None:
        """Manually set a resource's known state (e.g. seeded from server response)."""
        self.resource_states[resource_key] = state

    def get_state(self, resource_key: str) -> str | None:
        return self.resource_states.get(resource_key)

    def reset(self) -> None:
        """Clear session history — start a fresh Γ-tracking context."""
        self.called.clear()
        self.resource_states.clear()
        self.violations.clear()
