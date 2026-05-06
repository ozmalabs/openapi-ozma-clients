"""
GammaMock — a spec-grounded mock HTTP transport.

Traditional mocks lie. They encode your assumptions about the API,
not the actual contract. They go stale. They pass when the real thing
would fail.

GammaMock is instantiated from the OpenAPI spec. It enforces the
declared Γ (admissibility grammar) before generating responses, and
generates response bodies from the OpenAPI response schemas. It cannot
drift from the spec because it IS the spec executing.

Use it as an httpx transport — drop-in for ASGITransport or any real
HTTP backend. The same test function works against both.

Usage::

    from gamma_client.mock import GammaMock

    # From a FastAPI app (no server needed)
    mock = GammaMock.from_app(app)

    # From a spec file (no app needed)
    mock = GammaMock.from_spec(load_spec_file("openapi.json"))

    async with httpx.AsyncClient(transport=mock, base_url="http://mock") as c:
        r = await c.post("/items", json={"title": "hello"})
        assert r.status_code == 200
        item_id = r.json()["id"]

        r = await c.post(f"/items/{item_id}/archive")
        assert r.status_code == 409          # grammar enforced
        assert r.json()["violation"] == "state_violation"

        r = await c.post(f"/items/{item_id}/publish")
        assert r.status_code == 200
        r = await c.post(f"/items/{item_id}/archive")
        assert r.status_code == 200          # now admissible
"""
from __future__ import annotations

import json
import re
from typing import Any

import httpx

from gamma_client.errors import (
    ForbiddenAfterViolation,
    GammaError,
    GammaViolation,
    RequiresPriorViolation,
    RequiresStateViolation,
)
from gamma_client.spec import OperationGamma, parse_spec
from gamma_client.static import GammaChecker

_HTTP_METHODS = {"get", "post", "put", "patch", "delete", "head", "options", "trace"}


# ---------------------------------------------------------------------------
# Schema-based response generator
# ---------------------------------------------------------------------------

class _SchemaGenerator:
    """
    Generate plausible values from a JSON Schema fragment.

    Not exhaustive — handles the common shapes that appear in REST APIs:
    objects with typed properties, arrays, string enums, date-times.
    Synthesises state-consistent values when given hints.
    """

    def __init__(self, spec: dict) -> None:
        self._spec = spec
        self._schemas: dict[str, Any] = (
            spec.get("components", {}).get("schemas", {})
        )
        self._counter = 0

    def _next_id(self) -> int:
        self._counter += 1
        return self._counter

    def _resolve_ref(self, ref: str) -> dict:
        parts = ref.lstrip("#/").split("/")
        node: Any = self._spec
        for part in parts:
            node = node[part]
        return node  # type: ignore[return-value]

    def generate(self, schema: dict, hints: dict | None = None) -> Any:
        if not schema:
            return None

        if "$ref" in schema:
            schema = self._resolve_ref(schema["$ref"])

        if "allOf" in schema:
            result: dict = {}
            for sub in schema["allOf"]:
                generated = self.generate(sub, hints=hints)
                if isinstance(generated, dict):
                    result.update(generated)
            return result

        if "anyOf" in schema or "oneOf" in schema:
            options = schema.get("anyOf") or schema.get("oneOf") or []
            for option in options:
                # Skip null
                if option.get("type") != "null":
                    return self.generate(option, hints=hints)
            return None

        type_ = schema.get("type")

        if type_ == "object" or "properties" in schema:
            return self._gen_object(schema, hints)
        if type_ == "array":
            items = schema.get("items")
            return [self.generate(items, hints=hints)] if items else []
        if type_ == "string":
            return self._gen_string(schema, hints)
        if type_ == "integer":
            h = hints or {}
            return h.get("_id", self._next_id())
        if type_ == "number":
            return 1.0
        if type_ == "boolean":
            return True
        if "enum" in schema:
            return schema["enum"][0]
        if type_ == "null":
            return None

        return None

    def _gen_object(self, schema: dict, hints: dict | None) -> dict[str, Any]:
        result: dict[str, Any] = {}
        h = hints or {}
        for prop, prop_schema in schema.get("properties", {}).items():
            if prop in ("id",):
                result[prop] = h.get("_id", self._next_id())
            elif prop in ("status", "state", "phase", "lifecycle"):
                result[prop] = h.get("_state") or self.generate(prop_schema, hints={**h, "_field": prop})
            elif prop in ("title", "name") and prop in h:
                result[prop] = h[prop]
            elif prop in ("body", "content", "text") and prop in h:
                result[prop] = h[prop]
            else:
                result[prop] = self.generate(prop_schema, hints={**h, "_field": prop})
        return result

    def _gen_string(self, schema: dict, hints: dict | None) -> str:
        if "enum" in schema:
            state_hint = (hints or {}).get("_state")
            if state_hint and state_hint in schema["enum"]:
                return state_hint
            return schema["enum"][0]
        fmt = schema.get("format", "")
        field = (hints or {}).get("_field", "")
        if fmt == "date-time" or field.endswith("_at") or field.endswith("_date"):
            return "2024-01-01T00:00:00Z"
        if fmt == "email" or "email" in field:
            return "mock@example.com"
        if fmt == "uri" or "url" in field:
            return "https://example.com"
        return (hints or {}).get(field, "")


