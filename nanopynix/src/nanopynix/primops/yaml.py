"""Worker-side Python primop helpers.

.. note::
   Primop registration (both these YAML primops and custom
   ``Session(primops=..., primop_callables=...)`` registration) requires
   Nix >= 2.32. Every supported Nix meets that, because ``supportedNixFloor``
   is 2.34. Registration is broken on Nix 2.31 and is not expected to be fixed
   there, which matters only if you link that version yourself.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

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


def _yaml11_loader() -> type[Any]:
    class Loader(yaml.CSafeLoader):  # type: ignore[reportUnknownBaseType] -- PyYAML stubs may be incomplete
        pass

    # Helm's dialect, which is what this parser is for, and which is neither
    # version cleanly: 1.1 for integers, because `defaultMode: 0644` means 420
    # and 1.2 would read it as 644, and 1.2 for floats, because of the
    # exponent above.
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


def from_yaml11(source: str) -> JsonValue:
    """Parse legacy YAML 1.1 input into JSON-like Python values."""

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
    """Parse a legacy YAML 1.1 document stream into JSON-like Python values."""

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

# Quote a string that any reader would take back as a number.
#
# PyYAML decides whether a scalar may be written plain by resolving the text
# it is about to emit and checking that the answer is still the tag it holds.
# The dumper's resolvers are YAML 1.1's, so `0644` is quoted -- 1.1 reads it as
# octal -- and `1e+06` is not, because 1.1 reads that as a string. A YAML 1.2
# reader does not, and the value comes back a float. Writing a string in a form
# that changes its type on the next parse is not a round trip.
#
# So the dumper is told about 1.2's numbers as well, and the check becomes the
# union of the two: text either version would read as a number is quoted. This
# only ever adds quotes. A real float still goes out plain, because PyYAML's
# own float representer writes `1.0e+30` rather than `1e+30`, which both
# versions resolve.
for _tag, _pattern in (
    ("tag:yaml.org,2002:float", _YAML12_ONLY_FLOAT),
    # 1.2 integers 1.1 does not resolve: `0o17` is octal in 1.2 and a string
    # in 1.1, and 1.1 reads a leading-zero `017` as octal while 1.2 reads it
    # as decimal 17 -- either way the text means a number to somebody.
    ("tag:yaml.org,2002:int", re.compile(r"^[-+]?0o[0-7_]+$")),
):
    _BlockStyleDumper.add_implicit_resolver(  # type: ignore[reportUnknownMemberType] -- yaml.Dumper methods may not have complete stubs
        _tag,
        _pattern,
        list("-+0123456789."),
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
            name="fromYAML11",
            arity=1,
            args=["source"],
            doc="Parse a YAML 1.1 string into a Nix value.",
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
            doc="Parse a YAML 1.1 document stream into a Nix list.",
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
