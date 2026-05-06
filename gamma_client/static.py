"""
Static Γ checker — verify API contracts without running anything.

Given an OpenAPI spec with x-gamma, this module lets you:

1. Analyze the grammar for internal consistency:
   - All states referenced in requires_state/produces_state are declared
   - All operations in requires_prior/forbidden_after are known
   - The requires_prior DAG is acyclic (no operation can be permanently blocked)
   - Every state has at least one operation that can reach it

2. Check a sequence of operations against the grammar:
   - Simulate session execution (no HTTP, no server)
   - Report every violation with WHY — same GammaError structure as the live client

3. Query which operations are currently admissible given session state

4. Enumerate valid paths through the grammar (for test generation)

Usage::

    from gamma_client.spec import load_spec_file, parse_spec
    from gamma_client.static import GammaChecker

    gamma_map = parse_spec(load_spec_file("openapi.json"))
    checker = GammaChecker(gamma_map)

    # Check grammar consistency
    issues = checker.analyze()
    assert issues == [], issues

    # Check a workflow without running the server
    violations = checker.check_sequence(
        ["createItem", "publishItem", "archiveItem"],
        resource_states={"item:1": "draft"},
        resource_key="item:1",
    )
    assert violations == []

    # What can be called right now?
    admissible = checker.valid_next(called=set(), resource_states={})
    assert "createItem" in admissible
    assert "checkout" not in admissible  # requires_prior addToCart
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from gamma_client.errors import (
    ForbiddenAfterViolation,
    GammaViolation,
    RequiresPriorViolation,
    RequiresStateViolation,
)
from gamma_client.spec import OperationGamma


# ---------------------------------------------------------------------------
# Grammar issues — problems with the spec itself, not with a sequence
# ---------------------------------------------------------------------------

@dataclass
class GrammarIssue:
    """An inconsistency in the declared grammar."""
    severity: str          # "error" | "warning"
    code: str              # machine-readable issue code
    description: str       # WHY this is a problem
    operation_id: str | None = None
    detail: Any = None

    def __str__(self) -> str:
        loc = f"[{self.operation_id}] " if self.operation_id else ""
        return f"{self.severity.upper()} {self.code}: {loc}{self.description}"


# ---------------------------------------------------------------------------
# GammaChecker
# ---------------------------------------------------------------------------

class GammaChecker:
    """
    Static Γ checker. Takes a gamma_map from parse_spec() and provides
    analysis and simulation without any HTTP calls or running server.
    """

    def __init__(self, gamma_map: dict[str, OperationGamma]) -> None:
        self.gamma_map = gamma_map
        self._all_states = self._collect_all_states()
        self._all_ops = set(gamma_map)

    # ------------------------------------------------------------------
    # 1. Grammar analysis
    # ------------------------------------------------------------------

    def analyze(self) -> list[GrammarIssue]:
        """
        Check the grammar for internal consistency.

        Returns a list of GrammarIssue. An empty list means the grammar
        is self-consistent.
        """
        issues: list[GrammarIssue] = []
        issues.extend(self._check_state_references())
        issues.extend(self._check_operation_references())
        issues.extend(self._check_requires_prior_dag())
        issues.extend(self._check_state_reachability())
        return issues

    def _collect_all_states(self) -> set[str]:
        states: set[str] = set()
        for g in self.gamma_map.values():
            if g.states:
                states.update(g.states)
            if g.requires_state:
                states.update(g.requires_state)
            if g.produces_state:
                states.add(g.produces_state)
        return states

    def _check_state_references(self) -> list[GrammarIssue]:
        """requires_state and produces_state must reference declared states."""
        issues = []
        declared = {s for g in self.gamma_map.values() if g.states for s in g.states}
        if not declared:
            return []  # no state machine declared — nothing to check

        for op_id, g in self.gamma_map.items():
            for state in (g.requires_state or []):
                if state not in declared:
                    issues.append(GrammarIssue(
                        severity="error",
                        code="unknown_state_ref",
                        description=f"requires_state references {state!r} which is not in any declared states list",
                        operation_id=op_id,
                        detail={"state": state, "declared": sorted(declared)},
                    ))
            if g.produces_state and g.produces_state not in declared:
                issues.append(GrammarIssue(
                    severity="error",
                    code="unknown_state_ref",
                    description=f"produces_state {g.produces_state!r} is not in any declared states list",
                    operation_id=op_id,
                    detail={"state": g.produces_state, "declared": sorted(declared)},
                ))
        return issues

    def _check_operation_references(self) -> list[GrammarIssue]:
        """requires_prior and forbidden_after must reference known operationIds."""
        issues = []
        for op_id, g in self.gamma_map.items():
            for ref in (g.requires_prior or []):
                if ref not in self._all_ops:
                    issues.append(GrammarIssue(
                        severity="error",
                        code="unknown_op_ref",
                        description=f"requires_prior references {ref!r} which is not a known operationId",
                        operation_id=op_id,
                        detail={"referenced": ref},
                    ))
            for ref in (g.forbidden_after or []):
                if ref not in self._all_ops:
                    issues.append(GrammarIssue(
                        severity="error",
                        code="unknown_op_ref",
                        description=f"forbidden_after references {ref!r} which is not a known operationId",
                        operation_id=op_id,
                        detail={"referenced": ref},
                    ))
        return issues

    def _check_requires_prior_dag(self) -> list[GrammarIssue]:
        """
        requires_prior must form a DAG — a cycle makes some operations
        permanently inadmissible (A requires B requires A).
        """
        issues = []
        # Build adjacency: op → set of ops it requires
        edges: dict[str, set[str]] = {
            op_id: set(g.requires_prior or [])
            for op_id, g in self.gamma_map.items()
        }

        # DFS cycle detection
        WHITE, GRAY, BLACK = 0, 1, 2
        color = {op: WHITE for op in edges}

        def dfs(node: str, path: list[str]) -> list[str] | None:
            color[node] = GRAY
            for neighbour in edges.get(node, set()):
                if neighbour not in color:
                    continue
                if color[neighbour] == GRAY:
                    cycle = path[path.index(neighbour):] + [neighbour]
                    return cycle
                if color[neighbour] == WHITE:
                    result = dfs(neighbour, path + [neighbour])
                    if result:
                        return result
            color[node] = BLACK
            return None

        for op in list(edges):
            if color[op] == WHITE:
                cycle = dfs(op, [op])
                if cycle:
                    cycle_str = " → ".join(cycle)
                    issues.append(GrammarIssue(
                        severity="error",
                        code="requires_prior_cycle",
                        description=(
                            f"requires_prior cycle detected: {cycle_str}. "
                            f"These operations can never all be called."
                        ),
                        detail={"cycle": cycle},
                    ))
                    break  # one cycle report is enough

        return issues

    def _check_state_reachability(self) -> list[GrammarIssue]:
        """
        Warn if a declared state is not reachable by any produces_state.
        States that can only be set externally (initial states) are fine
        — this is a warning, not an error.
        """
        issues = []
        declared = {s for g in self.gamma_map.values() if g.states for s in g.states}
        if not declared:
            return []

        produced = {g.produces_state for g in self.gamma_map.values() if g.produces_state}
        unreachable = declared - produced

        for state in sorted(unreachable):
            issues.append(GrammarIssue(
                severity="warning",
                code="state_not_produced",
                description=(
                    f"state {state!r} is declared but no operation produces_state it. "
                    f"It can only be set externally (e.g. as an initial state)."
                ),
                detail={"state": state},
            ))

        return issues

    # ------------------------------------------------------------------
    # 2. Sequence checking
    # ------------------------------------------------------------------

    def check_sequence(
        self,
        operations: list[str],
        *,
        resource_states: dict[str, str] | None = None,
        resource_key: str | None = None,
    ) -> list[GammaViolation]:
        """
        Simulate executing a sequence of operationIds against the grammar.

        No HTTP calls. No server. Pure grammar simulation.

        Parameters
        ----------
        operations:
            Ordered list of operationIds to simulate.
        resource_states:
            Initial resource states keyed by resource_key.
        resource_key:
            Default resource key for requires_state / produces_state checks.

        Returns a list of GammaViolation — one per violation found.
        An empty list means the sequence is fully admissible.
        """
        called: set[str] = set()
        states: dict[str, str] = dict(resource_states or {})
        violations: list[GammaViolation] = []

        for op_id in operations:
            g = self.gamma_map.get(op_id)
            if g is None:
                # No x-gamma on this operation — no constraints, always admissible.
                called.add(op_id)
                continue

            rkey = resource_key
            v = self._check_step(op_id, g, called, states, rkey)
            violations.extend(v)

            # Advance session state even if there were violations (to catch all of them)
            called.add(op_id)
            if g.produces_state and rkey is not None:
                states[rkey] = g.produces_state

        return violations

    def _check_step(
        self,
        op_id: str,
        g: OperationGamma,
        called: set[str],
        states: dict[str, str],
        resource_key: str | None,
    ) -> list[GammaViolation]:
        violations = []

        if g.requires_prior:
            missing = [op for op in g.requires_prior if op not in called]
            if missing:
                violations.append(RequiresPriorViolation(op_id, missing, g))

        if g.forbidden_after:
            for blocker in g.forbidden_after:
                if blocker in called:
                    violations.append(ForbiddenAfterViolation(op_id, blocker, g))
                    break

        if g.requires_state and resource_key is not None:
            current = states.get(resource_key)
            if current not in g.requires_state:
                violations.append(RequiresStateViolation(
                    op_id, resource_key, current, g.requires_state, g
                ))

        return violations

    # ------------------------------------------------------------------
    # 3. Admissibility query
    # ------------------------------------------------------------------

    def valid_next(
        self,
        called: set[str],
        resource_states: dict[str, str] | None = None,
        resource_key: str | None = None,
    ) -> list[str]:
        """
        Return the operationIds that are currently admissible given the
        session state — no violations would occur if called next.

        This is the grammar's answer to "what can I do now?"
        """
        admissible = []
        states = resource_states or {}

        for op_id, g in self.gamma_map.items():
            violations = self._check_step(op_id, g, called, states, resource_key)
            if not violations:
                admissible.append(op_id)

        return sorted(admissible)

    # ------------------------------------------------------------------
    # 4. Path enumeration (test generation)
    # ------------------------------------------------------------------

    def enumerate_paths(
        self,
        *,
        max_length: int = 6,
        resource_key: str | None = None,
        initial_state: str | None = None,
    ) -> list[list[str]]:
        """
        Enumerate all valid operation sequences up to max_length.

        Useful for generating test cases: every path returned is a
        sequence that the grammar admits. Test it against a real server
        to verify the server also admits it.

        Parameters
        ----------
        max_length:
            Maximum sequence length. Grows exponentially — keep small.
        resource_key:
            Resource key for state tracking during path search.
        initial_state:
            Starting resource state (if any).
        """
        paths: list[list[str]] = []
        initial_states = {resource_key: initial_state} if resource_key and initial_state else {}

        self._dfs_paths(
            path=[],
            called=set(),
            states=initial_states,
            resource_key=resource_key,
            max_length=max_length,
            paths=paths,
        )

        return paths

    def _dfs_paths(
        self,
        path: list[str],
        called: set[str],
        states: dict[str, str],
        resource_key: str | None,
        max_length: int,
        paths: list[list[str]],
    ) -> None:
        if path:
            paths.append(list(path))

        if len(path) >= max_length:
            return

        for op_id, g in self.gamma_map.items():
            violations = self._check_step(op_id, g, called, states, resource_key)
            if violations:
                continue

            new_states = dict(states)
            if g.produces_state and resource_key is not None:
                new_states[resource_key] = g.produces_state

            # Avoid infinite loops: don't revisit an op in the same path
            # unless it's explicitly not forbidden_after itself
            if op_id in called and g.forbidden_after and op_id in (g.forbidden_after or []):
                continue

            self._dfs_paths(
                path=path + [op_id],
                called=called | {op_id},
                states=new_states,
                resource_key=resource_key,
                max_length=max_length,
                paths=paths,
            )
