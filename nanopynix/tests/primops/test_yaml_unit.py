"""Direct unit tests for nanopynix.primops.yaml.

The only existing coverage goes through real worker RPC round-trips in
nanopynix/tests/rpc/client/test_eval_rpc.py, which the module's own docstring
notes is broken on Nix 2.31. These dumb coverage tests exercise the pure
Python parsing/rendering logic directly so it stays covered regardless of
that upstream primop-registration issue.
"""

from __future__ import annotations

import pytest
import yaml
from yaml.representer import RepresenterError

from nanopynix.primops.yaml import (
    _parse_error_message,  # pyright: ignore[reportPrivateUsage] -- test reaches into the private helper for direct unit coverage of its fallback branches
    from_yaml,
    from_yaml11,
    from_yaml11_stream,
    from_yaml_stream,
    to_yaml,
)


def test_from_yaml_parses_hex_integers_as_yaml12() -> None:
    assert from_yaml("x: 0x1F") == {"x": 31}


def test_from_yaml_rejects_a_document_that_is_not_json_compatible() -> None:
    """A bare YAML timestamp parses to a datetime.date, which JsonValue
    cannot represent -- this must surface as a descriptive ValueError."""
    with pytest.raises(ValueError, match="YAML document is not JSON-compatible"):
        from_yaml("2023-01-01")


def test_from_yaml11_reports_a_parse_error() -> None:
    with pytest.raises(ValueError, match=r"fromYAML11: failed to parse YAML 1\.1 document"):
        from_yaml11("key: [unterminated")


def test_from_yaml_stream_reports_a_parse_error() -> None:
    with pytest.raises(ValueError, match=r"fromYAMLStream: failed to parse YAML 1\.2 stream"):
        from_yaml_stream("key: [unterminated")


def test_from_yaml11_stream_reports_a_parse_error() -> None:
    with pytest.raises(ValueError, match=r"fromYAML11Stream: failed to parse YAML 1\.1 stream"):
        from_yaml11_stream("key: [unterminated")


def test_to_yaml_wraps_a_render_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """No JsonValue-shaped input actually fails yaml.dump; force the failure
    path with a monkeypatch instead of contriving an unrepresentable value."""

    def _boom(*_args: object, **_kwargs: object) -> str:
        raise RepresenterError("cannot represent an object")

    monkeypatch.setattr(yaml, "dump", _boom)

    with pytest.raises(ValueError, match="toYAML: failed to render YAML"):
        to_yaml({"a": 1})


def test_parse_error_message_falls_back_to_str_without_a_problem_attribute() -> None:
    exc = RepresenterError("cannot represent an object")

    assert _parse_error_message(exc) == str(exc)


def test_parse_error_message_uses_the_problem_without_a_mark() -> None:
    class _FakeYamlError(Exception):
        problem = "mock problem"
        problem_mark = None

    assert _parse_error_message(_FakeYamlError()) == "mock problem"


# -- what Helm emits, and what has to come back out ------------------------
#
# `fromYAML11` reads Helm's output, and Helm's output is neither version
# cleanly: 1.1 integers, so that `defaultMode: 0644` means 420, and 1.2
# floats, because Go's `%v` on a float64 goes to scientific notation from 1e6
# upwards. The two below are one round trip and both halves used to break it.


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # The shape topolvm's priorityClass produces. 1.1's float production
        # requires a decimal point before the exponent, so this used to parse
        # as the string "1e+06" and the API server refused the PriorityClass:
        # ".value: expected numeric (int or float), got string".
        ("1e+06", 1000000.0),
        ("1e6", 1000000.0),
        # 1.1 also requires a sign in the exponent.
        ("1.5e6", 1500000.0),
        ("-2e3", -2000.0),
        (".5e1", 5.0),
    ],
)
def test_from_yaml11_reads_a_float_the_way_helm_writes_one(text: str, expected: float) -> None:
    assert from_yaml11(f"value: {text}") == {"value": expected}


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # Octal is the whole reason this parser is 1.1 rather than 1.2, and
        # widening the float production must not have touched it.
        ("0644", 420),
        ("017", 15),
        # A 1.1 float stays one, decided before the added resolver is reached.
        ("1.5e+06", 1500000.0),
        ("1.5", 1.5),
        # And a scalar neither version calls a number stays a string.
        ("1e", "1e"),
        ("abc", "abc"),
        ("1.2.3", "1.2.3"),
    ],
)
def test_from_yaml11_still_resolves_what_it_always_did(text: str, expected: object) -> None:
    assert from_yaml11(f"value: {text}") == {"value": expected}


@pytest.mark.parametrize(
    "text",
    [
        # Written plain, a YAML 1.2 reader takes this back as a float. The
        # dumper's own resolvers are 1.1's, which call it a string, so it used
        # to go out unquoted and change type on the next parse.
        "1e+06",
        "1e6",
        "1.5e6",
        # 1.1 already quoted these; they are here so a later change cannot
        # quietly stop.
        "0644",
        "1.5",
        "1000",
        "true",
        # 1.2 octal, which 1.1 calls a string.
        "0o17",
    ],
)
def test_to_yaml_quotes_a_string_some_reader_would_call_a_number(text: str) -> None:
    assert to_yaml({"value": text}) == f"value: '{text}'\n"


@pytest.mark.parametrize(
    ("value", "rendered"),
    [
        # A real number still goes out plain. PyYAML writes `1.0e+30` rather
        # than `1e+30`, which both versions resolve as a float, so nothing
        # here starts quoting numbers.
        (1000000.0, "1000000.0"),
        (1e30, "1.0e+30"),
        (420, "420"),
        (1.5, "1.5"),
        (True, "true"),
    ],
)
def test_to_yaml_leaves_a_number_unquoted(value: object, rendered: str) -> None:
    assert to_yaml({"value": value}) == f"value: {rendered}\n"


def test_a_helm_float_survives_the_round_trip() -> None:
    """Read what Helm wrote, write it again, read it again."""
    once = from_yaml11("value: 1e+06")

    assert from_yaml11(to_yaml(once)) == once == {"value": 1000000.0}