# ---------------------------------------------------------------------------
# Route table
# ---------------------------------------------------------------------------

def _build_route_table(
    spec: dict,
) -> list[tuple[str, re.Pattern[str], str, str, dict]]:
    """
    Parse the OpenAPI paths into a matchable route table.

    Each entry: (HTTP_METHOD, compiled_regex, operationId, path_template, operation_dict)
    Path parameters {id} become named capture groups (?P<id>[^/]+).
    """
    routes = []
    for path, path_item in spec.get("paths", {}).items():
        pattern = re.sub(r"\{(\w+)\}", r"(?P<\1>[^/]+)", path)
        regex = re.compile(f"^{pattern}$")
        for method, operation in path_item.items():
            if method not in _HTTP_METHODS:
                continue
            if not isinstance(operation, dict):
                continue
            op_id = operation.get("operationId")
            if op_id:
                routes.append((method.upper(), regex, op_id, path, operation))
    return routes


def _param_resource_key(path_template: str, path_params: dict[str, str]) -> str | None:
    """
    Derive the resource key from path params, stripping trailing action segments.

    /items/{id}/publish  with id=1  →  /items/1
    /items/{id}          with id=1  →  /items/1
    /items               (no params) →  None
    """
    if not path_params:
        return None
    segments = path_template.split("/")
    last_param_idx = 0
    for i, seg in enumerate(segments):
        if seg.startswith("{") and seg.endswith("}"):
            last_param_idx = i
    resource_template = "/".join(segments[: last_param_idx + 1])
    for name, value in path_params.items():
        resource_template = resource_template.replace(f"{{{name}}}", value)
    return resource_template


def _success_schema(operation: dict) -> dict | None:
    """Find the JSON schema for the first 2xx response."""
    for code in ("200", "201", "202"):
        resp = operation.get("responses", {}).get(code, {})
        schema = (
            resp.get("content", {})
            .get("application/json", {})
            .get("schema")
        )
        if schema:
            return schema
    return None


def _success_status(operation: dict) -> int:
    for code in (200, 201, 202):
        if str(code) in operation.get("responses", {}):
            return code
    return 200


def _violation_to_error(v: GammaViolation) -> GammaError:
    if isinstance(v, RequiresStateViolation):
        return GammaError.wrong_state(
            operation=v.operation_id,
            resource=v.resource_key,
            current=str(v.current_state),
            required=v.required_states,
        )
    if isinstance(v, RequiresPriorViolation):
        return GammaError.requires_prior(operation=v.operation_id, missing=v.missing)
    if isinstance(v, ForbiddenAfterViolation):
        return GammaError.forbidden_after(operation=v.operation_id, blocked_by=v.blocked_by)
    return GammaError(violation="precondition_failed", description=v.reason)


# ---------------------------------------------------------------------------
# GammaMock
# ---------------------------------------------------------------------------

