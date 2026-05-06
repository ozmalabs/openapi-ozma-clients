"""
TypeGenerator — generate type-valid Python values from type annotations.

The Python-object equivalent of _SchemaGenerator. Where _SchemaGenerator
works from JSON Schema fragments, TypeGenerator works from Python type hints:
int, str, list[X], dict[K, V], Optional[X], Union[X, Y], Literal["a"],
Pydantic models, dataclasses, Enums.

Used by GammaPyMock to produce valid return values without running real code.
"""
from __future__ import annotations

import dataclasses
import enum
import inspect
import typing
from typing import Any, get_type_hints


def _origin(tp: Any) -> Any:
    return getattr(tp, "__origin__", None)


def _args(tp: Any) -> tuple:
    return getattr(tp, "__args__", ()) or ()


class TypeGenerator:
    """Generate plausible values from Python type annotations."""

    def __init__(self) -> None:
        self._counter = 0

    def _next_id(self) -> int:
        self._counter += 1
        return self._counter

    def generate(self, tp: Any, hints: dict | None = None) -> Any:
        """Return a value that satisfies the type annotation tp."""
        h = hints or {}

        if tp is None or tp is type(None) or tp is inspect.Parameter.empty:
            return None

        # Unwrap typing aliases
        origin = _origin(tp)
        args = _args(tp)

        # Union / Optional
        if origin is typing.Union:
            for a in args:
                if a is not type(None):
                    return self.generate(a, hints)
            return None

        # list / List
        if origin is list:
            inner = args[0] if args else str
            return [self.generate(inner, hints)]

        # tuple / Tuple
        if origin is tuple:
            if args:
                return tuple(self.generate(a, hints) for a in args)
            return ()

        # dict / Dict
        if origin is dict:
            k_tp = args[0] if len(args) > 0 else str
            v_tp = args[1] if len(args) > 1 else Any
            return {self.generate(k_tp): self.generate(v_tp)}

        # Literal
        if origin is typing.Literal:
            state_hint = h.get("_state")
            if state_hint and state_hint in args:
                return state_hint
            return args[0] if args else None

        # Any
        if tp is Any:
            return {}

        # Primitives
        if tp is int:
            return h.get("_id", self._next_id())
        if tp is str:
            return h.get("_str", "")
        if tp is float:
            return 1.0
        if tp is bool:
            return True
        if tp is bytes:
            return b""

        # None-type
        if tp is type(None):
            return None

        # Enum
        if isinstance(tp, type) and issubclass(tp, enum.Enum):
            state_hint = h.get("_state")
            if state_hint:
                for member in tp:
                    if member.value == state_hint or member.name == state_hint:
                        return member
            return next(iter(tp))

        # Pydantic model (v2 and v1)
        if isinstance(tp, type) and hasattr(tp, "model_fields"):
            return self._generate_pydantic_v2(tp, hints)

        if isinstance(tp, type) and hasattr(tp, "__fields__"):
            return self._generate_pydantic_v1(tp, hints)

        # Dataclass
        if isinstance(tp, type) and dataclasses.is_dataclass(tp):
            return self._generate_dataclass(tp, hints)

        # Plain class with __init__ annotations
        if isinstance(tp, type):
            return self._generate_plain_class(tp, hints)

        return None

    def _generate_pydantic_v2(self, model: type, hints: dict | None) -> Any:
        kwargs: dict[str, Any] = {}
        h = hints or {}
        for name, field_info in model.model_fields.items():
            annotation = field_info.annotation
            if name in ("id",):
                kwargs[name] = h.get("_id", self._next_id())
            elif name in ("status", "state", "phase", "lifecycle") and "_state" in h:
                kwargs[name] = h["_state"]
            else:
                kwargs[name] = self.generate(annotation, hints)
        try:
            return model(**kwargs)
        except Exception:
            return kwargs

    def _generate_pydantic_v1(self, model: type, hints: dict | None) -> Any:
        kwargs: dict[str, Any] = {}
        h = hints or {}
        for name, field in model.__fields__.items():
            annotation = field.outer_type_
            if name in ("id",):
                kwargs[name] = h.get("_id", self._next_id())
            elif name in ("status", "state", "phase") and "_state" in h:
                kwargs[name] = h["_state"]
            else:
                kwargs[name] = self.generate(annotation, hints)
        try:
            return model(**kwargs)
        except Exception:
            return kwargs

    def _generate_dataclass(self, klass: type, hints: dict | None) -> Any:
        kwargs: dict[str, Any] = {}
        for f in dataclasses.fields(klass):
            kwargs[f.name] = self.generate(f.type if isinstance(f.type, type) else str, hints)
        try:
            return klass(**kwargs)
        except Exception:
            return kwargs

    def _generate_plain_class(self, klass: type, hints: dict | None) -> Any:
        try:
            type_hints = get_type_hints(klass.__init__)
        except Exception:
            type_hints = {}
        kwargs = {}
        sig = inspect.signature(klass.__init__)
        for name, param in sig.parameters.items():
            if name == "self":
                continue
            if param.default is not inspect.Parameter.empty:
                continue  # optional — skip
            tp = type_hints.get(name, str)
            kwargs[name] = self.generate(tp, hints)
        try:
            return klass(**kwargs)
        except Exception:
            return None
