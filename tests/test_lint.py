"""
GammaLinter tests — static Γ analysis of Python source code.

No running code. No mocks. No servers. Just source strings and AST analysis.

These tests show that Γ violations in source code are caught statically —
the same violations that would raise at runtime are reported before
any code runs.
"""
from __future__ import annotations

import pytest

from tenet.lint import GammaLinter, LintIssue
from tenet.errors import RequiresPriorViolation, ForbiddenAfterViolation
from tests.test_py_mock import FakeSession


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------

@pytest.fixture
def linter():
    return GammaLinter.for_classes(FakeSession)


# ---------------------------------------------------------------------------
# Valid sequences — no issues reported
# ---------------------------------------------------------------------------

def test_no_issues_for_valid_lifecycle(linter):
    source = """
session = FakeSession()
session.connect()
session.begin()
result = session.execute("SELECT 1")
session.commit()
session.close()
"""
    issues = linter.check_source(source)
    assert issues == []


def test_no_issues_for_connect_only(linter):
    source = """
session = FakeSession()
session.connect()
"""
    issues = linter.check_source(source)
    assert issues == []


def test_no_issues_when_class_not_tracked(linter):
    """Unknown classes are not tracked — no false positives."""
    source = """
conn = SomethingElse()
conn.execute("SELECT 1")
"""
    issues = linter.check_source(source)
    assert issues == []


# ---------------------------------------------------------------------------
# Violations caught statically
# ---------------------------------------------------------------------------

def test_execute_without_begin(linter):
    source = """
session = FakeSession()
session.connect()
session.execute("SELECT 1")
"""
    issues = linter.check_source(source)
    assert len(issues) == 1
    assert issues[0].operation == "execute"
    assert isinstance(issues[0].violation, RequiresPriorViolation)
    assert "begin" in issues[0].violation.missing


def test_begin_without_connect(linter):
    source = """
session = FakeSession()
session.begin()
"""
    issues = linter.check_source(source)
    assert len(issues) == 1
    assert issues[0].operation == "begin"
    assert isinstance(issues[0].violation, RequiresPriorViolation)
    assert "connect" in issues[0].violation.missing


def test_double_close(linter):
    source = """
session = FakeSession()
session.connect()
session.close()
session.close()
"""
    issues = linter.check_source(source)
    assert len(issues) == 1
    assert issues[0].operation == "close"
    assert isinstance(issues[0].violation, ForbiddenAfterViolation)
    assert issues[0].violation.blocked_by == "close"


def test_multiple_violations_all_reported(linter):
    """Linter is exhaustive — reports all violations, not just the first."""
    source = """
session = FakeSession()
session.execute("SELECT 1")
session.close()
session.close()
"""
    issues = linter.check_source(source)
    # execute without begin, first close without connect, second close (forbidden)
    assert len(issues) >= 2
    operations = {i.operation for i in issues}
    assert "execute" in operations
    assert "close" in operations


# ---------------------------------------------------------------------------
# Location reporting
# ---------------------------------------------------------------------------

def test_issue_reports_correct_line(linter):
    source = "session = FakeSession()\nsession.connect()\nsession.execute('x')\n"
    issues = linter.check_source(source, filename="test.py")
    assert len(issues) == 1
    assert issues[0].line == 3
    assert issues[0].file == "test.py"
    assert issues[0].variable == "session"


def test_issue_str_format(linter):
    source = "session = FakeSession()\nsession.execute('x')\n"
    issues = linter.check_source(source, filename="app.py")
    s = str(issues[0])
    assert "app.py" in s
    assert "execute" in s
    assert "session" in s


# ---------------------------------------------------------------------------
# Scope isolation — different functions are independent
# ---------------------------------------------------------------------------

def test_violations_are_per_scope(linter):
    """A violation in one function doesn't affect another."""
    source = """
def good():
    session = FakeSession()
    session.connect()
    session.begin()
    session.execute("SELECT 1")

def bad():
    session = FakeSession()
    session.execute("SELECT 1")
"""
    issues = linter.check_source(source)
    assert len(issues) == 1
    assert issues[0].operation == "execute"


def test_two_independent_variables(linter):
    """Two sessions in the same scope are tracked independently."""
    source = """
s1 = FakeSession()
s2 = FakeSession()
s1.connect()
s1.begin()
s1.execute("ok")
s2.execute("not ok")
"""
    issues = linter.check_source(source)
    assert len(issues) == 1
    assert issues[0].variable == "s2"


# ---------------------------------------------------------------------------
# Constructor variants
# ---------------------------------------------------------------------------

def test_tracks_attribute_constructor(linter):
    """module.FakeSession() is also tracked."""
    source = """
import mymodule
session = mymodule.FakeSession()
session.execute("SELECT 1")
"""
    issues = linter.check_source(source)
    # FakeSession matched by class name regardless of module
    assert len(issues) == 1


def test_no_track_after_reassignment(linter):
    """If a variable is reassigned to an untracked type, tracking stops."""
    source = """
session = FakeSession()
session.connect()
session = None
session.execute("x")
"""
    issues = linter.check_source(source)
    # After reassignment, session is no longer tracked — no false positive
    assert all(i.operation != "execute" for i in issues)


# ---------------------------------------------------------------------------
# Grammar inspection
# ---------------------------------------------------------------------------

def test_tracked_classes(linter):
    assert "FakeSession" in linter.tracked_classes()


def test_grammar_for_class(linter):
    grammar = linter.grammar_for("FakeSession")
    assert grammar is not None
    assert "begin" in grammar
    assert "execute" in grammar
    assert "close" in grammar


def test_grammar_for_unknown_class(linter):
    assert linter.grammar_for("NonExistent") is None


# ---------------------------------------------------------------------------
# Grammar overrides
# ---------------------------------------------------------------------------

def test_override_relaxes_constraint():
    """An explicit grammar override can relax an inferred constraint."""
    from tenet.spec import OperationGamma
    # Override: execute only requires connect (not begin)
    linter = GammaLinter.for_classes(
        FakeSession,
        grammar_overrides={
            "FakeSession": {
                "execute": OperationGamma(
                    operation_id="execute",
                    method="call",
                    path="FakeSession.execute",
                    requires_prior=["connect"],
                )
            }
        },
    )
    source = """
session = FakeSession()
session.connect()
session.execute("SELECT 1")
"""
    issues = linter.check_source(source)
    assert issues == []


# ---------------------------------------------------------------------------
# check_directory
# ---------------------------------------------------------------------------

def test_check_directory(linter, tmp_path):
    good = tmp_path / "good.py"
    good.write_text("session = FakeSession()\nsession.connect()\n")

    bad = tmp_path / "bad.py"
    bad.write_text("session = FakeSession()\nsession.execute('x')\n")

    issues = linter.check_directory(tmp_path)
    files_with_issues = {i.file for i in issues}
    assert str(bad) in files_with_issues
    assert str(good) not in files_with_issues
