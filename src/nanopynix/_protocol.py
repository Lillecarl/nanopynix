"""Typed worker RPC protocol declarations.

Request models describe the wire command: namespace, method name, positional
arguments, and response normalization. Worker handlers remain ordinary named
functions so stateful Nix behavior stays easy to find.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any, ClassVar, Self, TypeVar

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter
from pydantic_core import PydanticUndefined

from nanopynix.models import (
    BuildResult,
    CallArgWire,
    DeepValueWire,
    Derivation,
    FlakeRef,
    Input,
    JsonValue,
    LockedFlake,
    MissingInfo,
    NixType,
    PathInfo,
    RemoteValueRef,
    StorePath,
    ValueHandle,
)

if TYPE_CHECKING:
    from pydantic.fields import FieldInfo

T = TypeVar("T")
_TypeVarType = type(T)

ResponseAdapter = Callable[[Any], Any]


def _identity(value: Any) -> Any:
    return value


def _model_dump(model: type[BaseModel]) -> ResponseAdapter:
    return lambda value: model.model_validate(value).model_dump(mode="json")


def _adapter_dump(adapter: TypeAdapter[Any]) -> ResponseAdapter:
    return lambda value: adapter.dump_python(adapter.validate_python(value), mode="json")


def _maybe_model_dump(model: type[BaseModel]) -> ResponseAdapter:
    return lambda value: None if value is None else model.model_validate(value).model_dump(mode="json")


def _already_model_dump(value: BaseModel) -> dict[str, Any]:
    return value.model_dump(mode="json")


def rpc_arg(index: int, default: Any = PydanticUndefined) -> Any:
    extra: dict[str, Any] = {"rpc_pos": index}
    if default is PydanticUndefined:
        return Field(json_schema_extra=extra)
    return Field(default, json_schema_extra=extra)


class WorkerRequest[T](BaseModel):
    """Base class for positional worker RPC requests."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    namespace: ClassVar[str]
    method: ClassVar[str]
    response_adapter: ClassVar[ResponseAdapter] = _identity
    response_type_adapter: ClassVar[TypeAdapter[Any]] = TypeAdapter(Any)
    _registry: ClassVar[dict[tuple[str, str], type[WorkerRequest[Any]]]] = {}

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        response_type = _response_type_arg(cls)
        if response_type is not None:
            cls.response_type_adapter = TypeAdapter(response_type)
        namespace = getattr(cls, "namespace", None)
        method = getattr(cls, "method", None)
        if namespace is not None and method is not None:
            cls._registry[(namespace, method)] = cls

    def to_args(self) -> list[Any]:
        values: list[tuple[int, Any]] = []
        for name, field in type(self).model_fields.items():
            pos = _rpc_pos(field)
            if pos is None:
                continue
            values.append((pos, getattr(self, name)))
        return [TypeAdapter(Any).dump_python(value, mode="json") for _, value in sorted(values, key=lambda item: item[0])]

    @classmethod
    def from_args(cls, args: list[Any]) -> Self:
        data: dict[str, Any] = {}
        for name, field in cls.model_fields.items():
            pos = _rpc_pos(field)
            if pos is None:
                continue
            if pos < len(args):
                data[name] = args[pos]
        return cls.model_validate(data)

    @classmethod
    def dump_response(cls, value: Any) -> Any:
        return cls.response_adapter(value)

    @classmethod
    def parse_response(cls, value: Any) -> T:
        return cls.response_type_adapter.validate_python(value)


def _response_type_arg(cls: type[object]) -> object | None:
    for base in cls.__mro__:
        metadata = getattr(base, "__pydantic_generic_metadata__", None)
        if not isinstance(metadata, dict):
            continue
        args = metadata.get("args", ())
        if len(args) != 1:
            continue
        response_type = args[0]
        if isinstance(response_type, _TypeVarType):
            continue
        return response_type
    return None


def _rpc_pos(field: FieldInfo) -> int | None:
    extra = field.json_schema_extra
    if not isinstance(extra, dict):
        return None
    pos = extra.get("rpc_pos")
    return pos if isinstance(pos, int) else None


_StorePathList = TypeAdapter(list[StorePath])
_BuildResultList = TypeAdapter(list[BuildResult])
_StringList = TypeAdapter(list[str])
_DeepValueWire = TypeAdapter(DeepValueWire)

type ForceValueWire = JsonValue | RemoteValueRef


class StoreRequest(WorkerRequest[T]):
    namespace: ClassVar[str] = "store"


class EvalRequest(WorkerRequest[T]):
    namespace: ClassVar[str] = "eval"


class GetUri(StoreRequest[str]):
    method: ClassVar[str] = "get_uri"


class GetStoreDir(StoreRequest[str]):
    method: ClassVar[str] = "get_store_dir"


class IsValidPath(StoreRequest[bool]):
    method: ClassVar[str] = "is_valid_path"
    path: str = rpc_arg(0)


class ParseStorePath(StoreRequest[StorePath]):
    method: ClassVar[str] = "parse_store_path"
    response_adapter: ClassVar[ResponseAdapter] = _model_dump(StorePath)
    path: str = rpc_arg(0)


class QueryPathInfo(StoreRequest[PathInfo]):
    method: ClassVar[str] = "query_path_info"
    response_adapter: ClassVar[ResponseAdapter] = _model_dump(PathInfo)
    path: str = rpc_arg(0)


class QueryPathFromHashPart(StoreRequest[StorePath | None]):
    method: ClassVar[str] = "query_path_from_hash_part"
    response_adapter: ClassVar[ResponseAdapter] = _maybe_model_dump(StorePath)
    hash_part: str = rpc_arg(0)


