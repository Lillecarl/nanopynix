"""Import-time checked adapters from generated RPC services to local RPC calls."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import TYPE_CHECKING, Any, get_type_hints

if TYPE_CHECKING:
    from betterproto2 import Message


class GeneratedServiceAdapterMixin:
    """Install generated service methods from a checked binding method surface."""

    def __init_subclass__(
        cls,
        *,
        rpc_service_base: type | None = None,
        binding_method_names: set[str] | None = None,
        method_prefix: str = "",
        **kwargs: Any,
    ) -> None:
        super().__init_subclass__(**kwargs)
        if rpc_service_base is not None:
            if binding_method_names is None:
                raise TypeError("binding_method_names is required with rpc_service_base")
            _install_generated_service_methods(cls, rpc_service_base, binding_method_names, method_prefix)

    def _nanobind_rpc_call(self, binding_method_name: str, message: Message) -> Any:
        raise NotImplementedError


def _install_generated_service_methods(
    cls: type,
    service_base: type,
    binding_method_names: set[str],
    method_prefix: str,
) -> None:
    expected = _service_method_names(service_base)
    actual = {name.removeprefix(method_prefix) for name in binding_method_names}

    missing = sorted(expected - actual)
    extra = sorted(actual - expected)
    if missing or extra:
        details = []
        if missing:
            details.append(f"missing {method_prefix} methods: {', '.join(missing)}")
        if extra:
            details.append(f"extra {method_prefix} methods: {', '.join(extra)}")
        raise TypeError(f"{cls.__name__} does not match {service_base.__name__}: {'; '.join(details)}")

    for method_name in sorted(expected):
        method = getattr(service_base, method_name)
        response_type = get_type_hints(method)["return"]
        setattr(cls, method_name, _make_service_forwarder(method_name, method_prefix, response_type))


def _service_method_names(service_base: type) -> set[str]:
    return {
        name
        for name, value in service_base.__dict__.items()
        if not name.startswith("_") and callable(value)
    }


def _make_service_forwarder(
    method_name: str,
    method_prefix: str,
    response_type: type[Message],
) -> Callable[[GeneratedServiceAdapterMixin, Message], Any]:
    binding_method_name = f"{method_prefix}{method_name}"

    async def _forward(self: GeneratedServiceAdapterMixin, message: Message) -> Message:
        raw = self._nanobind_rpc_call(binding_method_name, message)
        if isinstance(raw, response_type):
            return raw
        if isinstance(raw, Mapping):
            return response_type.from_dict(_proto_shape(raw))
        raise TypeError(f"{binding_method_name} returned {type(raw).__name__}, expected proto-shaped mapping")

    _forward.__name__ = method_name
    return _forward


def _proto_shape(value: Any) -> Any:
    """Normalize nanobind containers into proto-shaped plain Python data."""
    if isinstance(value, Mapping):
        return {str(k): _proto_shape(v) for k, v in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return [_proto_shape(v) for v in value]
    return value
