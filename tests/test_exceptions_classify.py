"""Tests for exception classification — pure Python, no Nix daemon needed.

Covers every regex pattern in _CLASSIFIERS plus ANSI stripping,
fallback behavior, and the from_response() factory.
"""

from __future__ import annotations

import pytest

from nanopynix.exceptions import (
    AssertionError_,
    EvalError,
    InfiniteRecursionError,
    MissingArgumentError,
    NixError,
    ParseError,
    RestrictedPathError,
    StoreError,
    ThrownError,
    TypeError_,
    UndefinedVarError,
    UsageError,
    from_response,
)


# ════════════════════════════════════════════════════════════════════
# Exception hierarchy
# ════════════════════════════════════════════════════════════════════


def test_nix_error_repr():
    e = NixError("SomeType", "the message")
    assert repr(e) == "NixError(error_type='SomeType', msg='the message')"


def test_nix_error_str():
    e = NixError("SomeType", "the message")
    assert str(e) == "[SomeType] the message"


def test_nix_error_defaults():
    e = NixError("T", "")
    assert e.error_type == "T"
    assert e.msg == ""
    assert e.raw == ""
    assert e.info is None


def test_nix_error_kwargs():
    e = NixError("T", "msg", raw="traceback", info={"traces": []})
    assert e.raw == "traceback"
    assert e.info == {"traces": []}


def test_nix_error_ansi_helpers_preserve_raw_and_newlines():
    e = NixError("T", "\x1b[31mline 1\x1b[0m\nline 2", raw="\x1b[35mtrace\x1b[0m\nraw")

    assert e.msg == "\x1b[31mline 1\x1b[0m\nline 2"
    assert e.raw == "\x1b[35mtrace\x1b[0m\nraw"
    assert e.msg_without_ansi == "line 1\nline 2"
    assert e.raw_without_ansi == "trace\nraw"


def test_hierarchy():
    assert issubclass(StoreError, NixError)
    assert issubclass(EvalError, NixError)
    assert issubclass(TypeError_, EvalError)
    assert issubclass(AssertionError_, EvalError)
    assert issubclass(UndefinedVarError, EvalError)
    assert issubclass(ThrownError, EvalError)
    assert issubclass(InfiniteRecursionError, EvalError)
    assert issubclass(RestrictedPathError, EvalError)
    assert issubclass(MissingArgumentError, EvalError)
    assert issubclass(ParseError, NixError)
    assert issubclass(UsageError, NixError)


def test_subclass_repr():
    e = StoreError("StoreError", "path is not valid")
    assert repr(e) == "StoreError(error_type='StoreError', msg='path is not valid')"


# ════════════════════════════════════════════════════════════════════
# _classify — each regex pattern
# ════════════════════════════════════════════════════════════════════

# Direct access to the private classifier for precise testing.
from nanopynix.exceptions import _classify


# ── Eval error patterns ─────────────────────────────────────────


def test_classify_undefined_variable():
    cls, name = _classify("error: undefined variable 'foo'", "EvalError")
    assert cls is UndefinedVarError
    assert name == "UndefinedVarError"


def test_classify_infinite_recursion():
    cls, name = _classify("infinite recursion encountered while evaluating", "EvalError")
    assert cls is InfiniteRecursionError
    assert name == "InfiniteRecursionError"


def test_classify_assertion_failed():
    cls, name = _classify("assertion 1 == 2 failed", "EvalError")
    assert cls is AssertionError_
    assert name == "AssertionError"


def test_classify_restricted_path_absolute():
    cls, name = _classify("access to absolute path '/etc/shadow' is forbidden in pure eval mode", "EvalError")
    assert cls is RestrictedPathError
    assert name == "RestrictedPathError"


def test_classify_restricted_path_uri():
    cls, name = _classify("access to URI 'https://example.com' is forbidden", "EvalError")
    assert cls is RestrictedPathError
    assert name == "RestrictedPathError"


def test_classify_thrown_error():
    cls, name = _classify("threw an error while evaluating the expression", "EvalError")
    assert cls is ThrownError
    assert name == "ThrownError"


def test_classify_thrown_error_variant():
    cls, name = _classify("this threw error: division by zero", "EvalError")
    assert cls is ThrownError
    assert name == "ThrownError"


def test_classify_type_error_expected():
    cls, name = _classify("expected a string but found an integer", "EvalError")
    assert cls is TypeError_
    assert name == "TypeError"


def test_classify_type_error_expected_an():
    cls, name = _classify("expected an integer but got a float", "EvalError")
    assert cls is TypeError_
    assert name == "TypeError"


def test_classify_type_error_cannot_coerce():
    cls, name = _classify("cannot coerce a function to a string", "EvalError")
    assert cls is TypeError_
    assert name == "TypeError"


def test_classify_type_error_cannot_add():
    cls, name = _classify("cannot add a string to an integer", "EvalError")
    assert cls is TypeError_
    assert name == "TypeError"


def test_classify_type_error_cannot_compare():
    cls, name = _classify("cannot compare a set with a list", "EvalError")
    assert cls is TypeError_
    assert name == "TypeError"


def test_classify_type_error_but_found():
    cls, name = _classify("value is a function but found a string", "EvalError")
    assert cls is TypeError_
    assert name == "TypeError"


