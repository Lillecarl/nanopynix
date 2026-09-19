"""Worker-side Python primop helpers.

.. note::
   Primop registration (both these YAML primops and custom
   ``Session(primops=..., primop_callables=...)`` registration) requires
   Nix >= 2.32. Every supported Nix meets that, because ``supportedNixFloor``
   is 2.34. Registration is broken on Nix 2.31 and is not expected to be fixed
   there, which matters only if you link that version yourself.
"""

from __future__ import annotations

import math
import re
from decimal import Decimal
from typing import TYPE_CHECKING, Any, cast

import yaml
from pydantic import TypeAdapter, ValidationError

from nanopynix._typechecking import BEARTYPING
from nanopynix.models import JsonValue, PrimOpSpec

if TYPE_CHECKING or BEARTYPING:
    from collections.abc import Iterable

_JsonValue: TypeAdapter[JsonValue] = TypeAdapter(JsonValue)


def _yaml12_loader() -> type[Any]:
    # CSafeLoader (libyaml-backed) instead of the pure-Python SafeLoader: the
    # C scanner/parser/composer still calls back into the same Python-level
    # Resolver/SafeConstructor machinery add_implicit_resolver/add_constructor
    # mutate, so this custom-tag setup carries over unchanged -- just faster
    # (easykubenix's ekn makes the same swap on the dump side in its own
    # gitops.py, benchmarked ~7.5x).
    class Loader(yaml.CSafeLoader):  # type: ignore[reportUnknownBaseType] -- PyYAML stubs may be incomplete
        pass

    legacy_tags = {
        "tag:yaml.org,2002:bool",
        "tag:yaml.org,2002:float",
        "tag:yaml.org,2002:int",
    }
    Loader.yaml_implicit_resolvers = {  # type: ignore[reportUnknownMemberType] -- yaml_implicit_resolvers class attribute not in stubs
        ch: [(tag, regexp) for tag, regexp in resolvers if tag not in legacy_tags]
        for ch, resolvers in yaml.CSafeLoader.yaml_implicit_resolvers.items()  # type: ignore[reportUnknownMemberType] -- yaml_implicit_resolvers not in stubs
    }

    Loader.add_implicit_resolver(  # type: ignore[reportUnknownMemberType] -- yaml.Loader methods may not have complete stubs
        "tag:yaml.org,2002:bool",
        re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$"),
        list("tTfF"),
    )
    Loader.add_implicit_resolver(  # type: ignore[reportUnknownMemberType] -- yaml.Loader methods may not have complete stubs
        "tag:yaml.org,2002:int",
        re.compile(r"^[-+]?(?:[0-9]+|0o[0-7]+|0x[0-9a-fA-F]+)$"),
        list("-+0123456789"),
    )
    Loader.add_implicit_resolver(  # type: ignore[reportUnknownMemberType] -- yaml.Loader methods may not have complete stubs
        "tag:yaml.org,2002:float",
        re.compile(
            r"""^(?:[-+]?(?:[0-9][0-9_]*)\.[0-9_]*(?:[eE][-+]?[0-9]+)?
            |[-+]?(?:[0-9][0-9_]*)(?:[eE][-+]?[0-9]+)
            |[-+]?\.(?:inf|Inf|INF)
            |\.(?:nan|NaN|NAN))$""",
            re.VERBOSE,
        ),
        list("-+0123456789."),
    )
    Loader.add_constructor("tag:yaml.org,2002:int", _construct_yaml12_int)  # type: ignore[reportUnknownMemberType] -- yaml.Loader methods may not have complete stubs
    return Loader


# A float a YAML 1.2 reader resolves and a YAML 1.1 reader does not.
#
# 1.1's float production requires a decimal point before the exponent, and
# requires the exponent to carry a sign. So `1.5e+06` is a float in both, while
# `1e+06`, `1e6` and `1.5e6` are floats in 1.2 and plain strings in 1.1.
#
# That gap is not academic. Helm renders a chart value through Go's `%v` on a
# float64, which goes to scientific notation from 1e6 upwards, so a chart with
# `priorityClass.value: 1000000` -- topolvm, and most charts that set one --
# emits `value: 1e+06`. Read as 1.1 that is the string "1e+06", and the API
# server refuses it: ".value: expected numeric (int or float), got string".
_YAML12_ONLY_FLOAT = re.compile(
    r"^[-+]?(?:[0-9][0-9_]*(?:\.[0-9_]*)?|\.[0-9_]+)[eE][-+]?[0-9]+$",
)

