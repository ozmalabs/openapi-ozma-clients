"""
Static Γ checker tests.

No server. No HTTP. No running application.
The grammar is in the spec; the spec is a file; the checker is pure Python.

These are CONTRACT TESTS — they verify that the declared grammar is
internally consistent and that described workflows are admissible.
If the spec changes and a previously-valid workflow becomes invalid,
these tests catch it before anything is deployed.
"""
from __future__ import annotations

import pytest

from tests.fixtures.item_app import app
from tenet.spec import parse_spec
from tenet.static import GammaChecker, GrammarIssue
from tenet.errors import (
    ForbiddenAfterViolation,
    RequiresPriorViolation,
    RequiresStateViolation,
)


# ---------------------------------------------------------------------------
# Shared fixture — parse the spec once, no server needed after this
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def gamma_map():
    """
    Generate the gamma_map directly from the app's OpenAPI schema.
    No HTTP. No running server. This is the point.

    In CI or with an external API, use:
        parse_spec(load_spec_file("openapi.json"))
    """
    return parse_spec(app.openapi())


@pytest.fixture
def checker(gamma_map):
    return GammaChecker(gamma_map)


# ---------------------------------------------------------------------------
# Grammar analysis — is the spec internally consistent?
# ---------------------------------------------------------------------------

def test_grammar_has_no_errors(checker):
    """The reference app's grammar is self-consistent."""
    issues = checker.analyze()
    errors = [i for i in issues if i.severity == "error"]
    assert errors == [], "\n".join(str(i) for i in errors)


def test_grammar_warnings_only(checker):
    """The reference app grammar has no errors — warnings only."""
    issues = checker.analyze()
    errors = [i for i in issues if i.severity == "error"]
    assert errors == []
    # warnings are informational — fine to have them


# ---------------------------------------------------------------------------
# Valid sequence checking — no server, pure grammar simulation
# ---------------------------------------------------------------------------

def test_full_lifecycle_is_admissible(checker):
    violations = checker.check_sequence(
        ["createItem", "publishItem", "archiveItem"],
        resource_states={"item:1": "draft"},
        resource_key="item:1",
    )
    assert violations == []


def test_create_and_delete_is_admissible(checker):
    violations = checker.check_sequence(
        ["createItem", "deleteItem"],
        resource_states={"item:1": "draft"},
        resource_key="item:1",
    )
    assert violations == []


def test_cart_checkout_is_admissible(checker):
    violations = checker.check_sequence(["addToCart", "checkout"])
    assert violations == []


def test_create_get_is_admissible(checker):
    violations = checker.check_sequence(["createItem", "getItem"])
    assert violations == []


# ---------------------------------------------------------------------------
# Invalid sequences — caught statically, no server needed
# ---------------------------------------------------------------------------

def test_archive_without_publish_is_inadmissible(checker):
    """
    Skipping publishItem means the resource is still in draft.
    archiveItem requires_state published — violation caught statically.
    """
    violations = checker.check_sequence(
        ["createItem", "archiveItem"],
        resource_states={"item:1": "draft"},
        resource_key="item:1",
    )
    assert len(violations) == 1
    assert isinstance(violations[0], RequiresStateViolation)
    assert violations[0].operation_id == "archiveItem"
    assert violations[0].current_state == "draft"
    assert "published" in violations[0].required_states


def test_checkout_without_cart_is_inadmissible(checker):
    violations = checker.check_sequence(["checkout"])
    assert len(violations) == 1
    assert isinstance(violations[0], RequiresPriorViolation)
    assert violations[0].operation_id == "checkout"
    assert "addToCart" in violations[0].missing


def test_double_archive_is_inadmissible(checker):
    """archiveItem is in its own forbidden_after."""
    violations = checker.check_sequence(
        ["createItem", "publishItem", "archiveItem", "archiveItem"],
        resource_states={"item:1": "draft"},
        resource_key="item:1",
    )
    assert any(isinstance(v, ForbiddenAfterViolation) for v in violations)
    blocked = next(v for v in violations if isinstance(v, ForbiddenAfterViolation))
    assert blocked.operation_id == "archiveItem"
    assert blocked.blocked_by == "archiveItem"


def test_all_violations_reported_not_just_first(checker):
    """
    A sequence with multiple violations reports all of them,
    not just the first. Static checking is exhaustive.
    """
    # checkout requires addToCart; also doing it twice triggers forbidden_after
    # if checkout had it — in this grammar, checkout doesn't self-forbid, but
    # we can construct a multi-violation sequence.
    # archive without publish (state violation) AND archive twice (forbidden_after):
    violations = checker.check_sequence(
        ["createItem", "archiveItem", "archiveItem"],
        resource_states={"item:1": "draft"},
        resource_key="item:1",
    )
    assert len(violations) >= 2  # both archiveItem calls are flagged


