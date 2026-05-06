"""
GammaPyMock tests.

The same proof as test_mock.py, but for Python objects instead of HTTP.

A real class with a lifecycle contract and a GammaPyMock of that class
are tested against the same scenarios. If both pass, the mock is valid.

No MagicMock. No patch. No encoded assumptions. The mock is the contract.
"""
from __future__ import annotations

import pytest

from tenet.py_mock import GammaPyMock, infer_grammar
from tenet.spec import OperationGamma
from tenet.errors import GammaViolation, RequiresPriorViolation, ForbiddenAfterViolation


# ---------------------------------------------------------------------------
# Reference class — a simple database session with a clear lifecycle
# ---------------------------------------------------------------------------

class FakeSession:
    """
    Minimal database-session-like class.

    Lifecycle: connect → begin → execute* → commit/rollback → close.
    close is self-forbidding (can't close twice).
    """

    def __init__(self) -> None:
        self._connected = False
        self._in_transaction = False
        self._closed = False

    def connect(self) -> None:
        """Establish the connection."""
        self._connected = True

    def begin(self) -> None:
        """Begin a transaction. Requires: connect."""
        if not self._connected:
            raise RuntimeError("not connected")
        self._in_transaction = True

    def execute(self, sql: str) -> list[dict]:
        """Execute SQL. Requires: begin."""
        if not self._in_transaction:
            raise RuntimeError("no active transaction")
        return [{"result": "ok"}]

    def commit(self) -> None:
        """Commit the transaction. Requires: begin."""
        if not self._in_transaction:
            raise RuntimeError("nothing to commit")
        self._in_transaction = False

    def rollback(self) -> None:
        """Rollback the transaction. Requires: begin."""
        self._in_transaction = False

    def close(self) -> None:
        """Close the connection. Requires: connect. Cannot be called twice."""
        if self._closed:
            raise RuntimeError("already closed")
        self._closed = True


# ---------------------------------------------------------------------------
# Shared test scenarios — class-agnostic
# ---------------------------------------------------------------------------

def _run_full_lifecycle(session_factory) -> None:
    """connect → begin → execute → commit → close"""
    s = session_factory()
    s.connect()
    s.begin()
    result = s.execute("SELECT 1")
    assert isinstance(result, list)
    s.commit()
    s.close()


def _run_execute_without_begin(session_factory) -> type[Exception]:
    """execute without begin — should fail with some exception."""
    s = session_factory()
    s.connect()
    try:
        s.execute("SELECT 1")
        pytest.fail("expected an error")
    except Exception as e:
        return type(e)


def _run_begin_without_connect(session_factory) -> type[Exception]:
    s = session_factory()
    try:
        s.begin()
        pytest.fail("expected an error")
    except Exception as e:
        return type(e)


# ---------------------------------------------------------------------------
# The proof: same lifecycle, two backends
# ---------------------------------------------------------------------------

def test_full_lifecycle_real():
    _run_full_lifecycle(FakeSession)


def test_full_lifecycle_mock():
    MockSession = GammaPyMock.from_class(FakeSession)
    _run_full_lifecycle(MockSession)


def test_execute_without_begin_real():
    exc_type = _run_execute_without_begin(FakeSession)
    assert exc_type is RuntimeError


def test_execute_without_begin_mock():
    MockSession = GammaPyMock.from_class(FakeSession)
    exc_type = _run_execute_without_begin(MockSession)
    assert issubclass(exc_type, GammaViolation)


def test_begin_without_connect_real():
    exc_type = _run_begin_without_connect(FakeSession)
    assert exc_type is RuntimeError


def test_begin_without_connect_mock():
    MockSession = GammaPyMock.from_class(FakeSession)
    exc_type = _run_begin_without_connect(MockSession)
    assert issubclass(exc_type, GammaViolation)


# ---------------------------------------------------------------------------
# Mock-specific: type-valid return values
# ---------------------------------------------------------------------------

def test_mock_execute_returns_list():
    """execute() → list[dict] — type-valid, not MagicMock."""
    MockSession = GammaPyMock.from_class(FakeSession)
    s = MockSession()
    s.connect()
    s.begin()
    result = s.execute("SELECT 1")
    assert isinstance(result, list)
    assert isinstance(result[0], dict)


def test_mock_returns_none_for_void_methods():
    """connect/begin/commit return None — same as real."""
    MockSession = GammaPyMock.from_class(FakeSession)
    s = MockSession()
    assert s.connect() is None


# ---------------------------------------------------------------------------
# Mock-specific: session tracking and reset
# ---------------------------------------------------------------------------

def test_mock_tracks_called_methods():
    MockSession = GammaPyMock.from_class(FakeSession)
    s = MockSession()
    s.connect()
    s.begin()
    assert "connect" in s.called()
    assert "begin" in s.called()


def test_mock_reset_clears_state():
    MockSession = GammaPyMock.from_class(FakeSession)
    s = MockSession()
    s.connect()
    s.begin()
    s.reset()
    assert s.called() == set()
    # After reset, begin is blocked again
    with pytest.raises(GammaViolation):
        s.begin()


# ---------------------------------------------------------------------------
# Grammar inference
# ---------------------------------------------------------------------------

def test_infer_grammar_finds_lifecycle_constraints():
    grammar = infer_grammar(FakeSession)
    # begin requires connect (lifecycle pair)
    assert "begin" in grammar
    assert "connect" in grammar["begin"].requires_prior

    # execute requires begin (lifecycle pair)
    assert "execute" in grammar
    assert "begin" in grammar["execute"].requires_prior

    # commit requires begin
    assert "commit" in grammar
    assert "begin" in grammar["commit"].requires_prior