# YAML 1.2's octal form, which 1.1's integer production has no alternative for.
#
# go-yaml reads `0o755` as 493. `_BlockStyleDumper` below already knows this:
# it registers the same pattern, so a string that reads "0o17" goes out quoted
# and does not come back as a number. The reader is the half that missed it.
_YAML12_ONLY_INT = re.compile(r"^[-+]?0o[0-7_]+$")

# Where Go's JSON encoder changes from the plain form to the exponent form.
#
# go-yaml reads `1e+06` as a float64, and then Go writes that float64 as
# `1000000`, because `encoding/json` prints an integral float64 with no
# decimal point below 1e21. The number reaches Nix as an integer. This reader
# gives a Python float, which reaches Nix as a float and goes back out as
# `1000000.0` -- into a field the API server types as int32.
#
# Kubernetes makes the same conversion for the same reason:
# `k8s.io/apimachinery/pkg/util/json` turns an integral JSON number into an
# int64 before anything decodes it.
_GO_PLAIN_FLOAT_LIMIT = 1e21


# `int | float` and not `float`: beartype checks the annotation at run time,
# and an int is not an instance of float there.
def _construct_yaml11_float(loader: Any, node: Any) -> int | float:
    value: float = yaml.CSafeLoader.construct_yaml_float(loader, node)  # type: ignore[reportUnknownMemberType] -- PyYAML stubs may be incomplete
    if value.is_integer() and abs(value) < _GO_PLAIN_FLOAT_LIMIT:
        return int(value)
    return value


def _yaml11_loader() -> type[Any]:
    class Loader(yaml.CSafeLoader):  # type: ignore[reportUnknownBaseType] -- PyYAML stubs may be incomplete
        pass

    # Helm's dialect, which is what this parser is for, and which is neither
    # version cleanly. It keeps 1.1's integers, because `defaultMode: 0644`
    # means 420 and 1.2 reads the same text as 644. It adds 1.2's floats,
    # because of the exponent above, and 1.2's octal, because go-yaml resolves
    # both and this loader is a description of go-yaml.
    #
    # Appending is what makes this narrow. PyYAML tries a first character's
    # resolvers in order and takes the first match, so every scalar 1.1
    # already resolves -- every octal integer, every 1.1 float -- is decided
    # before this is reached. The only scalars whose type changes are ones 1.1
    # called strings and no producer here means as strings.
    Loader.add_implicit_resolver(  # type: ignore[reportUnknownMemberType] -- yaml.Loader methods may not have complete stubs
        "tag:yaml.org,2002:float",
        _YAML12_ONLY_FLOAT,
        list("-+0123456789."),
    )
    # The constructor needs no change. PyYAML's 1.1 `construct_yaml_int` gives
    # a leading-zero integer to `int(text, 8)`, and Python accepts the `0o`
    # prefix when it matches the base.
    Loader.add_implicit_resolver(  # type: ignore[reportUnknownMemberType] -- yaml.Loader methods may not have complete stubs
        "tag:yaml.org,2002:int",
        _YAML12_ONLY_INT,
        list("-+0"),
    )
    Loader.add_constructor("tag:yaml.org,2002:float", _construct_yaml11_float)

    # YAML 1.1's core schema resolves a bare, unquoted `=` scalar to the
    # special "value" type (historically a mapping's "default key" marker),
    # but PyYAML's SafeConstructor never registers a constructor for it --
    # only the non-safe Constructor does. Real-world YAML 1.1 producers (Helm
    # charts, generated CRDs -- e.g. prometheus-operator's Alertmanager CRD
    # enumerates `=` as a literal matcher operator) emit bare `=` as plain
    # string content and expect it to round-trip as such, so treat it the
    # same way `construct_yaml_str` does instead of raising.
    Loader.add_constructor(
        "tag:yaml.org,2002:value",
        yaml.CSafeLoader.construct_yaml_str,  # type: ignore[reportUnknownMemberType, reportUnknownArgumentType] -- yaml.Loader methods may not have complete stubs
    )
    return Loader


