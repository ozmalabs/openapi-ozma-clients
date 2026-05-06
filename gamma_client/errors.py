"""
Exceptions raised when an operation violates the admissibility grammar.

Every error explains WHY the operation was inadmissible — the full graph state
at the point of failure: what was attempted, what the grammar required,
and what the actual state was. "Failed" is not a useful error message.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from gamma_client.spec import OperationGamma


class GammaViolation(Exception):
    """
    An operation was called that violates Γ.

    Attributes
    ----------
    operation_id:
        The operationId that was attempted.
    reason:
        Human-readable explanation of WHY it was inadmissible.
    gamma:
        The full OperationGamma for the attempted operation, so callers
        can inspect the complete grammar that was violated.
    """

    def __init__(
        self,
        operation_id: str,
        reason: str,
        gamma: "OperationGamma | None" = None,
    ) -> None:
        self.operation_id = operation_id
        self.reason = reason
        self.gamma = gamma
        super().__init__(f"[{operation_id}] {reason}")


class RequiresPriorViolation(GammaViolation):
    """
    Γ violation: requires_prior constraint not satisfied.

    The operation declares that certain operations must have been called
    in this session before it is admissible. One or more of them have not.

    Attributes
    ----------
    missing:
        The operationIds that were required but not yet called.
    """

    def __init__(
        self,
        operation_id: str,
        missing: list[str],
        gamma: "OperationGamma | None" = None,
    ) -> None:
        self.missing = missing
        required = ", ".join(repr(m) for m in missing)
        reason = (
            f"operation is inadmissible: it requires prior calls to "
            f"{required}, but {'that has' if len(missing) == 1 else 'those have'} "
            f"not been called in this session"
        )
        super().__init__(operation_id, reason, gamma)


class ForbiddenAfterViolation(GammaViolation):
    """
    Γ violation: forbidden_after constraint triggered.

    The operation is inadmissible because an operation in its
    forbidden_after list was already called in this session.

    Attributes
    ----------
    blocked_by:
        The operationId whose prior call made this operation inadmissible.
    """

    def __init__(
        self,
        operation_id: str,
        blocked_by: str,
        gamma: "OperationGamma | None" = None,
    ) -> None:
        self.blocked_by = blocked_by
        reason = (
            f"operation is forbidden: {blocked_by!r} was already called in this "
            f"session, and the grammar declares {operation_id!r} inadmissible after it"
        )
        super().__init__(operation_id, reason, gamma)


class RequiresStateViolation(GammaViolation):
    """
    Γ violation: resource is not in a required state.

    The operation declares it is only admissible when the resource is
    in one of the listed states. The tracked state does not satisfy this.

    Attributes
    ----------
    resource_key:
        The key used to track this resource's state in the session.
    current_state:
        The resource's current tracked state (None if not yet tracked).
    required_states:
        The states in which this operation would have been admissible.
    """

    def __init__(
        self,
        operation_id: str,
        resource_key: str,
        current_state: str | None,
        required_states: list[str],
        gamma: "OperationGamma | None" = None,
    ) -> None:
        self.resource_key = resource_key
        self.current_state = current_state
        self.required_states = required_states

        current_desc = repr(current_state) if current_state is not None else "unknown (not yet tracked)"
        required_desc = " or ".join(repr(s) for s in required_states)
        reason = (
            f"resource {resource_key!r} is in state {current_desc}, "
            f"but {operation_id!r} is only admissible when the resource is in state {required_desc}. "
            f"Call the appropriate transition first."
        )
        super().__init__(operation_id, reason, gamma)
