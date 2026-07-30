"""Tests for exception classification — pure Python, no Nix daemon needed.

Covers every regex pattern in _CLASSIFIERS plus ANSI stripping,
fallback behavior, and the from_response() factory.
"""

from __future__ import annotations

from nanopynix.exceptions import (
    _WIRE_EXCEPTION_TYPES,  # type: ignore[reportPrivateUsage] -- the allowlist this file pins
    BuildHashMismatchError,
    EvalError,
    EvalHashMismatchError,
    HashMismatchError,
    InfiniteRecursionError,
    MissingArgumentError,
    NixAssertionError,
    NixError,
    NixTypeError,
    ParseError,
    RestrictedPathError,
    SettingNotLiveError,
    StoreError,
    ThrownError,
    UndefinedVarError,
    UsageError,
    _classify,  # type: ignore[reportPrivateUsage] -- test imports private classifier
    exception_from_wire,
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
    assert issubclass(NixTypeError, EvalError)
    assert issubclass(NixAssertionError, EvalError)
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
    assert cls is NixAssertionError
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
    assert cls is NixTypeError
    assert name == "TypeError"


def test_classify_type_error_expected_an():
    cls, name = _classify("expected an integer but got a float", "EvalError")
    assert cls is NixTypeError
    assert name == "TypeError"


def test_classify_type_error_cannot_coerce():
    cls, name = _classify("cannot coerce a function to a string", "EvalError")
    assert cls is NixTypeError
    assert name == "TypeError"


def test_classify_type_error_cannot_add():
    cls, name = _classify("cannot add a string to an integer", "EvalError")
    assert cls is NixTypeError
    assert name == "TypeError"


def test_classify_type_error_cannot_compare():
    cls, name = _classify("cannot compare a set with a list", "EvalError")
    assert cls is NixTypeError
    assert name == "TypeError"


def test_classify_type_error_but_found():
    cls, name = _classify("value is a function but found a string", "EvalError")
    assert cls is NixTypeError
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
    """A fixed-output mismatch classifies to the build-time hash variant.

    Previously this returned a bare ``StoreError``. It now resolves to the
    specific type, which is still a ``StoreError`` -- so existing
    ``except StoreError`` callers are unaffected -- while also being a
    ``HashMismatchError``, letting fixed-output update logic catch the
    build-time and eval-time (``builtins.fetchurl``) variants uniformly.
    """
    cls, name = _classify("hash mismatch in fixed-output derivation", "Error")
    assert cls is BuildHashMismatchError
    assert name == "HashMismatchError"
    assert issubclass(cls, StoreError)
    assert issubclass(cls, HashMismatchError)


def test_classify_hash_mismatch_from_a_fetchurl_is_an_eval_error():
    """``builtins.fetchurl`` fails during evaluation, not during a build."""
    cls, name = _classify("hash mismatch in file downloaded from 'file:///tmp/x'", "Error")
    assert cls is EvalHashMismatchError
    assert name == "HashMismatchError"
    assert issubclass(cls, EvalError)
    assert issubclass(cls, HashMismatchError)


def test_classify_bare_hash_mismatch_stays_neutral():
    """Boundary C: the daemon protocol discarded which kind it was."""
    cls, _ = _classify("hash mismatch somewhere unspecified", "Error")
    assert cls is HashMismatchError
    assert not issubclass(cls, StoreError)
    assert not issubclass(cls, EvalError)


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
    assert cls is NixAssertionError
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
# exception_from_wire — the allowlist for boundary B
# ════════════════════════════════════════════════════════════════════

# Every class the rpc client will build from a name the worker sent.
#
# Pinned as a literal on purpose. The table is derived, so a new exception
# class in `exceptions.py` joins it silently, and "may a worker construct this
# from a message?" is a decision somebody has to make rather than inherit.
# Update this list when you have made it.
#
# `WrongNixTypeError` is deliberately absent: its `__init__` is keyword-only,
# so a message alone cannot rebuild it. The six builtins are exactly what
# `rpc/worker/` and `nanopynix/_core/` raise.
WIRE_CLASSES = {
    "BadStorePathError",
    "BuildError",
    "BuildHashMismatchError",
    "BuildTimedOutError",
    "CachedBuildFailureError",
    "DependencyFailedError",
    "EvalError",
    "EvalHashMismatchError",
    "EvalSessionClosedError",
    "ForeignValueError",
    "HashMismatchError",
    "IndexError",
    "InfiniteRecursionError",
    "InputRejectedError",
    "InvalidPathError",
    "KeyError",
    "ListIndexError",
    "LockedFlakeReleasedError",
    "LogLimitExceededError",
    "MiscBuildError",
    "MissingArgumentError",
    "MissingAttributeError",
    "NixAssertionError",
    "NixError",
    "NixSysError",
    "NixTypeError",
    "NoSubstitutersError",
    "NotDeterministicError",
    "ObjectLifetimeError",
    "ObjectMisuseError",
    "OutputRejectedError",
    "ParseError",
    "PermanentBuildError",
    "RestrictedPathError",
    "RuntimeError",
    "SessionClosedError",
    "SettingNotLiveError",
    "StoreClosedError",
    "StoreError",
    "ThrownError",
    "TimeoutError",
    "TransientBuildError",
    "TypeError",
    "UndefinedVarError",
    "UnimplementedError",
    "UnresolvedValueError",
    "UnsupportedError",
    "UsageError",
    "ValueError",
    "ValueReleasedError",
}


def test_the_wire_allowlist_is_what_this_file_says_it_is():
    assert set(_WIRE_EXCEPTION_TYPES) == WIRE_CLASSES


def test_the_allowlist_holds_only_classes_this_module_defines_and_the_named_builtins():
    """``__module__`` is the fence around a table built by walking subclasses.

    Without it a subclass declared in a test, or in a consumer of this
    library, would enter the table and make the resolution depend on which
    modules happened to be imported first.
    """
    outside = {
        name: cls.__module__
        for name, cls in _WIRE_EXCEPTION_TYPES.items()
        if cls.__module__ not in {"builtins", "nanopynix.exceptions"}
    }
    assert outside == {}


def test_no_base_exception_can_be_named():
    """An open resolver would let the payload construct ``SystemExit``."""
    for name in ("SystemExit", "KeyboardInterrupt", "BaseException", "GeneratorExit"):
        assert exception_from_wire(nix_type="", class_name=name, msg="boom") is None


def test_a_nix_type_is_resolved_exactly_as_the_prose_prefix_was():
    """The identity replaces the *source* of the name and nothing after it.

    Same class, same ``error_type``, same message as the prefix path produces
    for the same worker error -- including the refinement, which is what keeps
    a coarse ``EvalError`` from re-coarsening a type error.
    """
    from_identity = exception_from_wire(nix_type="EvalError", class_name="", msg="EvalError: cannot compare a b")
    from_prose = from_response("Unknown", "EvalError: cannot compare a b")

    assert type(from_identity) is type(from_prose) is NixTypeError
    assert isinstance(from_identity, NixError)
    assert (from_identity.error_type, from_identity.msg) == (from_prose.error_type, from_prose.msg)


def test_a_plain_class_is_built_from_the_message_alone():
    resolved = exception_from_wire(
        nix_type="",
        class_name="SettingNotLiveError",
        msg="SettingNotLiveError: pure-eval is read at construction",
    )

    assert type(resolved) is SettingNotLiveError
    assert str(resolved) == "pure-eval is read at construction"


def test_a_prefix_that_is_not_the_resolved_name_is_left_alone():
    """Exact match only. A message that merely holds a colon is not a prefix."""
    resolved = exception_from_wire(nix_type="", class_name="ValueError", msg="setting: not one we know")

    assert type(resolved) is ValueError
    assert str(resolved) == "setting: not one we know"


# ════════════════════════════════════════════════════════════════════
# Pattern ordering — more specific before less specific
# ════════════════════════════════════════════════════════════════════


def test_ordering_missing_argument_before_expected():
    """MissingArgumentError must match before NixTypeError 'expected' pattern."""
    cls, _name = _classify(
        "function 'f' called without required argument 'x', expected an integer",
        "EvalError",
    )
    assert cls is MissingArgumentError  # not NixTypeError


def test_ordering_undefined_before_generic():
    """UndefinedVarError must match before patterns like 'does not exist'."""
    cls, _name = _classify("undefined variable 'x' does not exist", "EvalError")
    assert cls is UndefinedVarError  # not StoreError ("does not exist")
