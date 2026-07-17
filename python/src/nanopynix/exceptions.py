"""Typed Nix exceptions — raised by WorkerPool for structured error handling.

Classifies errors by parsing the Nix error message string, since C++
exception types are not yet bound via nanobind (Phase B).  The classifier
matches known Nix error message patterns from the Nix source code.

Usage::

    try:
        await nix.store.query_path_info("/bad/path")
    except nanopynix.StoreError as e:
        print(e.error_type, e.msg)
    except nanopynix.EvalError as e:
        print(e.error_type, e.msg)
"""

from __future__ import annotations

import re
from typing import Any

from nanopynix_proto.nix.common import NixType
from strip_ansi import strip_ansi  # type: ignore[reportMissingTypeStubs] -- strip_ansi has no PEP 561 stubs

# ════════════════════════════════════════════════════════════════════
# Exception hierarchy
# ════════════════════════════════════════════════════════════════════


class NixError(RuntimeError):
    """Base for all Nix-originated errors from RPC calls.

    Attributes:
        error_type: the classified Nix error kind (e.g. ``"TypeError"``).
        msg: the error message string from Nix.
        raw: the full traceback string from the worker subprocess.
        info: ErrorInfo dict (traces, suggestions, level, etc.) or None.
    """

    def __init__(self, error_type: str, msg: str, *, raw: str = "", info: dict[str, Any] | None = None) -> None:
        self.error_type = error_type
        self.msg = msg
        self.raw = raw
        self.info = info
        super().__init__(f"[{error_type}] {msg}")

    def __repr__(self) -> str:
        return f"{type(self).__name__}(error_type={self.error_type!r}, msg={self.msg!r})"

    @property
    def msg_without_ansi(self) -> str:
        """Error message with ANSI color escapes removed, preserving other text."""

        return strip_ansi(self.msg)

    @property
    def raw_without_ansi(self) -> str:
        """Raw traceback text with ANSI color escapes removed."""

        return strip_ansi(self.raw)


class StoreError(NixError):
    """Errors from Nix store operations (invalid paths, hash mismatches, etc.)."""


class EvalError(NixError):
    """Errors from Nix expression evaluation."""


class NixTypeError(EvalError):
    """Type errors during evaluation (expected X but found Y)."""


class NixAssertionError(EvalError):
    """Assertion failures in Nix expressions."""


class UndefinedVarError(EvalError):
    """Undefined variable in Nix expression."""


class ThrownError(EvalError):
    """Nix ``throw`` or ``builtins.throw`` called."""


class InfiniteRecursionError(EvalError):
    """Infinite recursion in Nix evaluation."""


class RestrictedPathError(EvalError):
    """Access to a restricted path in pure evaluation mode."""


class MissingArgumentError(EvalError):
    """Missing function argument in Nix expression."""


class ParseError(NixError):
    """Nix expression parse error."""


class UsageError(NixError):
    """Invalid usage or configuration."""


class EvalProxyError(RuntimeError):
    """Base for master-side eval proxy misuse errors."""


class EvalSessionClosedError(EvalProxyError):
    """A proxy or eval session was used after its owning session closed."""


class EvalStateBusyError(EvalProxyError):
    """A session already owns its one permitted live EvalState."""


class ValueReleasedError(EvalProxyError):
    """A proxy wrapper was used after its remote value handle was released."""


class UnresolvedValueError(EvalProxyError):
    """A lazy child proxy was inspected before it had a remote handle."""


class WrongNixTypeError(EvalProxyError, TypeError):
    """A Nix value had a different type than the operation requires."""

    def __init__(self, *, expected: NixType | str, actual: NixType | str) -> None:
        self.expected = expected.name.lower() if isinstance(expected, NixType) else expected
        self.actual = actual.name.lower() if isinstance(actual, NixType) else actual
        super().__init__(f"Nix value is {self.actual}, expected {self.expected}")


class ForeignValueError(EvalProxyError, ValueError):
    """A value proxy from another eval session was used where a local value is required."""


class NixCoercionError(EvalProxyError, ValueError):
    """A forced Nix value could not be converted by a lenient proxy coercion."""


