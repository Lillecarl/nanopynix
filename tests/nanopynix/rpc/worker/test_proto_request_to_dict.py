"""``proto_request_to_dict`` must stay interchangeable with betterproto2's ``to_dict``.

The worker used to marshal every store request with
``message.to_dict(casing=SNAKE, include_default_values=True,
output_format=PYTHON)``. That call runs ``typing.get_type_hints`` on the
message class *every time* and never caches it: ~380us, against a ~3us
nanobind store call. Marshalling the request outweighed the Nix work it was
marshalling for by two orders of magnitude.

``proto_request_to_dict`` reads the dataclass fields directly instead, which is
exact for a message of scalars, lists of scalars and enums -- ``PYTHON`` output
format is an identity transform on all of those -- and wrong the moment a field
holds another message, where ``to_dict`` recurses to a nested dict while a
direct read yields the ``Message``. The eval schema has such fields, so the
helper detects them and defers to ``to_dict``.

This compares the two implementations across the whole schema: every request
message empty, and every *individual field* of every request message carrying a
non-default value. One field at a time rather than all at once, because several
messages are ``oneof`` groups where setting every member is invalid by
construction -- and because a failure then names the exact field that diverged.

``test_the_nested_message_fallback_is_load_bearing`` is the separate reason the
fallback is not dead code.
"""

from __future__ import annotations

import dataclasses
import enum
import pkgutil
import types
from importlib import import_module
from typing import Any, Union, cast, get_args, get_origin, get_type_hints

import nanopynix_proto.nix
import pytest
from betterproto2 import Casing, Message, OutputFormat

from nanopynix.rpc.worker._service_adapter import proto_request_to_dict

TO_DICT_ARGS: dict[str, Any] = {
    "casing": Casing.SNAKE,
    "include_default_values": True,
    "output_format": OutputFormat.PYTHON,
}

A_STORE_PATH = "/nix/store/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-a"


def _request_messages() -> list[type[Message]]:
    """Every generated request message across the whole proto schema."""
    found: dict[str, type[Message]] = {}
    for module_info in pkgutil.iter_modules(nanopynix_proto.nix.__path__):
        module = import_module(f"nanopynix_proto.nix.{module_info.name}")
        for name, obj in vars(module).items():
            if isinstance(obj, type) and issubclass(obj, Message) and name.endswith("Request"):
                found[f"{module_info.name}.{name}"] = obj
    return [found[key] for key in sorted(found)]


REQUEST_MESSAGES = _request_messages()


def _fields_of(message: type[Message] | Message) -> tuple[dataclasses.Field[Any], ...]:
    """``dataclasses.fields`` for a generated message.

    betterproto2's ``Message`` base is not itself a dataclass, so pyright
    rejects it as an argument even though every generated subclass is one.
    """
    return dataclasses.fields(cast("Any", message))


def _without_none(annotation: Any) -> Any:
    """``X | None`` -> ``X``; anything else unchanged."""
    if get_origin(annotation) in {Union, types.UnionType}:
        args = [arg for arg in get_args(annotation) if arg is not type(None)]
        if len(args) == 1:
            return args[0]
    return annotation


# bool before int: bool is an int subclass, so the order is what makes a bool
# field get True rather than 7.
SCALARS: list[tuple[type, object]] = [(bool, True), (int, 7), (float, 1.5), (str, "populated")]


def _leaf_value(annotation: type) -> object:
    """A non-default value for a non-container field type."""
    if issubclass(annotation, Message):
        # A default instance is enough: what makes the two implementations
        # diverge is only that the value *is* a Message. Populating it deeply
        # would hit the same oneof problem this test avoids by setting one
        # field at a time.
        return annotation()
    if issubclass(annotation, enum.Enum):
        members = list(annotation)
        return next((member for member in members if member.value != 0), members[0])
    for kind, value in SCALARS:
        if issubclass(annotation, kind):
            return value
    return pytest.fail(f"test cannot build a value for {annotation!r} -- teach _leaf_value about it")


def _non_default_value(annotation: Any) -> object:
    """A value for a field of this declared type, distinct from its default."""
    annotation = _without_none(annotation)
    origin = get_origin(annotation)
    if origin is list:
        (item,) = get_args(annotation) or (str,)
        item = _without_none(item)
        return [item()] if isinstance(item, type) and issubclass(item, Message) else [A_STORE_PATH]
    if origin is dict:
        _, value_type = get_args(annotation) or (str, str)
        return {"key": _non_default_value(value_type)}
    if isinstance(annotation, type):
        return _leaf_value(annotation)
    return pytest.fail(f"test cannot build a value for {annotation!r} -- teach _non_default_value about it")


def _field_cases() -> list[tuple[type[Message], str]]:
    return [
        (message_type, field.name)
        for message_type in REQUEST_MESSAGES
        for field in _fields_of(message_type)
    ]


FIELD_CASES = _field_cases()


def _one_field_set(message_type: type[Message], field_name: str) -> Message:
    annotation = get_type_hints(message_type)[field_name]
    return message_type(**{field_name: _non_default_value(annotation)})


@pytest.mark.parametrize("message_type", REQUEST_MESSAGES, ids=lambda cls: cls.__name__)
def test_matches_to_dict_for_an_empty_request(message_type: type[Message]) -> None:
    """A default-constructed request marshals identically either way."""
    message = message_type()
    assert proto_request_to_dict(message) == message.to_dict(**TO_DICT_ARGS)


@pytest.mark.parametrize(
    ("message_type", "field_name"), FIELD_CASES, ids=lambda case: case if isinstance(case, str) else case.__name__
)
def test_matches_to_dict_for_each_populated_field(message_type: type[Message], field_name: str) -> None:
    """Each field, carrying a non-default value, marshals identically either way.

    The empty case alone would miss a field whose two implementations agree on
    the zero value and disagree on anything else -- which is exactly how a
    nested message behaves, since an unset one is ``None`` on both sides.
    """
    message = _one_field_set(message_type, field_name)
    assert proto_request_to_dict(message) == message.to_dict(**TO_DICT_ARGS)


def test_the_schema_actually_has_request_messages() -> None:
    """A discovery bug would otherwise make the tests above vacuously pass."""
    assert len(REQUEST_MESSAGES) > 20, f"only found {len(REQUEST_MESSAGES)} request messages"
    assert len(FIELD_CASES) > 100, f"only found {len(FIELD_CASES)} field cases"


def test_the_nested_message_fallback_is_load_bearing() -> None:
    """Reading fields directly really does diverge where the fallback triggers.

    Without this, the tests above would still pass if the shortcut happened to
    be universally correct, and the fallback would be dead code nobody noticed
    was unnecessary. This pins the opposite: real schema messages exist where
    the shortcut is *wrong*, which is why the helper defers to ``to_dict`` for
    them.
    """
    diverging: list[str] = []
    for message_type, field_name in FIELD_CASES:
        message = _one_field_set(message_type, field_name)
        naive = {field.name: getattr(message, field.name) for field in _fields_of(message)}
        reference = message.to_dict(**TO_DICT_ARGS)
        if naive != reference:
            diverging.append(f"{message_type.__name__}.{field_name}")
            assert proto_request_to_dict(message) == reference, (
                f"{message_type.__name__}.{field_name} diverges and the fallback did not catch it"
            )

    assert diverging, "no field makes the naive read diverge -- the fallback branch is untested"