class ComputeFsClosure(StoreRequest[list[StorePath]]):
    method: ClassVar[str] = "compute_fs_closure"
    path: str = rpc_arg(0)
    flip_direction: bool = rpc_arg(1, False)
    include_outputs: bool = rpc_arg(2, False)
    include_derivers: bool = rpc_arg(3, False)


class QueryMissing(StoreRequest[MissingInfo]):
    method: ClassVar[str] = "query_missing"
    response_adapter: ClassVar[ResponseAdapter] = _model_dump(MissingInfo)
    paths: list[str] = rpc_arg(0)


class QueryDerivationOutputs(StoreRequest[list[StorePath]]):
    method: ClassVar[str] = "query_derivation_outputs"
    path: str = rpc_arg(0)


class QueryValidDerivers(StoreRequest[list[StorePath]]):
    method: ClassVar[str] = "query_valid_derivers"
    path: str = rpc_arg(0)


class QueryAllValidPaths(StoreRequest[list[StorePath]]):
    method: ClassVar[str] = "query_all_valid_paths"


class QueryReferrers(StoreRequest[list[StorePath]]):
    method: ClassVar[str] = "query_referrers"
    path: str = rpc_arg(0)


class QuerySubstitutablePaths(StoreRequest[list[StorePath]]):
    method: ClassVar[str] = "query_substitutable_paths"
    paths: list[str] = rpc_arg(0)


class BuildPathsWithResults(StoreRequest[list[BuildResult]]):
    method: ClassVar[str] = "build_paths_with_results"
    response_adapter: ClassVar[ResponseAdapter] = _adapter_dump(_BuildResultList)
    paths: list[str] = rpc_arg(0)


class ReadDerivation(StoreRequest[Derivation]):
    method: ClassVar[str] = "read_derivation"
    response_adapter: ClassVar[ResponseAdapter] = _model_dump(Derivation)
    path: str = rpc_arg(0)


class BuildDerivation(StoreRequest[BuildResult]):
    method: ClassVar[str] = "build_derivation"
    response_adapter: ClassVar[ResponseAdapter] = _model_dump(BuildResult)
    path: str = rpc_arg(0)
    build_mode: int = rpc_arg(1, 0)


class FollowLinksToStorePath(StoreRequest[StorePath]):
    method: ClassVar[str] = "follow_links_to_store_path"
    response_adapter: ClassVar[ResponseAdapter] = _model_dump(StorePath)
    path: str = rpc_arg(0)


class AddTempRoot(StoreRequest[None]):
    method: ClassVar[str] = "add_temp_root"
    path: str = rpc_arg(0)


class FetchFromUrl(StoreRequest[Input]):
    method: ClassVar[str] = "fetch_from_url"
    response_adapter: ClassVar[ResponseAdapter] = _already_model_dump
    url: str = rpc_arg(0)


class FetchFromAttrs(StoreRequest[Input]):
    method: ClassVar[str] = "fetch_from_attrs"
    response_adapter: ClassVar[ResponseAdapter] = _already_model_dump
    attrs: dict[str, str | int | bool] = rpc_arg(0)


class EvalFile(EvalRequest[ValueHandle]):
    method: ClassVar[str] = "eval_file"
    path: str = rpc_arg(0)


class EvalString(EvalRequest[ValueHandle]):
    method: ClassVar[str] = "eval_string"
    expr: str = rpc_arg(0)
    source_name: str = rpc_arg(1, "<string>")


class Force(EvalRequest[ForceValueWire]):
    method: ClassVar[str] = "force"
    response_adapter: ClassVar[ResponseAdapter] = _adapter_dump(TypeAdapter(ForceValueWire))
    handle: int = rpc_arg(0)


class ForceDeep(EvalRequest[DeepValueWire]):
    method: ClassVar[str] = "force_deep"
    response_adapter: ClassVar[ResponseAdapter] = _adapter_dump(_DeepValueWire)
    handle: int = rpc_arg(0)


class Attr(EvalRequest[ValueHandle]):
    method: ClassVar[str] = "attr"
    handle: int = rpc_arg(0)
    name: str = rpc_arg(1)


class ListGet(EvalRequest[ValueHandle]):
    method: ClassVar[str] = "list_get"
    handle: int = rpc_arg(0)
    index: int = rpc_arg(1)


class ListLength(EvalRequest[int]):
    method: ClassVar[str] = "list_length"
    handle: int = rpc_arg(0)


class AttrNames(EvalRequest[list[str]]):
    method: ClassVar[str] = "attr_names"
    response_adapter: ClassVar[ResponseAdapter] = _adapter_dump(_StringList)
    handle: int = rpc_arg(0)


class HasAttr(EvalRequest[bool]):
    method: ClassVar[str] = "has_attr"
    handle: int = rpc_arg(0)
    name: str = rpc_arg(1)


class TypeName(EvalRequest[NixType]):
    method: ClassVar[str] = "type_name"
    handle: int = rpc_arg(0)


class Call(EvalRequest[ValueHandle]):
    method: ClassVar[str] = "call"
    handle: int = rpc_arg(0)
    args: list[CallArgWire] = rpc_arg(1)


class LockFlake(EvalRequest[LockedFlake]):
    method: ClassVar[str] = "lock_flake"
    response_adapter: ClassVar[ResponseAdapter] = _model_dump(LockedFlake)
    ref: str | dict[str, Any] = rpc_arg(0)


class GetFlake(EvalRequest[FlakeRef]):
    method: ClassVar[str] = "get_flake"
    response_adapter: ClassVar[ResponseAdapter] = _already_model_dump
    ref: str | dict[str, Any] = rpc_arg(0)


class Release(EvalRequest[None]):
    method: ClassVar[str] = "release"
    handle: int = rpc_arg(0)


class ReleaseAll(EvalRequest[None]):
    method: ClassVar[str] = "release_all"