# ════════════════════════════════════════════════════════════════════
# Classification — string-based, matches Nix error message patterns
# ════════════════════════════════════════════════════════════════════

# Tuples of (regex, Python class, error_type_name).
# Ordered: more specific patterns first.
_CLASSIFIERS: list[tuple[re.Pattern[str], type[NixError], str]] = [
    # ── Eval errors ──────────────────────────────────────────────
    (re.compile(r"undefined variable"), UndefinedVarError, "UndefinedVarError"),
    (re.compile(r"infinite recursion"), InfiniteRecursionError, "InfiniteRecursionError"),
    (re.compile(r"assertion .+ failed"), NixAssertionError, "AssertionError"),
    (re.compile(r"access to (absolute path|URI).+forbidden"), RestrictedPathError, "RestrictedPathError"),
    (re.compile(r"threw.*(?:while|error)"), ThrownError, "ThrownError"),
    (re.compile(r"function .+ called without required argument"), MissingArgumentError, "MissingArgumentError"),
    (re.compile(r"invalid value.*expected"), UsageError, "UsageError"),
    (re.compile(r"expected (?:a |an )"), NixTypeError, "TypeError"),
    (re.compile(r"cannot coerce .+ to a"), NixTypeError, "TypeError"),
    (re.compile(r"cannot add .+ to"), NixTypeError, "TypeError"),
    (re.compile(r"cannot compare"), NixTypeError, "TypeError"),
    (re.compile(r"but found"), NixTypeError, "TypeError"),
    (re.compile(r"builtin .+ not found"), EvalError, "EvalError"),
    (re.compile(r"attribute .+ missing"), EvalError, "EvalError"),
    (re.compile(r"integer overflow"), EvalError, "EvalError"),
    (re.compile(r"doesn't represent an absolute path"), EvalError, "EvalError"),
    # ── Parse errors ─────────────────────────────────────────────
    (re.compile(r"syntax error|parse error|unexpected (?:token|EOF)"), ParseError, "ParseError"),
    # ── Store errors ─────────────────────────────────────────────
    (re.compile(r"path .+ is not valid"), StoreError, "StoreError"),
    (re.compile(r"path .+ is not in the Nix store"), StoreError, "StoreError"),
    (re.compile(r"hash mismatch"), StoreError, "StoreError"),
    (re.compile(r"does not exist"), StoreError, "StoreError"),
    (re.compile(r"lacks a signature"), StoreError, "StoreError"),
    (re.compile(r"is not a valid derivation"), StoreError, "StoreError"),
    # ── Usage errors ─────────────────────────────────────────────
    (re.compile(r"setting .+ is a path and paths cannot be empty"), UsageError, "UsageError"),
    # ── System errors ────────────────────────────────────────────
    (re.compile(r"error \(ignored\)"), NixError, "Error"),  # swallowed by ignoreException
]


def _classify(msg: str, fallback_type: str) -> tuple[type[NixError], str]:
    """Determine the Python exception class and Nix error type from a message.

    Returns ``(exception_class, error_type_name)``.
    """
    clean = strip_ansi(msg)
    for pattern, cls, name in _CLASSIFIERS:
        if pattern.search(clean):
            return cls, name
    # Fallback: use the Python type name from the worker
    return NixError, fallback_type


def from_response(error_type: str, msg: str, *, raw: str = "", info: dict[str, Any] | None = None) -> NixError:
    """Factory: create the right NixError subclass from a worker error response.

    Called by ``_pool.py`` when it receives an ``{"type":"error",...}`` response.
    """
    cls, classified_type = _classify(msg, error_type)
    return cls(classified_type, msg, raw=raw, info=info)


__all__ = [
    "EvalError",
    "EvalProxyError",
    "EvalSessionClosedError",
    "ForeignValueError",
    "InfiniteRecursionError",
    "MissingArgumentError",
    "NixAssertionError",
    "NixCoercionError",
    "NixError",
    "NixTypeError",
    "ParseError",
    "RestrictedPathError",
    "StoreError",
    "ThrownError",
    "UndefinedVarError",
    "UnresolvedValueError",
    "UsageError",
    "ValueReleasedError",
    "WrongNixTypeError",
    "from_response",
]