# ---------------------------------------------------------------------------
# valid_next — grammar answers "what can I do now?"
# ---------------------------------------------------------------------------

def test_valid_next_from_empty_session(checker):
    """From a fresh session, operations with no preconditions are admissible."""
    admissible = checker.valid_next(called=set())
    assert "createItem" in admissible
    assert "addToCart" in admissible
    # checkout requires_prior addToCart — not yet called
    assert "checkout" not in admissible


def test_valid_next_after_add_to_cart(checker):
    admissible = checker.valid_next(called={"addToCart"})
    assert "checkout" in admissible


def test_valid_next_with_resource_in_draft(checker):
    admissible = checker.valid_next(
        called={"createItem"},
        resource_states={"item:1": "draft"},
        resource_key="item:1",
    )
    assert "publishItem" in admissible
    assert "deleteItem" in admissible
    assert "archiveItem" not in admissible  # requires published


def test_valid_next_with_resource_published(checker):
    admissible = checker.valid_next(
        called={"createItem", "publishItem"},
        resource_states={"item:1": "published"},
        resource_key="item:1",
    )
    assert "archiveItem" in admissible
    assert "deleteItem" in admissible       # requires draft or published — both ok
    assert "publishItem" not in admissible  # requires_state=draft; resource is published


def test_valid_next_after_archive_excludes_archive(checker):
    admissible = checker.valid_next(
        called={"createItem", "publishItem", "archiveItem"},
        resource_states={"item:1": "archived"},
        resource_key="item:1",
    )
    assert "archiveItem" not in admissible  # forbidden_after itself


# ---------------------------------------------------------------------------
# Path enumeration — grammar-derived test cases
# ---------------------------------------------------------------------------

def test_enumerate_paths_includes_full_lifecycle(checker):
    paths = checker.enumerate_paths(
        max_length=4,
        resource_key="item:1",
        initial_state="draft",
    )
    path_sets = [tuple(p) for p in paths]
    assert ("createItem", "publishItem", "archiveItem") in path_sets


def test_enumerate_paths_excludes_invalid_sequences(checker):
    """Every enumerated path must itself pass check_sequence."""
    paths = checker.enumerate_paths(
        max_length=3,
        resource_key="item:1",
        initial_state="draft",
    )
    for path in paths:
        violations = checker.check_sequence(
            path,
            resource_states={"item:1": "draft"},
            resource_key="item:1",
        )
        assert violations == [], f"path {path} was enumerated but is invalid: {violations}"


def test_enumerate_paths_cart_checkout(checker):
    paths = checker.enumerate_paths(max_length=3)
    path_sets = [tuple(p) for p in paths]
    assert ("addToCart", "checkout") in path_sets
    # checkout before addToCart must NOT appear
    assert ("checkout",) not in path_sets
    assert ("checkout", "addToCart") not in path_sets


# ---------------------------------------------------------------------------
# Grammar analysis on a broken grammar (unit test the analyzer itself)
# ---------------------------------------------------------------------------

def test_analyzer_catches_unknown_state_reference():
    from tenet.spec import OperationGamma
    broken = {
        "publish": OperationGamma(
            operation_id="publish",
            method="post",
            path="/publish",
            requires_state=["nonexistent_state"],
            states=["draft", "published"],
        )
    }
    checker = GammaChecker(broken)
    issues = checker.analyze()
    errors = [i for i in issues if i.code == "unknown_state_ref"]
    assert len(errors) == 1
    assert "nonexistent_state" in errors[0].description


def test_analyzer_catches_unknown_op_reference():
    from tenet.spec import OperationGamma
    broken = {
        "checkout": OperationGamma(
            operation_id="checkout",
            method="post",
            path="/checkout",
            requires_prior=["ghost_operation"],
        )
    }
    checker = GammaChecker(broken)
    issues = checker.analyze()
    errors = [i for i in issues if i.code == "unknown_op_ref"]
    assert len(errors) == 1
    assert "ghost_operation" in errors[0].description


def test_analyzer_catches_requires_prior_cycle():
    from tenet.spec import OperationGamma
    # A requires B, B requires A — permanently deadlocked
    broken = {
        "opA": OperationGamma(
            operation_id="opA", method="post", path="/a",
            requires_prior=["opB"],
        ),
        "opB": OperationGamma(
            operation_id="opB", method="post", path="/b",
            requires_prior=["opA"],
        ),
    }
    checker = GammaChecker(broken)
    issues = checker.analyze()
    errors = [i for i in issues if i.code == "requires_prior_cycle"]
    assert len(errors) == 1
    assert "opA" in errors[0].description or "opB" in errors[0].description
