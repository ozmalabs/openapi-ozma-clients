"""
Parse an OpenAPI spec and extract x-gamma from each operation.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class Precondition:
    type: str           # "dependency" | "security" | "explicit"
    name: str
    description: str | None = None
    scopes: list[str] = field(default_factory=list)


@dataclass
class Postcondition:
    description: str
    effect: str | None = None
    produces_state: str | None = None


@dataclass
class Transition:
    from_state: str
    to_state: str


@dataclass
class OperationGamma:
    operation_id: str
    method: str
    path: str
    preconditions: list[Precondition] = field(default_factory=list)
    postconditions: list[Postcondition] = field(default_factory=list)
    states: list[str] | None = None
    transitions: list[Transition] | None = None
    requires_state: list[str] | None = None
    produces_state: str | None = None
    requires_prior: list[str] | None = None
    forbidden_after: list[str] | None = None

    def has_constraints(self) -> bool:
        return bool(
            self.preconditions
            or self.postconditions
            or self.requires_state
            or self.requires_prior
            or self.forbidden_after
        )


def _parse_gamma_block(raw: dict[str, Any], operation_id: str, method: str, path: str) -> OperationGamma:
    g = OperationGamma(operation_id=operation_id, method=method, path=path)

    for p in raw.get("preconditions", []):
        g.preconditions.append(Precondition(
            type=p.get("type", "explicit"),
            name=p["name"],
            description=p.get("description"),
            scopes=p.get("scopes", []),
        ))

    for p in raw.get("postconditions", []):
        g.postconditions.append(Postcondition(
            description=p["description"],
            effect=p.get("effect"),
            produces_state=p.get("produces_state"),
        ))

    if "states" in raw:
        g.states = raw["states"]

    if "transitions" in raw:
        g.transitions = [
            Transition(from_state=t["from"], to_state=t["to"])
            for t in raw["transitions"]
        ]

    g.requires_state = raw.get("requires_state")
    g.produces_state = raw.get("produces_state")
    g.requires_prior = raw.get("requires_prior")
    g.forbidden_after = raw.get("forbidden_after")

    return g


def parse_spec(spec: dict[str, Any]) -> dict[str, OperationGamma]:
    """
    Walk an OpenAPI spec dict and return a map of operationId → OperationGamma.

    Only operations with x-gamma blocks are included.
    """
    result: dict[str, OperationGamma] = {}
    paths = spec.get("paths", {})
    http_methods = {"get", "post", "put", "patch", "delete", "head", "options", "trace"}

    for path, path_item in paths.items():
        for method, operation in path_item.items():
            if method not in http_methods:
                continue
            if not isinstance(operation, dict):
                continue
            gamma_raw = operation.get("x-gamma")
            if not gamma_raw:
                continue
            operation_id = operation.get("operationId", f"{method}_{path}")
            g = _parse_gamma_block(gamma_raw, operation_id, method, path)
            result[operation_id] = g

    return result


def load_spec_file(path: str | Path) -> dict[str, Any]:
    """Load a JSON or YAML OpenAPI spec from disk."""
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    if p.suffix in (".yaml", ".yml"):
        try:
            import yaml  # type: ignore[import-untyped]
            return yaml.safe_load(text)  # type: ignore[no-any-return]
        except ImportError as exc:
            raise ImportError("pip install pyyaml for YAML spec support") from exc
    return json.loads(text)  # type: ignore[no-any-return]


def load_spec_url(url: str) -> dict[str, Any]:
    """Fetch and parse an OpenAPI spec from a URL."""
    try:
        import httpx
    except ImportError as exc:
        raise ImportError("pip install httpx for URL spec loading") from exc
    resp = httpx.get(url, follow_redirects=True, timeout=30)
    resp.raise_for_status()
    if url.endswith((".yaml", ".yml")):
        try:
            import yaml  # type: ignore[import-untyped]
            return yaml.safe_load(resp.text)  # type: ignore[no-any-return]
        except ImportError as exc:
            raise ImportError("pip install pyyaml for YAML spec support") from exc
    return resp.json()  # type: ignore[no-any-return]
