"""
GammaPyMock — a spec-grounded mock for any Python class or module.

Traditional mocks (MagicMock, patch) return MagicMock objects for every
call. They don't know what a valid return value looks like, and they don't
know whether the call was even admissible given what happened before it.
They are lies that happen to be convenient.

GammaPyMock wraps any Python class and:

  1. Generates type-valid return values from the method's return annotation.
  2. Enforces declared Γ — the same grammar engine as GammaMock (HTTP).
  3. Infers a baseline grammar from method signatures and docstrings,
     so common lifecycle patterns (open/close, begin/commit) are caught
     without any manual annotation.

Usage::

    from gamma_client.py_mock import GammaPyMock

    # Wrap any class — grammar inferred from annotations + docstrings
    MockSession = GammaPyMock.from_class(Session)
    session = MockSession()
    session.execute("SELECT 1")   # GammaViolation: begin() not called yet
    session.begin()
    result = session.execute("SELECT 1")   # returns list[Row] — type-valid
    assert isinstance(result, list)
    session.commit()

    # Declare explicit grammar — overrides inference
    grammar = infer_grammar(Session) | {
        "execute": OperationGamma(
            operation_id="execute",
            method="call",
            path="Session.execute",
            requires_prior=["begin"],
        )
    }
    MockSession = GammaPyMock.from_class(Session, grammar=grammar)

    # From any instance
    mock_client = GammaPyMock.from_instance(stripe.Charge, grammar=charge_grammar)
"""
from __future__ import annotations

import inspect
import re
from typing import Any, get_type_hints

from gamma_client.errors import GammaViolation
from gamma_client.spec import OperationGamma
from gamma_client.static import GammaChecker
from gamma_client.type_gen import TypeGenerator


# ---------------------------------------------------------------------------
# Grammar inference from method signatures and docstrings
# ---------------------------------------------------------------------------

# Lifecycle pairs: calling the second without the first is a grammar violation.
# These are the patterns that appear in almost every I/O library.
_LIFECYCLE_PAIRS: list[tuple[str, str]] = [
    ("open", "read"),
    ("open", "write"),
    ("open", "seek"),
    ("open", "flush"),
    ("open", "close"),
    ("connect", "begin"),
    ("connect", "send"),
    ("connect", "recv"),
    ("connect", "read"),
    ("connect", "write"),
    ("connect", "execute"),
    ("connect", "close"),
    ("begin", "execute"),
    ("begin", "commit"),
    ("begin", "rollback"),
    ("begin", "fetchone"),
    ("begin", "fetchall"),
    ("start", "stop"),
    ("start", "pause"),
    ("acquire", "release"),
    ("lock", "unlock"),
    ("subscribe", "poll"),
    ("subscribe", "consume"),
    ("subscribe", "commit"),
    ("publish", "flush"),
    ("login", "logout"),
    ("authenticate", "request"),
]

# Operations that should not be called again after themselves.
# Omits commit/rollback — those repeat legitimately across transactions.
_SELF_FORBIDDING: set[str] = {
    "close", "disconnect", "shutdown", "destroy", "delete", "terminate", "logout",
}

# Keywords in docstrings that hint at requires_prior
_REQUIRES_PRIOR_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"(?:must|should)\s+(?:first\s+)?call\s+[\`'\"]?(\w+)[\`'\"]?", re.I),
    re.compile(r"requires?\s+(?:a\s+)?(?:prior\s+)?(?:call\s+to\s+)?[\`'\"]?(\w+)[\`'\"]?", re.I),
    re.compile(r"(?:only\s+)?(?:after|following)\s+[\`'\"]?(\w+)[\`'\"]?", re.I),
    re.compile(r"[\`'\"]?(\w+)[\`'\"]?\s+must\s+be\s+called\s+(?:first|before)", re.I),
]


def infer_grammar(klass: type) -> dict[str, OperationGamma]:
    """
    Infer a baseline Γ from a class's method signatures and docstrings.

    Returns a grammar_map keyed by method name. Operations without any
    inferred constraints are omitted — they are always admissible.

    This is a conservative inference: it only flags patterns that are
    almost certainly intentional constraints (lifecycle pairs, self-forbidding
    ops, explicit docstring keywords). It never blocks something that might
    be valid.
    """
    methods = _public_methods(klass)
    method_names = set(methods)
    grammar: dict[str, OperationGamma] = {}

    # Build requires_prior from lifecycle pairs
    requires_prior_map: dict[str, list[str]] = {}
    for prerequisite, dependent in _LIFECYCLE_PAIRS:
        if prerequisite in method_names and dependent in method_names:
            requires_prior_map.setdefault(dependent, [])
            if prerequisite not in requires_prior_map[dependent]:
                requires_prior_map[dependent].append(prerequisite)

    # Build requires_prior from docstrings
    for name, method in methods.items():
        doc = inspect.getdoc(method) or ""
        for pattern in _REQUIRES_PRIOR_PATTERNS:
            for m in pattern.finditer(doc):
                candidate = m.group(1)
                if candidate in method_names and candidate != name:
                    requires_prior_map.setdefault(name, [])
                    if candidate not in requires_prior_map[name]:
                        requires_prior_map[name].append(candidate)

    # Build forbidden_after for self-forbidding ops
    forbidden_after_map: dict[str, list[str]] = {}
    for name in method_names:
        if name in _SELF_FORBIDDING:
            forbidden_after_map[name] = [name]

    # Assemble OperationGamma
    all_constrained = set(requires_prior_map) | set(forbidden_after_map)
    for name in all_constrained:
        grammar[name] = OperationGamma(
            operation_id=name,
            method="call",
            path=f"{klass.__qualname__}.{name}",
            requires_prior=requires_prior_map.get(name),
            forbidden_after=forbidden_after_map.get(name),
        )

    return grammar


