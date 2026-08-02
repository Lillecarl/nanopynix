"""Service wrappers for transport-independent RPC limits."""

from __future__ import annotations

import functools
from typing import TYPE_CHECKING, Any, cast

import anyio
from grpclib.const import Handler

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Collection

    from grpclib._typing import IServable


class ConcurrencyLimitedService:
    """Wrap a grpclib service and cap concurrent handler execution."""

    def __init__(
        self,
        service: IServable,
        *,
        semaphore: anyio.Semaphore,
    ) -> None:
        self.service = service
        self._semaphore = semaphore
        self._mapping: dict[str, Handler] = {}
        for method, handler in service.__mapping__().items():
            raw_handler = cast("Any", handler)
            func = cast("Callable[[Any], Awaitable[Any]]", raw_handler.func)
            self._mapping[method] = Handler(
                func=self._wrap(func),
                cardinality=raw_handler.cardinality,
                request_type=raw_handler.request_type,
                reply_type=raw_handler.reply_type,
            )

    def _wrap(self, func: Callable[[Any], Awaitable[Any]]) -> Callable[[Any], Awaitable[Any]]:
        @functools.wraps(func)
        async def wrapped(stream: Any) -> Any:
            async with self._semaphore:
                return await func(stream)

        return wrapped

    def __mapping__(self) -> dict[str, Handler]:
        return dict(self._mapping)


def limit_services_concurrency(
    services: Collection[IServable],
    *,
    max_concurrency: int | None,
) -> tuple[IServable, ...]:
    """Return services wrapped with one shared concurrency limit."""
    if max_concurrency is None:
        return tuple(services)
    if max_concurrency <= 0:
        raise ValueError("max_concurrency must be positive")

    # Constructed here, outside any running event loop -- anyio.Semaphore
    # builds its wait queue lazily, so this is safe from `Server.__init__`.
    semaphore = anyio.Semaphore(max_concurrency)
    return tuple(
        ConcurrencyLimitedService(
            service,
            semaphore=semaphore,
        )
        for service in services
    )