def test_classify_builtin_not_found():
    cls, name = _classify("builtin 'foobar' not found", "EvalError")
    assert cls is EvalError
    assert name == "EvalError"


def test_classify_attribute_missing():
    cls, name = _classify("attribute 'foo' missing", "EvalError")
    assert cls is EvalError
    assert name == "EvalError"


def test_classify_missing_argument():
    cls, name = _classify("function 'foo' called without required argument 'bar'", "EvalError")
    assert cls is MissingArgumentError
    assert name == "MissingArgumentError"


def test_classify_integer_overflow():
    cls, name = _classify("integer overflow in expression", "EvalError")
    assert cls is EvalError
    assert name == "EvalError"


def test_classify_not_absolute_path():
    cls, name = _classify("'relative/path' doesn't represent an absolute path", "EvalError")
    assert cls is EvalError
    assert name == "EvalError"


# ── Parse error patterns ────────────────────────────────────────


def test_classify_syntax_error():
    cls, name = _classify("syntax error, unexpected '}'", "EvalError")
    assert cls is ParseError
    assert name == "ParseError"


def test_classify_parse_error():
    cls, name = _classify("parse error: unexpected end of file", "EvalError")
    assert cls is ParseError
    assert name == "ParseError"


def test_classify_unexpected_token():
    cls, name = _classify("unexpected token ';'", "EvalError")
    assert cls is ParseError
    assert name == "ParseError"


def test_classify_unexpected_eof():
    cls, name = _classify("unexpected EOF in string literal", "EvalError")
    assert cls is ParseError
    assert name == "ParseError"


# ── Store error patterns ────────────────────────────────────────


def test_classify_store_invalid_path():
    cls, name = _classify("path '/nix/store/00000000000000000000000000000000-foo' is not valid", "Error")
    assert cls is StoreError
    assert name == "StoreError"


def test_classify_store_not_in_store():
    cls, name = _classify("path '/tmp/foo' is not in the Nix store", "Error")
    assert cls is StoreError
    assert name == "StoreError"


def test_classify_hash_mismatch():
    cls, name = _classify("hash mismatch in fixed-output derivation", "Error")
    assert cls is StoreError
    assert name == "StoreError"


def test_classify_does_not_exist():
    cls, name = _classify("path '/nix/store/...' does not exist", "Error")
    assert cls is StoreError
    assert name == "StoreError"


def test_classify_lacks_signature():
    cls, name = _classify("path lacks a signature", "Error")
    assert cls is StoreError
    assert name == "StoreError"


def test_classify_not_valid_derivation():
    cls, name = _classify("'foo' is not a valid derivation", "Error")
    assert cls is StoreError
    assert name == "StoreError"


# ── Usage error patterns ────────────────────────────────────────


def test_classify_invalid_value():
    cls, name = _classify("invalid value 'xyz' for setting 'build-cores': expected an integer", "Error")
    assert cls is UsageError
    assert name == "UsageError"


def test_classify_empty_path_setting():
    cls, name = _classify("setting 'substituters' is a path and paths cannot be empty", "Error")
    assert cls is UsageError
    assert name == "UsageError"


# ── System / fallthrough ────────────────────────────────────────


def test_classify_error_ignored():
    cls, name = _classify("some error (ignored)", "Error")
    assert cls is NixError
    assert name == "Error"


# ── Fallback (no pattern matches) ───────────────────────────────


def test_classify_fallback():
    cls, name = _classify("completely unknown error message", "InternalError")
    assert cls is NixError
    assert name == "InternalError"


# ── ANSI escape stripping ───────────────────────────────────────


def test_classify_ansi_stripping():
    """ANSI escape codes are removed before classification."""
    cls, name = _classify("\x1b[31m\x1b[1massertion 1 == 2 failed\x1b[0m", "EvalError")
    assert cls is AssertionError_
    assert name == "AssertionError"


# ════════════════════════════════════════════════════════════════════
# from_response — public factory
# ════════════════════════════════════════════════════════════════════


def test_from_response_classifies():
    e = from_response("EvalError", "undefined variable 'x'")
    assert isinstance(e, UndefinedVarError)
    assert e.error_type == "UndefinedVarError"
    assert e.msg == "undefined variable 'x'"
    assert e.raw == ""
    assert e.info is None


def test_from_response_passes_raw_and_info():
    e = from_response("SomeType", "msg", raw="traceback text", info={"level": 0})
    assert e.raw == "traceback text"
    assert e.info == {"level": 0}


def test_from_response_fallback_keeps_type_name():
    e = from_response("CustomNixError", "a very unusual error")
    assert isinstance(e, NixError)
    assert e.error_type == "CustomNixError"


# ════════════════════════════════════════════════════════════════════
# Pattern ordering — more specific before less specific
# ════════════════════════════════════════════════════════════════════


def test_ordering_missing_argument_before_expected():
    """MissingArgumentError must match before TypeError_ 'expected' pattern."""
    cls, name = _classify(
        "function 'f' called without required argument 'x', expected an integer",
        "EvalError",
    )
    assert cls is MissingArgumentError  # not TypeError_


def test_ordering_undefined_before_generic():
    """UndefinedVarError must match before patterns like 'does not exist'."""
    cls, name = _classify("undefined variable 'x' does not exist", "EvalError")
    assert cls is UndefinedVarError  # not StoreError ("does not exist")