def _construct_yaml12_int(loader: Any, node: Any) -> int:
    value = loader.construct_scalar(node).replace("_", "")
    sign = -1 if value.startswith("-") else 1
    unsigned = value[1:] if value.startswith(("-", "+")) else value
    if unsigned.startswith("0o"):
        return sign * int(unsigned, 0)
    if unsigned.startswith("0x"):
        return sign * int(unsigned, 0)
    return sign * int(unsigned, 10)


_STR_TAG = "tag:yaml.org,2002:str"
_BOOL_TAG = "tag:yaml.org,2002:bool"
_INT_TAG = "tag:yaml.org,2002:int"
_FLOAT_TAG = "tag:yaml.org,2002:float"
_NULL_TAG = "tag:yaml.org,2002:null"
_MERGE_TAG = "tag:yaml.org,2002:merge"

# `resolveTable` in `resolve.go`. go-yaml reads the first character of a plain
# scalar and stops at once when that character is absent from this table. So
# `=`, `_1` and `yellow` are strings before any pattern runs.
_GO_HINT: dict[str, str] = {
    **dict.fromkeys("+-", "S"),
    **dict.fromkeys("0123456789", "D"),
    **dict.fromkeys("yYnNtTfFoO~", "M"),
    ".": ".",
}

# `resolveMapList` in `resolve.go`. These spellings and no others: `YeS` is a
# string, and so is `Y_ES`.
_GO_SCALAR_MAP: dict[str, tuple[str, Any]] = {}
for _go_value, _go_tag, _go_spellings in (
    (True, _BOOL_TAG, ("y", "Y", "yes", "Yes", "YES")),
    (True, _BOOL_TAG, ("true", "True", "TRUE")),
    (True, _BOOL_TAG, ("on", "On", "ON")),
    (False, _BOOL_TAG, ("n", "N", "no", "No", "NO")),
    (False, _BOOL_TAG, ("false", "False", "FALSE")),
    (False, _BOOL_TAG, ("off", "Off", "OFF")),
    (None, _NULL_TAG, ("", "~", "null", "Null", "NULL")),
    (math.nan, _FLOAT_TAG, (".nan", ".NaN", ".NAN")),
    (math.inf, _FLOAT_TAG, (".inf", ".Inf", ".INF", "+.inf", "+.Inf", "+.INF")),
    (-math.inf, _FLOAT_TAG, ("-.inf", "-.Inf", "-.INF")),
    # `resolveMapList` also holds `<<`, and `resolve()` can never reach it:
    # `resolveTable` has no `<`, so the hint is zero and the scalar is a
    # string. See `_go_resolve`, which answers `<<` before the hint.
):
    for _go_spelling in _go_spellings:
        _GO_SCALAR_MAP[_go_spelling] = (_go_tag, _go_value)

# `yamlStyleFloat` in `resolve.go`. It gates the float attempt for a scalar
# that starts with a digit or with a sign. go-yaml removes the underscores
# before it applies this pattern, so the pattern has none.
_GO_STYLE_FLOAT = re.compile(r"[-+]?(?:\.[0-9]+|[0-9]+(?:\.[0-9]*)?)(?:[eE][-+]?[0-9]+)?")

# The `.` branch of `resolve.go` gives the text to `strconv.ParseFloat` with
# the underscores still in it, and Go's own literal syntax permits one between
# two digits. So `.5_0` is 0.5 and `.5e` is a string.
_GO_DOT_FLOAT = re.compile(r"\.[0-9](?:_?[0-9])*(?:[eE][-+]?[0-9](?:_?[0-9])*)?")

_GO_DIGITS = {
    2: re.compile(r"[01]+"),
    8: re.compile(r"[0-7]+"),
    10: re.compile(r"[0-9]+"),
    16: re.compile(r"[0-9a-fA-F]+"),
}
_INT64_MIN = -(2**63)
_INT64_MAX = 2**63 - 1
_UINT64_MAX = 2**64 - 1

_GO_BASE_PREFIX = {"b": 2, "o": 8, "x": 16}
# The shortest text a base prefix can be part of: the zero, the letter and one
# digit. Go's `strconv` takes the same bound, so `0x` is not a hexadecimal.
_GO_PREFIXED_MINIMUM = 3


