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

    from tenet.py_mock import GammaPyMock

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

import ast
import inspect
import re
import textwrap
from typing import Any, get_type_hints

from tenet.errors import GammaViolation
from tenet.spec import OperationGamma
from tenet.static import GammaChecker
from tenet.type_gen import TypeGenerator


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
    Infer Γ from a class — three layers, each filling gaps the previous misses.

    1. Source guards: reads precondition checks written in method bodies
       (if not self._attr: raise ...). Most accurate — reads grammar as written.
    2. Docstring keywords: requires/must call/after patterns in docstrings.
    3. Lifecycle heuristics: common pairs (connect/begin, begin/execute, open/close).

    Source reading wins where it can reach. Heuristics fill the gaps for
    libraries without accessible source (C extensions, compiled code).
    """
    methods = _public_methods(klass)
    method_names = set(methods)

    requires_prior_map: dict[str, list[str]] = {}
    forbidden_after_map: dict[str, list[str]] = {}

    # --- Layer 1: source guard reader ---
    try:
        source_requires, source_forbids = _read_source_guards(klass, method_names)
        for dep, prereqs in source_requires.items():
            requires_prior_map.setdefault(dep, [])
            for p in prereqs:
                if p not in requires_prior_map[dep]:
                    requires_prior_map[dep].append(p)
        for op, blockers in source_forbids.items():
            forbidden_after_map.setdefault(op, [])
            for b in blockers:
                if b not in forbidden_after_map[op]:
                    forbidden_after_map[op].append(b)
    except Exception:
        pass  # source not available (C extension, built-in) — fall through

    # --- Layer 2: docstring keywords ---
    for name, method in methods.items():
        doc = inspect.getdoc(method) or ""
        for pattern in _REQUIRES_PRIOR_PATTERNS:
            for m in pattern.finditer(doc):
                candidate = m.group(1)
                if candidate in method_names and candidate != name:
                    requires_prior_map.setdefault(name, [])
                    if candidate not in requires_prior_map[name]:
                        requires_prior_map[name].append(candidate)

    # --- Layer 3: lifecycle heuristics ---
    for prerequisite, dependent in _LIFECYCLE_PAIRS:
        if prerequisite in method_names and dependent in method_names:
            requires_prior_map.setdefault(dependent, [])
            if prerequisite not in requires_prior_map[dependent]:
                requires_prior_map[dependent].append(prerequisite)

    # Self-forbidding ops (name-based — source reader catches the general case)
    for name in method_names:
        if name in _SELF_FORBIDDING and name not in forbidden_after_map:
            forbidden_after_map[name] = [name]

    # Assemble
    all_constrained = set(requires_prior_map) | set(forbidden_after_map)
    grammar: dict[str, OperationGamma] = {}
    for name in all_constrained:
        grammar[name] = OperationGamma(
            operation_id=name,
            method="call",
            path=f"{klass.__qualname__}.{name}",
            requires_prior=requires_prior_map.get(name) or None,
            forbidden_after=forbidden_after_map.get(name) or None,
        )

    return grammar


def _read_source_guards(
    klass: type,
    method_names: set[str],
) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    """
    Read precondition guards from method bodies.

    Extracts two patterns:

    requires_prior — guard variable must be True:
        if not self._attr: raise ...
        assert self._attr, ...

    forbidden_after — guard variable must be False:
        if self._attr: raise ...
        assert not self._attr, ...

    Then maps each guard variable back to the methods that set it,
    producing requires_prior and forbidden_after entries.

    Returns (requires_prior_map, forbidden_after_map).
    """
    # Step 1: for each method, find which instance attributes it sets True/False
    # attr_set_by[attr] = list of method names that set self.attr = True
    # attr_cleared_by[attr] = list of method names that set self.attr = False
    attr_set_by: dict[str, list[str]] = {}
    attr_cleared_by: dict[str, list[str]] = {}

    for name in method_names:
        method = getattr(klass, name, None)
        if method is None:
            continue
        try:
            src = textwrap.dedent(inspect.getsource(method))
            tree = ast.parse(src)
        except Exception:
            continue

        for node in ast.walk(tree):
            # self._attr = True / self._attr = False
            if (
                isinstance(node, ast.Assign)
                and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Attribute)
                and isinstance(node.targets[0].value, ast.Name)
                and node.targets[0].value.id == "self"
            ):
                attr = node.targets[0].attr
                val = node.value
                if isinstance(val, ast.Constant):
                    if val.value is True:
                        attr_set_by.setdefault(attr, [])
                        if name not in attr_set_by[attr]:
                            attr_set_by[attr].append(name)
                    elif val.value is False:
                        attr_cleared_by.setdefault(attr, [])
                        if name not in attr_cleared_by[attr]:
                            attr_cleared_by[attr].append(name)

    # Step 2: for each method, find guard checks near the top of the body
    requires_prior: dict[str, list[str]] = {}
    forbidden_after: dict[str, list[str]] = {}

    for name in method_names:
        method = getattr(klass, name, None)
        if method is None:
            continue
        try:
            src = textwrap.dedent(inspect.getsource(method))
            tree = ast.parse(src)
        except Exception:
            continue

        func_body = _get_func_body(tree)
        if func_body is None:
            continue

        # Scan the first few statements for guard patterns
        for stmt in func_body[:6]:
            guards_positive, guards_negative = _extract_guards(stmt)

            # if not self._attr: raise → attr must be True → set_by methods required
            for attr in guards_positive:
                setters = attr_set_by.get(attr, [])
                for setter in setters:
                    if setter != name and setter in method_names:
                        requires_prior.setdefault(name, [])
                        if setter not in requires_prior[name]:
                            requires_prior[name].append(setter)

            # if self._attr: raise → attr must be False → set_by methods forbidden
            for attr in guards_negative:
                setters = attr_set_by.get(attr, [])
                for setter in setters:
                    if setter != name and setter in method_names:
                        forbidden_after.setdefault(name, [])
                        if setter not in forbidden_after[name]:
                            forbidden_after[name].append(setter)

    return requires_prior, forbidden_after


def _get_func_body(tree: ast.AST) -> list[ast.stmt] | None:
    """Return the body of the first function definition found."""
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return node.body
    return None


def _extract_guards(stmt: ast.stmt) -> tuple[list[str], list[str]]:
    """
    Extract guard attribute names from a single statement.

    Returns (must_be_true, must_be_false) — lists of self._attr names.

    must_be_true:  if not self._attr: raise / assert self._attr
    must_be_false: if self._attr: raise / assert not self._attr
    """
    must_be_true: list[str] = []
    must_be_false: list[str] = []

    # if <test>: raise / if <test>: raise (with only raise in body)
    if isinstance(stmt, ast.If):
        body_is_raise = (
            len(stmt.body) == 1
            and isinstance(stmt.body[0], (ast.Raise, ast.Return))
            and not stmt.orelse
        )
        if body_is_raise:
            test = stmt.test
            # if not self._attr: raise  → attr must be True
            if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
                attr = _self_attr(test.operand)
                if attr:
                    must_be_true.append(attr)
            # if self._attr: raise  → attr must be False
            else:
                attr = _self_attr(test)
                if attr:
                    must_be_false.append(attr)

    # assert self._attr / assert not self._attr
    if isinstance(stmt, ast.Assert):
        test = stmt.test
        if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
            attr = _self_attr(test.operand)
            if attr:
                must_be_false.append(attr)
        else:
            attr = _self_attr(test)
            if attr:
                must_be_true.append(attr)

    return must_be_true, must_be_false


def _self_attr(node: ast.expr) -> str | None:
    """Return the attribute name if node is self._something, else None."""
    if (
        isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "self"
    ):
        return node.attr
    return None


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
