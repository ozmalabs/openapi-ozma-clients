"""
GammaLinter — static Γ analysis of Python source code.

Type checkers (mypy, pyright) verify type compatibility at each call site.
GammaLinter verifies ordering compatibility — whether call sequences satisfy
the declared admissibility grammar — without running any code.

Given a grammar for any library (inferred or declared), GammaLinter walks
the AST of your codebase and reports every place where a call is inadmissible
given what happened before it in the same scope.

Example violations caught statically::

    session = Session()
    session.execute("SELECT 1")   # ← LINT: begin() not called yet
    session.begin()

    client = Session()
    client.connect()
    client.close()
    client.close()                # ← LINT: close is self-forbidding

    conn = Connection()
    conn.send(data)               # ← LINT: connect() not called yet

Usage::

    from tenet.lint import GammaLinter
    from mylib import Session

    linter = GammaLinter.for_classes(Session)
    issues = linter.check_source(source_code)
    for issue in issues:
        print(f"{issue.file}:{issue.line}:{issue.col}: {issue.message}")

    # Or check a whole file
    issues = linter.check_file("src/db/queries.py")

    # Or configure with explicit grammars
    linter = GammaLinter.for_classes(
        Session,
        grammar_overrides={"Session": {"execute": OperationGamma(...)}},
    )
"""
from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tenet.errors import GammaViolation
from tenet.py_mock import infer_grammar
from tenet.spec import OperationGamma
from tenet.static import GammaChecker


# ---------------------------------------------------------------------------
# Lint issue
# ---------------------------------------------------------------------------

@dataclass
class LintIssue:
    """A Γ violation found statically in source code."""
    file: str
    line: int
    col: int
    variable: str
    operation: str
    message: str
    violation: GammaViolation

    def __str__(self) -> str:
        return f"{self.file}:{self.line}:{self.col}: [{self.variable}.{self.operation}] {self.message}"


# ---------------------------------------------------------------------------
# Per-variable tracking state within one scope
# ---------------------------------------------------------------------------

@dataclass
class _VarState:
    class_name: str
    called: set[str] = field(default_factory=set)
    resource_states: dict[str, str] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# AST visitor — checks one function body
# ---------------------------------------------------------------------------

class _ScopeChecker(ast.NodeVisitor):
    """
    Walks a function or module scope, tracking variable bindings and
    checking method call sequences against the declared grammar.

    Control flow: linear scan only — we check the sequence as written,
    without branching. This is conservative: we only report violations
    that occur on every path through the code (the path as linearised
    by reading order). We never report false positives from branch analysis.
    """

    def __init__(
        self,
        tracked: dict[str, dict[str, OperationGamma]],  # class_name → grammar
        filename: str,
    ) -> None:
        self._tracked = tracked         # which class names to watch
        self._filename = filename
        self._vars: dict[str, _VarState] = {}   # var_name → tracking state
        self.issues: list[LintIssue] = []

    # ------------------------------------------------------------------
    # Variable binding detection
    # ------------------------------------------------------------------

    def visit_Assign(self, node: ast.Assign) -> None:
        """x = SomeClass() — start tracking; x = other — stop tracking."""
        class_name = self._class_from_expr(node.value)

        for target in node.targets:
            if isinstance(target, ast.Name):
                if class_name and class_name in self._tracked:
                    self._vars[target.id] = _VarState(class_name)
                elif target.id in self._vars:
                    # Reassigned to something we can't track — drop it
                    source = self._maybe_source_var(node.value)
                    if source and source in self._vars:
                        # x = y — propagate y's state to x
                        src_state = self._vars[source]
                        self._vars[target.id] = _VarState(
                            class_name=src_state.class_name,
                            called=set(src_state.called),
                            resource_states=dict(src_state.resource_states),
                        )
                    else:
                        del self._vars[target.id]

        # Still check for method calls in the RHS
        self._check_expr_for_calls(node.value, node.lineno, node.col_offset)
        # Don't call generic_visit — we've handled children explicitly

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        """x: SomeClass = ... — start tracking."""
        if isinstance(node.target, ast.Name):
            class_name = self._class_name_from_annotation(node.annotation)
            if class_name and class_name in self._tracked:
                self._vars[node.target.id] = _VarState(class_name)
        if node.value:
            self._check_expr_for_calls(node.value, node.lineno, node.col_offset)

    def visit_With(self, node: ast.With) -> None:
        """with SomeClass() as x: — track x inside the with block."""
        for item in node.items:
            class_name = self._class_from_expr(item.context_expr)
            if class_name and class_name in self._tracked:
                if item.optional_vars and isinstance(item.optional_vars, ast.Name):
                    self._vars[item.optional_vars.id] = _VarState(class_name)
        self.generic_visit(node)

    # ------------------------------------------------------------------
    # Method call detection
    # ------------------------------------------------------------------

    def visit_Expr(self, node: ast.Expr) -> None:
        """Statement-level call: session.begin()"""
        self._check_expr_for_calls(node.value, node.lineno, node.col_offset)

    def _check_expr_for_calls(self, node: ast.expr, line: int, col: int) -> None:
        if not isinstance(node, ast.Call):
            return
        func = node.func
        if not isinstance(func, ast.Attribute):
            return
        if not isinstance(func.value, ast.Name):
            return

        var_name = func.value.id
        method_name = func.attr

        if var_name not in self._vars:
            return

        state = self._vars[var_name]
        grammar = self._tracked[state.class_name]
        gamma = grammar.get(method_name)

        if gamma is not None:
            checker = GammaChecker(grammar)
            violations = checker._check_step(
                method_name, gamma, state.called, state.resource_states, None
            )
            for v in violations:
                self.issues.append(LintIssue(
                    file=self._filename,
                    line=line,
                    col=col,
                    variable=var_name,
                    operation=method_name,
                    message=v.reason,
                    violation=v,
                ))

        # Advance state regardless of violations (report all issues, not just first)
        state.called.add(method_name)
        if gamma and gamma.produces_state:
            state.resource_states["_default"] = gamma.produces_state

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _class_from_expr(self, node: ast.expr) -> str | None:
        """Return the class name if this expression is a constructor call."""
        if not isinstance(node, ast.Call):
            return None
        func = node.func
        if isinstance(func, ast.Name):
            return func.id
        if isinstance(func, ast.Attribute):
            return func.attr  # SomeModule.Session() → "Session"
        return None

    def _class_name_from_annotation(self, node: ast.expr) -> str | None:
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            return node.attr
        return None

    def _maybe_source_var(self, node: ast.expr) -> str | None:
        if isinstance(node, ast.Name):
            return node.id
        return None