def _public_methods(klass: type) -> dict[str, Any]:
    """Return all public non-dunder callable members of klass."""
    result: dict[str, Any] = {}
    for name in dir(klass):
        if name.startswith("_"):
            continue
        try:
            attr = getattr(klass, name)
        except AttributeError:
            continue
        if callable(attr):
            result[name] = attr
    return result


# ---------------------------------------------------------------------------
# Mock instance — wraps one instantiation
# ---------------------------------------------------------------------------

class _GammaMockInstance:
    """
    A proxy object that looks like an instance of the wrapped class.

    Every method call is intercepted: grammar is checked, then a type-valid
    return value is generated from the method's return annotation.
    """

    def __init__(
        self,
        klass: type,
        gamma_map: dict[str, OperationGamma],
        checker: GammaChecker,
        gen: TypeGenerator,
    ) -> None:
        object.__setattr__(self, "_klass", klass)
        object.__setattr__(self, "_gamma_map", gamma_map)
        object.__setattr__(self, "_checker", checker)
        object.__setattr__(self, "_gen", gen)
        object.__setattr__(self, "_called", set())
        object.__setattr__(self, "_resource_states", {})

    def __getattr__(self, name: str) -> Any:
        klass = object.__getattribute__(self, "_klass")
        attr = getattr(klass, name, _MISSING)
        if attr is _MISSING:
            raise AttributeError(f"{klass.__qualname__!r} has no attribute {name!r}")
        if not callable(attr):
            return attr

        gamma_map = object.__getattribute__(self, "_gamma_map")
        checker = object.__getattribute__(self, "_checker")
        gen = object.__getattribute__(self, "_gen")
        called = object.__getattribute__(self, "_called")
        resource_states = object.__getattribute__(self, "_resource_states")

        def _proxy(*args: Any, **kwargs: Any) -> Any:
            op_id = name
            gamma = gamma_map.get(op_id)

            if gamma is not None:
                violations = checker._check_step(
                    op_id, gamma, called, resource_states, None
                )
                if violations:
                    raise violations[0]

            called.add(op_id)
            if gamma and gamma.produces_state:
                resource_states["_default"] = gamma.produces_state

            return _generate_return(attr, gen, gamma)

        return _proxy

    # Inspection helpers (same interface as GammaMock)

    def called(self) -> set[str]:
        """Method names called so far."""
        return set(object.__getattribute__(self, "_called"))

    def reset(self) -> None:
        """Clear all session state."""
        object.__getattribute__(self, "_called").clear()
        object.__getattribute__(self, "_resource_states").clear()
        object.__getattribute__(self, "_gen")._counter = 0


_MISSING = object()


def _generate_return(method: Any, gen: TypeGenerator, gamma: OperationGamma | None) -> Any:
    """Generate a type-valid return value for a method call."""
    hints: dict = {}
    if gamma and gamma.produces_state:
        hints["_state"] = gamma.produces_state

    try:
        type_hints = get_type_hints(method)
    except Exception:
        type_hints = {}

    return_tp = type_hints.get("return")
    if return_tp is None:
        try:
            sig = inspect.signature(method)
            ann = sig.return_annotation
            if ann is not inspect.Parameter.empty:
                return_tp = ann
        except (ValueError, TypeError):
            pass

    if return_tp is None:
        return None

    return gen.generate(return_tp, hints)


# ---------------------------------------------------------------------------
# GammaPyMock — factory
# ---------------------------------------------------------------------------

class GammaPyMock:
    """
    Spec-grounded mock for any Python class.

    Wraps a class and returns a callable that produces mock instances.
    Each instance enforces Γ and generates type-valid return values.

    Unlike MagicMock, this mock:
    - Cannot return an invalid type (it reads the annotation and generates from it)
    - Cannot allow invalid call sequences (it enforces the declared grammar)
    - Documents WHY a call was rejected (GammaViolation with full context)
    """

    def __init__(
        self,
        klass: type,
        gamma_map: dict[str, OperationGamma],
    ) -> None:
        self._klass = klass
        self._gamma_map = gamma_map
        self._checker = GammaChecker(gamma_map)
        self._gen = TypeGenerator()

    def __call__(self, *args: Any, **kwargs: Any) -> _GammaMockInstance:
        """Instantiate a new mock instance (grammar starts fresh)."""
        return _GammaMockInstance(
            self._klass,
            self._gamma_map,
            GammaChecker(self._gamma_map),
            TypeGenerator(),
        )

    # ------------------------------------------------------------------
    # Constructors
    # ------------------------------------------------------------------

    @classmethod
    def from_class(
        cls,
        klass: type,
        *,
        grammar: dict[str, OperationGamma] | None = None,
    ) -> "GammaPyMock":
        """
        Wrap a class. Grammar is inferred from the class, then overridden
        by any explicitly provided entries.

        Parameters
        ----------
        klass:
            The class to mock. Its public method signatures and docstrings
            are used for inference.
        grammar:
            Explicit grammar entries. Merged on top of inferred grammar —
            explicit entries always win.
        """
        inferred = infer_grammar(klass)
        combined = inferred | (grammar or {})
        return cls(klass, combined)

    @classmethod
    def from_class_no_infer(
        cls,
        klass: type,
        grammar: dict[str, OperationGamma],
    ) -> "GammaPyMock":
        """Wrap a class with fully explicit grammar — no inference."""
        return cls(klass, grammar)

    # ------------------------------------------------------------------
    # Inspection
    # ------------------------------------------------------------------

    def grammar(self) -> dict[str, OperationGamma]:
        """The effective grammar for this mock."""
        return dict(self._gamma_map)

    def operations(self) -> list[str]:
        """All operations with declared Γ constraints."""
        return sorted(self._gamma_map)