def test_infer_grammar_self_forbids_close():
    grammar = infer_grammar(FakeSession)
    assert "close" in grammar
    assert "close" in grammar["close"].forbidden_after


def test_double_close_is_blocked():
    MockSession = GammaPyMock.from_class(FakeSession)
    s = MockSession()
    s.connect()
    s.begin()
    s.commit()
    s.close()
    with pytest.raises(ForbiddenAfterViolation) as exc_info:
        s.close()
    assert exc_info.value.blocked_by == "close"


# ---------------------------------------------------------------------------
# Explicit grammar override
# ---------------------------------------------------------------------------

def test_explicit_grammar_overrides_inference():
    """An explicit grammar entry overrides the inferred one."""
    explicit = {
        "execute": OperationGamma(
            operation_id="execute",
            method="call",
            path="FakeSession.execute",
            requires_prior=["connect"],  # only requires connect, not begin
        )
    }
    MockSession = GammaPyMock.from_class(FakeSession, grammar=explicit)
    s = MockSession()
    s.connect()
    # execute is now admissible without begin
    result = s.execute("SELECT 1")
    assert isinstance(result, list)


def test_explicit_grammar_no_infer():
    """from_class_no_infer uses only what's given — nothing inferred."""
    MockSession = GammaPyMock.from_class_no_infer(
        FakeSession,
        grammar={},  # empty — all operations always admissible
    )
    s = MockSession()
    # execute without connect/begin — no grammar, no violation
    result = s.execute("SELECT 1")
    assert isinstance(result, list)


# ---------------------------------------------------------------------------
# Grammar inspection
# ---------------------------------------------------------------------------

def test_mock_reports_grammar():
    MockSession = GammaPyMock.from_class(FakeSession)
    grammar = MockSession.grammar()
    assert "begin" in grammar
    assert isinstance(grammar["begin"], OperationGamma)


def test_mock_reports_operations():
    MockSession = GammaPyMock.from_class(FakeSession)
    ops = MockSession.operations()
    assert "begin" in ops
    assert "close" in ops


# ---------------------------------------------------------------------------
# TypeGenerator — standalone
# ---------------------------------------------------------------------------

def test_type_gen_primitives():
    from tenet.type_gen import TypeGenerator
    gen = TypeGenerator()
    assert isinstance(gen.generate(int), int)
    assert isinstance(gen.generate(str), str)
    assert isinstance(gen.generate(float), float)
    assert isinstance(gen.generate(bool), bool)
    assert gen.generate(type(None)) is None


def test_type_gen_list():
    from tenet.type_gen import TypeGenerator
    import typing
    gen = TypeGenerator()
    result = gen.generate(typing.List[int])
    assert isinstance(result, list)
    assert isinstance(result[0], int)


def test_type_gen_optional():
    from tenet.type_gen import TypeGenerator
    import typing
    gen = TypeGenerator()
    result = gen.generate(typing.Optional[str])
    # prefers non-None
    assert isinstance(result, str)


def test_type_gen_pydantic_model():
    from tenet.type_gen import TypeGenerator
    from pydantic import BaseModel

    class Item(BaseModel):
        id: int
        title: str
        status: str

    gen = TypeGenerator()
    result = gen.generate(Item)
    assert isinstance(result, Item)
    assert isinstance(result.id, int)
    assert isinstance(result.title, str)


# ---------------------------------------------------------------------------
# Source guard reader — catches constraints not in lifecycle pairs or docstrings
# ---------------------------------------------------------------------------

class GuardOnlySession:
    """
    No docstrings. No lifecycle-pair method names.
    Grammar exists only as guards in the method bodies.
    The heuristic reader would find nothing. The source reader finds it all.
    """

    def __init__(self) -> None:
        self._ready = False
        self._done = False

    def prepare(self) -> None:
        self._ready = True

    def process(self, data: str) -> list[str]:
        if not self._ready:
            raise RuntimeError("not ready")
        return [data]

    def finish(self) -> None:
        if not self._ready:
            raise RuntimeError("not ready")
        self._done = True

    def restart(self) -> None:
        if self._done:
            raise RuntimeError("already finished")
        self._ready = False


def test_source_reader_finds_guards_without_heuristics():
    """Source reader extracts grammar from method body guards alone."""
    grammar = infer_grammar(GuardOnlySession)

    # process requires prepare (reads: if not self._ready: raise)
    assert "process" in grammar
    assert "prepare" in grammar["process"].requires_prior

    # finish requires prepare
    assert "finish" in grammar
    assert "prepare" in grammar["finish"].requires_prior

    # restart is forbidden after finish (reads: if self._done: raise, finish sets _done=True)
    assert "restart" in grammar
    assert "finish" in grammar["restart"].forbidden_after


def test_source_reader_mock_enforces_guard_constraints():
    """GammaPyMock built on source-read grammar enforces the implied constraints."""
    MockSession = GammaPyMock.from_class(GuardOnlySession)

    s = MockSession()
    with pytest.raises(GammaViolation):
        s.process("data")  # prepare not called

    s.prepare()
    result = s.process("data")
    assert isinstance(result, list)

    s.finish()
    with pytest.raises(GammaViolation):
        s.restart()  # forbidden after finish