def _go_base(digits: str) -> tuple[int, str]:
    """The base that `strconv.ParseInt(text, 0, 64)` reads, and the digits."""
    if not digits.startswith("0"):
        return (10, digits)
    if len(digits) >= _GO_PREFIXED_MINIMUM:
        base = _GO_BASE_PREFIX.get(digits[1].lower())
        if base is not None:
            return (base, digits[2:])
    # A bare leading zero is octal. `0` alone leaves no digit and is a syntax
    # error, so the float attempt answers it instead.
    return (8, digits[1:])


def _go_parse_int(plain: str) -> int | None:
    """`strconv.ParseInt(plain, 0, 64)`, and `ParseUint` after it."""
    # Python's `int` accepts a Unicode digit and surrounding space. Go rejects
    # both, so every part below matches ASCII only.
    if not plain.isascii():
        return None
    digits = plain
    negative = digits.startswith("-")
    if digits[:1] in ("+", "-"):
        digits = digits[1:]
    base, digits = _go_base(digits)
    if not _GO_DIGITS[base].fullmatch(digits):
        return None
    value = int(digits, base)
    if negative:
        value = -value
    if _INT64_MIN <= value <= _INT64_MAX:
        return value
    # ParseInt reported a range error. go-yaml then calls ParseUint on the
    # same text, and ParseUint permits no sign at all.
    if plain[:1] not in ("+", "-") and value <= _UINT64_MAX:
        return value
    return None


def _go_parse_float(text: str, pattern: re.Pattern[str]) -> float | None:
    """`strconv.ParseFloat(text, 64)`, behind the pattern of its own branch."""
    if not text.isascii() or not pattern.fullmatch(text):
        return None
    value = float(text)
    # ParseFloat reports a range error for a value it cannot hold, and go-yaml
    # leaves that scalar a string. Python returns an infinity instead, so
    # `75.e993` needs this line to stay the string it is in Kubernetes.
    if not math.isfinite(value):
        return None
    return value


# `int | float` and not `float`: beartype checks the annotation at run time,
# and an int is not an instance of float there.
def _go_number(value: float) -> int | float:
    """A float64 as Go's JSON encoder writes it.

    `encoding/json` writes an integral float64 with no decimal point below
    1e21, so the number reaches Nix as an integer. `repr` gives the same
    shortest decimal that Go gives, and `Decimal` then keeps the digits that
    `int()` of the float drops: Go writes float64(2**64) as
    18446744073709552000.
    """
    if value.is_integer() and abs(value) < _GO_PLAIN_FLOAT_LIMIT:
        return int(Decimal(repr(value)))
    return value


def _go_resolve(text: str) -> tuple[str, Any]:
    """`resolve()` in `go.yaml.in/yaml/v2/resolve.go`, for an untyped target."""
    # go-yaml decides a merge in `decode.go`, in `isMerge`, and not here: it
    # asks whether a plain key is the text `<<`. PyYAML decides it from the
    # tag instead, so the tag has to say merge. The constructor gives the
    # string back, which is what `<<` in value position is to both readers.
    if text == "<<":
        return (_MERGE_TAG, "<<")
    hint = "N" if text == "" else _GO_HINT.get(text[0], "")
    if hint:
        item = _GO_SCALAR_MAP.get(text)
        if item is not None:
            return item
        # Base 60 is absent on purpose. go-yaml reads `1:30` as a string, and
        # YAML 1.2 dropped the notation.
        if hint == ".":
            value = _go_parse_float(text, _GO_DOT_FLOAT)
            if value is not None:
                return (_FLOAT_TAG, _go_number(value))
        elif hint in ("D", "S"):
            # go-yaml tries a timestamp first here, and this port leaves that
            # out. `decode.go` sets the original string into an `interface{}`
            # for a timestamp, and no scalar that `parseTimestamp` accepts
            # resolves to anything but a string anyway. The branch cannot
            # change an answer. Do not add it back.
            plain = text.replace("_", "")
            number = _go_parse_int(plain)
            if number is not None:
                return (_INT_TAG, number)
            value = _go_parse_float(plain, _GO_STYLE_FLOAT)
            if value is not None:
                return (_FLOAT_TAG, _go_number(value))
            # The `0b` fallback of `resolve.go` follows here. `ParseInt` with
            # base 0 already reads that prefix, so the fallback answers
            # nothing, and this port leaves it out.
    return (_STR_TAG, text)


def _construct_go_scalar(loader: Any, node: Any) -> Any:
    text: str = loader.construct_scalar(node)
    _tag, value = _go_resolve(text)
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError(
            f"YAML scalar {text!r} reads as {value}, and JSON holds no such number. "
            "Quote the scalar to keep it a string.",
        )
    return value