class GammaMock(httpx.AsyncBaseTransport):
    """
    Spec-grounded mock HTTP transport for httpx.

    Instantiate from an OpenAPI spec (with x-gamma) and use as an
    httpx transport. Enforces Γ on every request; generates schema-valid
    response bodies; maintains resource state across calls.

    The same test that runs against the real server runs against this mock.
    """

    def __init__(
        self,
        spec: dict,
        gamma_map: dict[str, OperationGamma],
    ) -> None:
        self._spec = spec
        self._gamma_map = gamma_map
        self._checker = GammaChecker(gamma_map)
        self._routes = _build_route_table(spec)
        self._gen = _SchemaGenerator(spec)

        # Session state
        self._called: set[str] = set()
        self._resource_states: dict[str, str] = {}   # path → state string
        self._resources: dict[str, dict] = {}         # path → stored body

    # ------------------------------------------------------------------
    # Constructors
    # ------------------------------------------------------------------

    @classmethod
    def from_spec(cls, spec: dict) -> "GammaMock":
        """Build from a parsed OpenAPI spec dict."""
        return cls(spec, parse_spec(spec))

    @classmethod
    def from_app(cls, app: Any) -> "GammaMock":
        """Build from a FastAPI app — no server needed."""
        spec = app.openapi()
        return cls.from_spec(spec)

    # ------------------------------------------------------------------
    # httpx transport
    # ------------------------------------------------------------------

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        route = self._match(request)
        if route is None:
            return httpx.Response(404, json={"detail": "route not found in spec"})

        op_id, path_params, path_template, operation = route
        method = request.method.upper()
        gamma = self._gamma_map.get(op_id)

        # Resource key for Γ checking — derived from path params only.
        # For /items/{id}/publish with id=1 → /items/1.
        # For /items (creation POST) → None (no state to check yet).
        check_key = _param_resource_key(path_template, path_params)

        # Γ enforcement
        if gamma is not None:
            violations = self._checker._check_step(
                op_id, gamma, self._called, self._resource_states, check_key
            )
            if violations:
                err = _violation_to_error(violations[0])
                return httpx.Response(
                    err.status_code(),
                    json=err.model_dump(exclude_none=True),
                )

        # Parse request body for hints
        hints: dict[str, Any] = dict(path_params)
        if request.content:
            try:
                hints.update(json.loads(request.content))
            except Exception:
                pass

        # Resolve integer path param IDs
        for k, v in path_params.items():
            if k in ("id", "item_id", "user_id"):
                try:
                    hints["_id"] = int(v)
                except ValueError:
                    pass

        # If produces_state is declared, hint the generator so state
        # fields in the response body reflect the new state
        if gamma and gamma.produces_state:
            hints["_state"] = gamma.produces_state

        # Generate or retrieve response body; GET lookup uses check_key
        body = self._build_body(request, op_id, operation, check_key, hints)

        # Final resource key — creation ops (no path params) derive key from body id
        if check_key is not None:
            resource_key = check_key
        elif method == "POST" and isinstance(body, dict) and "id" in body:
            resource_key = f"{path_template.rstrip('/')}/{body['id']}"
        else:
            resource_key = request.url.path

        # Advance session state
        self._called.add(op_id)
        if gamma and gamma.produces_state:
            self._resource_states[resource_key] = gamma.produces_state
            if resource_key in self._resources and isinstance(body, dict):
                # Patch the state field in the stored resource
                for field in ("status", "state", "phase"):
                    if field in self._resources[resource_key]:
                        self._resources[resource_key][field] = gamma.produces_state
                        body = dict(self._resources[resource_key])
                        break

        # Store resource
        if isinstance(body, dict) and body:
            self._resources[resource_key] = dict(body)

        return httpx.Response(
            _success_status(operation),
            json=body,
            headers={"content-type": "application/json"},
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _match(
        self, request: httpx.Request
    ) -> tuple[str, dict[str, str], str, dict] | None:
        method = request.method.upper()
        path = request.url.path
        for route_method, pattern, op_id, path_template, operation in self._routes:
            if route_method != method:
                continue
            m = pattern.match(path)
            if m:
                return op_id, m.groupdict(), path_template, operation
        return None

    def _build_body(
        self,
        request: httpx.Request,
        op_id: str,
        operation: dict,
        resource_key: str,
        hints: dict,
    ) -> Any:
        method = request.method.upper()

        # GET — return stored resource if available
        if method == "GET" and resource_key in self._resources:
            return dict(self._resources[resource_key])

        # DELETE — return minimal confirmation
        if method == "DELETE":
            id_val = hints.get("_id") or hints.get("id")
            return {"deleted": id_val} if id_val is not None else {}

        schema = _success_schema(operation)
        if schema is None:
            return {}

        return self._gen.generate(schema, hints=hints)

    # ------------------------------------------------------------------
    # Inspection / reset
    # ------------------------------------------------------------------

    def called(self) -> set[str]:
        """operationIds called so far in this session."""
        return set(self._called)

    def resource_state(self, path: str) -> str | None:
        """Current tracked state for a resource path."""
        return self._resource_states.get(path)

    def stored(self, path: str) -> dict | None:
        """Last stored response body for a resource path."""
        return self._resources.get(path)

    def reset(self) -> None:
        """Clear all session state — start fresh."""
        self._called.clear()
        self._resource_states.clear()
        self._resources.clear()
        self._gen._counter = 0
