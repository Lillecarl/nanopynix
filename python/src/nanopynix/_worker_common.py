"""Shared worker dispatch helpers."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, TypeVar

from nanopynix import _protocol as rpc

ReqT = TypeVar("ReqT", bound=rpc.WorkerRequest[Any])


@dataclass(frozen=True)
class Endpoint[ReqT: rpc.WorkerRequest[Any]]:
    """Worker RPC endpoint bound to a typed request model."""

    request: type[ReqT]
    handler: Callable[[ReqT], Any]

    @property
    def name(self) -> str:
        return self.request.method

    def __call__(self, args: list[Any]) -> Any:
        request = self.request.from_args(args)
        return self.request.dump_response(self.handler(request))


def dispatch(endpoints: list[Endpoint[Any]]) -> dict[str, Endpoint[Any]]:
    return {endpoint.name: endpoint for endpoint in endpoints}
