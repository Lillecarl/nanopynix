"""Import-time checked adapters from generated RPC services to local RPC calls."""

# pyright: reportPrivateUsage=false, reportUnknownVariableType=false
# The mixin adapter pattern intentionally delegates to _nanobind_rpc_call across
# subclass boundaries.  This is by design, not a private-access violation.
# Unknown variable types arise from dynamically resolving methods via getattr
# and get_type_hints on generated service base classes without stubs.

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar, cast, get_type_hints

import anyio

if TYPE_CHECKING:
    from betterproto2 import Message


@dataclass(frozen=True)
class HandleArgSpec:
    """One extra resolved-handle keyword arg a binding method needs beyond the
    generic request dict -- e.g. a nanobind method that must receive another
    live object (a cross-object reference) rather than plain request data.
    """

    field: str
    """Request dict key to pop, e.g. ``"dest_store_handle"``."""
    kwarg: str
    """Binding method keyword-arg name to pass the resolved object as, e.g.
    ``"dest_store"``."""
    kind: str
    """``HandleRegistry`` kind tag the popped handle is expected to resolve
    to, e.g. ``"store"``."""
    required: bool = False
    """If true, a missing/falsy handle raises instead of omitting the kwarg."""


class GeneratedServiceAdapterMixin:
    """Install generated service methods from a checked binding method surface.

    If ``nix_executor_attr`` is given, generated forwarders dispatch the
    nanobind call through ``NixThreadExecutor.run()`` so that store operations
    run on the dedicated Nix thread rather than blocking the event loop.
    """

    _extra_handle_args: ClassVar[Mapping[str, tuple[HandleArgSpec, ...]]] = {}

    def __init_subclass__(
        cls,
        *,
        rpc_service_base: type | None = None,
        binding_method_names: set[str] | None = None,
        method_prefix: str = "",
        nix_executor_attr: str | None = None,
        extra_handle_args: Mapping[str, tuple[HandleArgSpec, ...]] | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init_subclass__(**kwargs)
        cls._extra_handle_args = extra_handle_args or {}
        if rpc_service_base is not None:
            if binding_method_names is None:
                raise TypeError("binding_method_names is required with rpc_service_base")
            _install_generated_service_methods(
                cls, rpc_service_base, binding_method_names, method_prefix, nix_executor_attr
            )

    def _nanobind_rpc_call(self, binding_method_name: str, message: Message) -> Any:
        raise NotImplementedError

    def _resolve_extra_binding_args(self, binding_method_name: str, request: dict[str, Any]) -> dict[str, Any]:
        """Pop and resolve any ``HandleArgSpec``s declared for this method into
        a kwargs dict, e.g. ``{"dest_store": <raw nanobind Store>}``.

        A spec's handle field is popped from ``request`` regardless of
        outcome, so callers never see it leak through as plain request data.
        """
        state: Any = cast("Any", self)._state
        kwargs: dict[str, Any] = {}
        for spec in self._extra_handle_args.get(binding_method_name, ()):
            handle = request.pop(spec.field, 0)
            if not handle:
                if spec.required:
                    raise RuntimeError(f"{spec.field} is required for {binding_method_name}")
                continue
            kwargs[spec.kwarg] = state.handles.get_typed(handle, spec.kind).require_raw()
        return kwargs


def _install_generated_service_methods(
    cls: type,
    service_base: type,
    binding_method_names: set[str],
    method_prefix: str,
    nix_executor_attr: str | None,
) -> None:
    expected: set[str] = _service_method_names(service_base)
    actual: set[str] = {name.removeprefix(method_prefix) for name in binding_method_names}

    missing: list[str] = sorted(expected - actual)
    extra: list[str] = sorted(actual - expected)
    if missing or extra:
        details: list[str] = []
        if missing:
            details.append(f"missing {method_prefix} methods: {', '.join(missing)}")
        if extra:
            details.append(f"extra {method_prefix} methods: {', '.join(extra)}")
        raise TypeError(f"{cls.__name__} does not match {service_base.__name__}: {'; '.join(details)}")

    for method_name in sorted(expected):
        method = getattr(service_base, method_name)
        response_type: type[Message] = get_type_hints(method)["return"]  # type: ignore[reportArgumentType] -- method is Any from getattr on service_base
        setattr(
            cls,
            method_name,
            _make_service_forwarder(method_name, method_prefix, response_type, nix_executor_attr),
        )


def _service_method_names(service_base: type) -> set[str]:
    return {name for name, value in service_base.__dict__.items() if not name.startswith("_") and callable(value)}


def _make_service_forwarder(
    method_name: str,
    method_prefix: str,
    response_type: type[Message],
    nix_executor_attr: str | None,
) -> Callable[[GeneratedServiceAdapterMixin, Message], Any]:
    binding_method_name = f"{method_prefix}{method_name}"

    if nix_executor_attr is not None:

        async def _forward_nix(self: GeneratedServiceAdapterMixin, message: Message) -> Message:
            executor: Any = _resolve_attr(self, nix_executor_attr)
            state = _resolve_attr(self, "_state")
            request = cast("Any", message)
            # NixThreadExecutor (dedicated-thread dispatch) and
            # anyio.CapacityLimiter (bounded anyio.to_thread dispatch) are
            # two distinct mechanisms, not variants of one shared interface
            # -- run_request() takes them as separate keyword parameters, so
            # this generic forwarder must route to the right one.
            if isinstance(executor, anyio.CapacityLimiter):
                raw = await state.run_request(
                    request_id=request.request_id,
                    limiter=executor,
                    operation=self._nanobind_rpc_call,
                    args=(binding_method_name, message),
                )
            else:
                raw = await state.run_request(
                    request_id=request.request_id,
                    executor=executor,
                    operation=self._nanobind_rpc_call,
                    args=(binding_method_name, message),
                )
            if isinstance(raw, response_type):
                return raw
            if isinstance(raw, Mapping):
                return response_type.from_dict(_proto_shape(raw))  # type: ignore[reportUnknownMemberType] -- betterproto2 Message stubs may be incomplete
            raise TypeError(f"{binding_method_name} returned {type(raw).__name__}, expected proto-shaped mapping")

        _forward_nix.__name__ = method_name
        return _forward_nix

    async def _forward(self: GeneratedServiceAdapterMixin, message: Message) -> Message:
        raw = self._nanobind_rpc_call(binding_method_name, message)
        if isinstance(raw, response_type):
            return raw
        if isinstance(raw, Mapping):
            return response_type.from_dict(_proto_shape(raw))  # type: ignore[reportUnknownMemberType] -- betterproto2 Message stubs may be incomplete
        raise TypeError(f"{binding_method_name} returned {type(raw).__name__}, expected proto-shaped mapping")

    _forward.__name__ = method_name
    return _forward


def _resolve_attr(obj: Any, attr_path: str) -> Any:
    for part in attr_path.split("."):
        obj = getattr(obj, part)
    return obj


def _proto_shape(value: object) -> Any:
    """Normalize nanobind containers into proto-shaped plain Python data."""
    if isinstance(value, Mapping):
        return {str(k): _proto_shape(v) for k, v in value.items()}  # type: ignore[reportUnknownArgumentType] -- Mapping narrowed without type params
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return [_proto_shape(v) for v in value]  # type: ignore[reportUnknownArgumentType] -- Sequence narrowed without type params
    return value