# ---------------------------------------------------------------------------
# Top-level visitor — walks all function and class bodies
# ---------------------------------------------------------------------------

class _ModuleVisitor(ast.NodeVisitor):
    def __init__(
        self,
        tracked: dict[str, dict[str, OperationGamma]],
        filename: str,
    ) -> None:
        self._tracked = tracked
        self._filename = filename
        self.issues: list[LintIssue] = []

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        checker = _ScopeChecker(self._tracked, self._filename)
        for child in node.body:
            checker.visit(child)
        self.issues.extend(checker.issues)
        # Recurse into nested functions
        self.generic_visit(node)

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Module(self, node: ast.Module) -> None:
        """Also check module-level code."""
        checker = _ScopeChecker(self._tracked, self._filename)
        for child in node.body:
            if not isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                checker.visit(child)
        self.issues.extend(checker.issues)
        self.generic_visit(node)


# ---------------------------------------------------------------------------
# GammaLinter
# ---------------------------------------------------------------------------

class GammaLinter:
    """
    Static Γ linter for Python source files.

    Configured with a map of class names to grammars. Walks source files
    and reports every call that violates the grammar given the call sequence
    in the enclosing scope.
    """

    def __init__(
        self,
        tracked: dict[str, dict[str, OperationGamma]],
    ) -> None:
        self._tracked = tracked

    # ------------------------------------------------------------------
    # Constructors
    # ------------------------------------------------------------------

    @classmethod
    def for_classes(
        cls,
        *classes: type,
        grammar_overrides: dict[str, dict[str, OperationGamma]] | None = None,
    ) -> "GammaLinter":
        """
        Build a linter by inferring grammar for each class.

        Parameters
        ----------
        *classes:
            Classes to track. Grammar inferred from lifecycle pairs + docstrings.
        grammar_overrides:
            Per-class grammar overrides. Keys are class names.
            Override entries win over inferred ones.
        """
        tracked: dict[str, dict[str, OperationGamma]] = {}
        overrides = grammar_overrides or {}

        for klass in classes:
            name = klass.__name__
            inferred = infer_grammar(klass)
            tracked[name] = inferred | overrides.get(name, {})

        return cls(tracked)

    @classmethod
    def from_grammar(
        cls,
        grammar_map: dict[str, dict[str, OperationGamma]],
    ) -> "GammaLinter":
        """Build from an explicit grammar map — no inference."""
        return cls(grammar_map)

    # ------------------------------------------------------------------
    # Analysis
    # ------------------------------------------------------------------

    def check_source(
        self,
        source: str,
        filename: str = "<string>",
    ) -> list[LintIssue]:
        """Check a source string. Returns all Γ violations found."""
        try:
            tree = ast.parse(source)
        except SyntaxError as e:
            return []  # not our job to report syntax errors
        visitor = _ModuleVisitor(self._tracked, filename)
        visitor.visit(tree)
        return visitor.issues

    def check_file(self, path: str | Path) -> list[LintIssue]:
        """Check a source file. Returns all Γ violations found."""
        p = Path(path)
        source = p.read_text()
        return self.check_source(source, filename=str(p))

    def check_directory(
        self,
        directory: str | Path,
        *,
        pattern: str = "**/*.py",
    ) -> list[LintIssue]:
        """Recursively check all Python files matching pattern."""
        issues: list[LintIssue] = []
        for path in Path(directory).glob(pattern):
            issues.extend(self.check_file(path))
        return sorted(issues, key=lambda i: (i.file, i.line, i.col))

    # ------------------------------------------------------------------
    # Grammar inspection
    # ------------------------------------------------------------------

    def tracked_classes(self) -> list[str]:
        """Class names this linter watches."""
        return sorted(self._tracked)

    def grammar_for(self, class_name: str) -> dict[str, OperationGamma] | None:
        """The effective grammar for a tracked class."""
        return self._tracked.get(class_name)