def _go_like_loader() -> type[Any]:
    """A loader that answers like go-yaml v2, and not like a YAML version.

    `sigs.k8s.io/yaml` reads with go-yaml v2, so the Kubernetes API server
    decodes with it and Helm renders through it. That dialect is neither YAML
    1.1 nor YAML 1.2: it reads `0644` as 420, which is 1.1, and `1e+06` as a
    number, which is 1.2.

    The loader replaces PyYAML's resolution of a plain scalar rather than
    adding patterns to it. One function answers the question, and that
    function is a port of one function of Go. Issue #307 reports what a
    description written from memory costs.
    """

    class Loader(yaml.CSafeLoader):  # type: ignore[reportUnknownBaseType] -- PyYAML stubs may be incomplete
        def resolve(self, kind: Any, value: Any, implicit: Any) -> str:
            # A plain scalar is the only scalar go-yaml resolves. A quoted one
            # is a string, and PyYAML gives the answer go-yaml gives for a
            # sequence and for a mapping.
            if kind is yaml.ScalarNode and implicit[0]:
                return _go_resolve(value)[0]
            return cast("str", super().resolve(kind, value, implicit))  # type: ignore[reportUnknownMemberType] -- PyYAML stubs may be incomplete

    # `str` and `null` keep PyYAML's constructors: the first returns the text
    # and the second returns None, whatever the spelling. Every other tag
    # needs Go's value, so `_go_resolve` answers again.
    for tag in (_BOOL_TAG, _INT_TAG, _FLOAT_TAG, _MERGE_TAG):
        Loader.add_constructor(tag, _construct_go_scalar)  # type: ignore[reportUnknownMemberType] -- yaml.Loader methods may not have complete stubs
    return Loader


def _validate_document(value: Any, builtin: str) -> JsonValue:
    try:
        result: JsonValue = _JsonValue.validate_python(value)  # type: ignore[reportUnknownVariableType] -- TypeAdapter returns Any
    except ValidationError as exc:
        raise ValueError(f"{builtin}: YAML document is not JSON-compatible: {exc}") from exc
    return result


def _validate_documents(values: Iterable[Any], builtin: str) -> list[JsonValue]:
    return [_validate_document(value, builtin) for value in values]


def _single_document(values: Iterable[Any], builtin: str, stream_builtin: str) -> JsonValue:
    docs = list(values)
    if len(docs) != 1:
        raise ValueError(
            f"{builtin}: expected exactly one YAML document, got {len(docs)}; "
            f"use {stream_builtin} for multi-document YAML",
        )
    return _validate_document(docs[0], builtin)


def _parse_error_message(exc: Exception) -> str:
    problem = getattr(exc, "problem", None)  # type: ignore[reportUnknownVariableType] -- dynamic attribute access on yaml exception
    mark = getattr(exc, "problem_mark", None)  # type: ignore[reportUnknownVariableType] -- dynamic attribute access on yaml exception
    if problem is None:
        return str(exc)
    if mark is None:
        return str(problem)
    return f"{problem} at line {mark.line + 1}, column {mark.column + 1}"  # type: ignore[reportUnknownMemberType] -- mark is Any from getattr on yaml exception


def from_yaml(source: str) -> JsonValue:
    """Parse YAML 1.2-style input into JSON-like Python values."""

    try:
        return _single_document(yaml.load_all(source, Loader=_yaml12_loader()), "fromYAML", "fromYAMLStream")
    except yaml.YAMLError as exc:
        raise ValueError(f"fromYAML: failed to parse YAML 1.2 document: {_parse_error_message(exc)}") from exc


def from_go_like_yaml(source: str) -> JsonValue:
    """Parse one YAML document the way go-yaml v2 reads it."""

    try:
        return _single_document(
            yaml.load_all(source, Loader=_go_like_loader()),
            "fromGoLikeYAML",
            "fromGoLikeYAMLStream",
        )
    except yaml.YAMLError as exc:
        raise ValueError(f"fromGoLikeYAML: failed to parse YAML document: {_parse_error_message(exc)}") from exc


