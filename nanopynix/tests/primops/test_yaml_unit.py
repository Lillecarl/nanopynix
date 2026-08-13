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