def from_go_like_yaml_stream(source: str) -> list[JsonValue]:
    """Parse a YAML document stream the way go-yaml v2 reads it."""

    try:
        return _validate_documents(
            yaml.load_all(source, Loader=_go_like_loader()),
            "fromGoLikeYAMLStream",
        )
    except yaml.YAMLError as exc:
        raise ValueError(f"fromGoLikeYAMLStream: failed to parse YAML stream: {_parse_error_message(exc)}") from exc


def from_yaml11(source: str) -> JsonValue:
    """Parse legacy YAML 1.1 input into JSON-like Python values.

    .. deprecated::
       Use `from_go_like_yaml`. This loader describes go-yaml v2 with YAML
       1.1's tables and two additions, and issue #307 lists six classes of
       scalar where the description is wrong.
    """

    try:
        return _single_document(
            yaml.load_all(source, Loader=_yaml11_loader()),
            "fromYAML11",
            "fromYAML11Stream",
        )
    except yaml.YAMLError as exc:
        raise ValueError(f"fromYAML11: failed to parse YAML 1.1 document: {_parse_error_message(exc)}") from exc


def from_yaml_stream(source: str) -> list[JsonValue]:
    """Parse a YAML 1.2-style document stream into JSON-like Python values."""

    try:
        return _validate_documents(yaml.load_all(source, Loader=_yaml12_loader()), "fromYAMLStream")
    except yaml.YAMLError as exc:
        raise ValueError(f"fromYAMLStream: failed to parse YAML 1.2 stream: {_parse_error_message(exc)}") from exc


def from_yaml11_stream(source: str) -> list[JsonValue]:
    """Parse a legacy YAML 1.1 document stream into JSON-like Python values.

    .. deprecated::
       Use `from_go_like_yaml_stream`. See `from_yaml11`.
    """

    try:
        return _validate_documents(
            yaml.load_all(source, Loader=_yaml11_loader()),
            "fromYAML11Stream",
        )
    except yaml.YAMLError as exc:
        raise ValueError(f"fromYAML11Stream: failed to parse YAML 1.1 stream: {_parse_error_message(exc)}") from exc


class _BlockStyleDumper(yaml.CSafeDumper):
    """CSafeDumper that renders multi-line strings as literal blocks (``|``).

    A plain SafeDumper falls back to an escaped double-quoted scalar for any
    string PyYAML doesn't consider "simple" (e.g. containing '${{ ... }}'
    literals), which is valid YAML but unreadable for multi-line shell
    scripts. Scoped to a Dumper subclass rather than mutating
    yaml.SafeDumper globally, since other code in this process may still
    want PyYAML's default string style. CSafeDumper (libyaml-backed) instead
    of the pure-Python SafeDumper for the same reason as `_yaml12_loader`/
    `_yaml11_loader` above -- add_representer still mutates the shared
    Python-level Representer machinery the C emitter calls back into.
    """


def _represent_str(dumper: _BlockStyleDumper, data: str) -> yaml.Node:
    style = "|" if "\n" in data else None
    return dumper.represent_scalar(  # type: ignore[reportUnknownMemberType] -- PyYAML stubs don't type represent_scalar's return precisely
        "tag:yaml.org,2002:str",
        data,
        style=style,
    )


_BlockStyleDumper.add_representer(str, _represent_str)

# Quote a string that any reader would take back as something else.
#
# PyYAML decides whether a scalar may be written plain by resolving the text
# it is about to emit and checking that the answer is still the tag it holds.
# The dumper's resolvers are YAML 1.1's, so `0644` is quoted -- 1.1 reads it as
# octal -- and `1e+06` is not, because 1.1 reads that as a string. A YAML 1.2
# reader does not, and the value comes back a float. Writing a string in a form
# that changes its type on the next parse is not a round trip.
#
# So the dumper is told about the other readers' scalars as well, and the check
# becomes the union: text somebody would read as a number or a boolean is
# quoted. This only ever adds quotes. A real float still goes out plain,
# because PyYAML's own float representer writes `1.0e+30` rather than `1e+30`,
# which every version resolves.
#
# **The reader that matters is go-yaml.** It is what the API server decodes
# with, so it reads this dumper's output, and it resolves four things YAML 1.1
# calls strings. A manifest that carries the string "n" and writes it plain
# reaches the cluster as false.
for _tag, _pattern in (
    ("tag:yaml.org,2002:float", _YAML12_ONLY_FLOAT),
    # 1.2 integers 1.1 does not resolve: 1.1 reads a leading-zero `017` as
    # octal while 1.2 reads it as decimal 17, and 1.2 reads `0o17` as octal
    # where 1.1's production has no alternative for the prefix -- either way
    # the text means a number to somebody.
    ("tag:yaml.org,2002:int", _YAML12_ONLY_INT),
    # go-yaml's integers that 1.1 leaves as strings. `08` is not octal, so 1.1
    # gives up and go-yaml reads decimal 8. And 1.1's hex and octal
    # productions are lowercase only, where go-yaml takes either case.
    ("tag:yaml.org,2002:int", re.compile(r"^[-+]?0[0-9_]+$")),
    ("tag:yaml.org,2002:int", re.compile(r"^[-+]?0[xX][0-9a-fA-F_]+$")),
    ("tag:yaml.org,2002:int", re.compile(r"^[-+]?0[oO][0-7_]+$")),
):
    _BlockStyleDumper.add_implicit_resolver(  # type: ignore[reportUnknownMemberType] -- yaml.Dumper methods may not have complete stubs
        _tag,
        _pattern,
        list("-+0123456789."),
    )

# go-yaml's one-letter booleans. PyYAML's 1.1 resolver deliberately leaves
# these alone, so the string "n" went out plain and came back false.
_BlockStyleDumper.add_implicit_resolver(  # type: ignore[reportUnknownMemberType] -- yaml.Dumper methods may not have complete stubs
    "tag:yaml.org,2002:bool",
    re.compile(r"^[yYnN]$"),
    list("yYnN"),
)


def to_yaml(value: JsonValue) -> str:
    """Render JSON-like Nix/Python values as Kubernetes-compatible YAML."""

    value = _validate_document(value, "toYAML")
    try:
        if isinstance(value, list):
            rendered = yaml.dump_all(  # type: ignore[reportUnknownVariableType] -- PyYAML's dump_all overloads don't narrow the return type for a custom Dumper
                value,
                explicit_start=True,
                sort_keys=False,
                Dumper=_BlockStyleDumper,
            )
        else:
            rendered = yaml.dump(  # type: ignore[reportUnknownVariableType] -- PyYAML's dump overloads don't narrow the return type for a custom Dumper
                value,
                sort_keys=False,
                Dumper=_BlockStyleDumper,
            )
    except yaml.YAMLError as exc:
        raise ValueError(f"toYAML: failed to render YAML: {_parse_error_message(exc)}") from exc
    result: str = rendered
    return result


def yaml_primops() -> list[PrimOpSpec]:
    """Return worker primop specs for YAML parsing and rendering."""
    return [
        PrimOpSpec(
            name="fromYAML",
            arity=1,
            args=["source"],
            doc="Parse a YAML 1.2 string into a Nix value.",
            import_path="nanopynix.primops:from_yaml",
        ),
        PrimOpSpec(
            name="fromGoLikeYAML",
            arity=1,
            args=["source"],
            doc="Parse a YAML string the way go-yaml v2 reads it into a Nix value.",
            import_path="nanopynix.primops:from_go_like_yaml",
        ),
        PrimOpSpec(
            name="fromGoLikeYAMLStream",
            arity=1,
            args=["source"],
            doc="Parse a YAML document stream the way go-yaml v2 reads it into a Nix list.",
            import_path="nanopynix.primops:from_go_like_yaml_stream",
        ),
        PrimOpSpec(
            name="fromYAML11",
            arity=1,
            args=["source"],
            doc="Deprecated; use fromGoLikeYAML. Parse a YAML 1.1 string into a Nix value.",
            import_path="nanopynix.primops:from_yaml11",
        ),
        PrimOpSpec(
            name="fromYAMLStream",
            arity=1,
            args=["source"],
            doc="Parse a YAML 1.2 document stream into a Nix list.",
            import_path="nanopynix.primops:from_yaml_stream",
        ),
        PrimOpSpec(
            name="fromYAML11Stream",
            arity=1,
            args=["source"],
            doc="Deprecated; use fromGoLikeYAMLStream. Parse a YAML 1.1 document stream into a Nix list.",
            import_path="nanopynix.primops:from_yaml11_stream",
        ),
        PrimOpSpec(
            name="toYAML",
            arity=1,
            args=["value"],
            doc="Render a Nix value as YAML; root lists render as document streams.",
            import_path="nanopynix.primops:to_yaml",
        ),
    ]
