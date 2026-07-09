"""Pydantic models for all nanopynix data types crossing the C++/Python boundary.

Canonical representation is attrs-based — URL/string forms are computed at the
facade boundary via ``FlakeRef::fromAttrs(attrs).to_string()`` and
``parseFlakeRef(url).toAttrs()``.  No C++ dependency in this module.
"""

from __future__ import annotations

from enum import IntEnum, StrEnum
from typing import Literal

from pydantic import AliasChoices, BaseModel, Field, computed_field, field_validator, model_validator
from strip_ansi import strip_ansi as _strip_ansi

type JsonScalar = str | int | float | bool | None
type JsonValue = JsonScalar | list[JsonValue] | dict[str, JsonValue]


class NixType(StrEnum):
    """Nix value types exposed by eval RPC."""

    THUNK = "thunk"
    INT = "int"
    FLOAT = "float"
    BOOL = "bool"
    STRING = "string"
    PATH = "path"
    NULL = "null"
    ATTRS = "attrs"
    LIST = "list"
    FUNCTION = "function"
    EXTERNAL = "external"
    UNKNOWN = "unknown"


def _parse_store_path_string(value: str) -> dict[str, str]:
    basename = value.rstrip("/").rsplit("/", 1)[-1]
    try:
        hyphen = basename.index("-")
    except ValueError:
        raise ValueError(f"Invalid store path: no '-' separator in '{value}'") from None
    return {
        "hash_part": basename[:hyphen],
        "name": basename[hyphen + 1 :],
        "to_string": basename,
    }


class StorePath(BaseModel):
    """A Nix store path (e.g. ``/nix/store/<hash>-<name>``)."""

    hash_part: str = Field(description="The 32-character hash portion")
    name: str = Field(description="The name portion (e.g. 'bash-5.2')")
    to_string: str = Field(description="Full basename: '<hash>-<name>'")

    def __init__(self, value: str | None = None, **data) -> None:
        if value is not None:
            if data:
                raise TypeError("StorePath accepts either a path string or explicit fields, not both")
            data = _parse_store_path_string(value)
        super().__init__(**data)

    @model_validator(mode="before")
    @classmethod
    def _from_string(cls, value):
        if isinstance(value, str):
            return _parse_store_path_string(value)
        return value

    @computed_field
    @property
    def is_derivation(self) -> bool:
        """True if this path ends with .drv."""
        return self.name.endswith(".drv")


class PathInfo(BaseModel):
    """ValidPathInfo — metadata about a store path."""

    path: StorePath
    nar_hash: str = Field(description="NAR hash in SRI format")
    nar_size: int = Field(description="NAR size in bytes")
    registration_time: int | None = Field(default=None, description="Unix timestamp of registration")
    deriver: StorePath | None = Field(default=None, description="The .drv that built this path")
    references: list[StorePath] = Field(default_factory=list, description="Runtime references")
    ca: str | None = Field(default=None, description="Content address (if CA derivation)")
    ultimate: bool = Field(default=False, description="Whether this path is an ultimate root")


class BuildResult(BaseModel):
    """Result of a derivation build."""

    drv_path: str = Field(description="The derivation path that was built")
    success: bool
    status: str = Field(description="Outcome: 'built', 'substituted', 'permanent-failure', ...")
    error_msg: str = Field(default="", description="Error message if build failed")


class MissingInfo(BaseModel):
    """Result of queryMissing — paths not yet in the store."""

    will_build: list[StorePath] = Field(default_factory=list)
    will_substitute: list[StorePath] = Field(default_factory=list)
    unknown: list[StorePath] = Field(default_factory=list)
    download_size: int = Field(default=0)
    nar_size: int = Field(default=0)


class Input(BaseModel):
    """A flake/fetcher input in canonical attrs form.

    Attrs are the output of ``Input::toAttrs()`` / ``FlakeRef::toAttrs()``:
    ``{"type": "github", "owner": "NixOS", "repo": "nixpkgs", ...}``.
    """

    attrs: dict[str, str | int | bool] = Field(default_factory=dict)


class FlakeRef(BaseModel):
    """A parsed flake reference: an Input plus optional subdirectory."""

    attrs: dict[str, str | int | bool] = Field(default_factory=dict)


class LockedInput(BaseModel):
    """A single locked input inside a LockedFlake.

    Either ``attrs`` (direct reference) or ``follows`` (follows another input).
    """

    attrs: dict[str, str | int | bool] | None = Field(
        default=None, description="FlakeRef attrs when input has a direct reference"
    )
    is_flake: bool = Field(default=True, description="Whether this input is a flake")
    follows: list[str] = Field(default_factory=list, description="Input IDs this input follows")


class LockedFlake(BaseModel):
    """A locked flake with description and resolved inputs."""

    handle: int = Field(description="Worker-side handle to the LockedFlake object")
    description: str = Field(default="", description="Flake description from meta.description")
    inputs: dict[str, LockedInput] = Field(default_factory=dict, description="Locked inputs, keyed by id")


class ValueHandle(BaseModel):
    """Opaque eval value handle exported by the worker."""

    handle: int
    type: NixType


class RemoteValueRef(BaseModel):
    """Wire reference to a Nix value exported by the worker."""

    kind: Literal["remote_value"] = "remote_value"
    value: ValueHandle


class DeepScalar(BaseModel):
    """Wire node for a scalar result from recursive forcing."""

    kind: Literal["scalar"] = "scalar"
    value: JsonScalar


class DeepList(BaseModel):
    """Wire node for a recursively forced Nix list."""

    kind: Literal["list"] = "list"
    items: list[DeepValueWire]


class DeepAttrs(BaseModel):
    """Wire node for a recursively forced Nix attrset."""

    kind: Literal["attrs"] = "attrs"
    attrs: dict[str, DeepValueWire]


type DeepValueWire = DeepScalar | DeepList | DeepAttrs | RemoteValueRef


class ScalarCallArg(BaseModel):
    """Copied JSON scalar supplied to a Nix function call."""

    kind: Literal["scalar"] = "scalar"
    value: JsonScalar


class ListCallArg(BaseModel):
    """Copied list supplied to a Nix function call, with remote value leaves allowed."""

    kind: Literal["list"] = "list"
    items: list[CallArgWire]


class AttrsCallArg(BaseModel):
    """Copied attrset supplied to a Nix function call, with remote value leaves allowed."""

    kind: Literal["attrs"] = "attrs"
    attrs: dict[str, CallArgWire]


class RemoteCallArg(BaseModel):
    """Existing same-session Nix value supplied to a Nix function call."""

    kind: Literal["remote_value"] = "remote_value"
    handle: int


type CallArgWire = ScalarCallArg | ListCallArg | AttrsCallArg | RemoteCallArg


class PrimOpSpec(BaseModel):
    """Importable Python primop registered inside the worker process."""

    name: str = Field(description="Nix builtin name")
    arity: int = Field(description="Number of arguments")
    args: list[str] = Field(default_factory=list, description="Argument names for Nix documentation")
    doc: str = Field(default="", description="Nix builtin documentation")
    import_path: str = Field(description="Importable Python callable path, formatted as 'module:attribute'")


class LogEvent(BaseModel):
    """A single log event from Nix's internal logger.

    Worker emits ``request_id`` in the wire format; ``Nix.log_stream()``
    passes it through as the model's ``request_id`` field.
    """

    request_id: int = Field(default=0, description="RPC request ID for multiplexing")
    action: str = Field(description="'msg', 'warn', 'error', 'start', 'stop', or 'result'")
    args: list = Field(default_factory=list, description="Action-specific arguments")
    result_type: ResultType | None = Field(
        default=None,
        description="ResultType when action='result' (None for other actions)",
    )

    @property
    def message(self) -> str | None:
        """Raw message payload for log actions that carry text."""

        if self.action not in {"msg", "warn", "error"} or not self.args:
            return None
        message = self.args[-1]
        return message if isinstance(message, str) else None

    @property
    def message_without_ansi(self) -> str | None:
        """Message payload with ANSI color escapes removed, preserving newlines."""

        message = self.message
        return None if message is None else _strip_ansi(message)

    def without_ansi(self) -> LogEvent:
        """Return a copy with ANSI color escapes removed from string args."""

        return self.model_copy(update={"args": [_strip_ansi(arg) if isinstance(arg, str) else arg for arg in self.args]})


class ResultType(IntEnum):
    """Nix ``ResultType`` enum — activity build/check result types.

    Values match ``nix::ResultType`` from ``<nix/util/logging.hh>``.
    These appear as ``LogEvent.action == 'result'`` with the type in
    ``LogEvent.result_type``.
    """

    file_linked = 100
    build_log_line = 101
    untrusted_path = 102
    corrupted_path = 103
    set_phase = 104
    progress = 105
    set_expected = 106
    post_build_log_line = 107
    fetch_status = 108


# ── Derivation ───────────────────────────────────────────────────────


class DerivationOutputs(BaseModel):
    """Output spec for a derivation input."""

    outputs: list[str] = Field(default_factory=list)
    dynamic_outputs: dict[str, str] = Field(default_factory=dict)


class Derivation(BaseModel):
    """C++ ``nix::Derivation`` data — not the richer ``nix derivation show`` JSON."""

    name: str
    system: str = Field(validation_alias=AliasChoices("system", "platform"))
    builder: str
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    input_drvs: dict[str, DerivationOutputs] = Field(
        default_factory=dict,
        validation_alias=AliasChoices("input_drvs", "inputDrvs"),
    )  # StorePath string → outputs
    input_srcs: list[str] = Field(
        default_factory=list,
        validation_alias=AliasChoices("input_srcs", "inputSrcs"),
    )  # StorePath strings

    @field_validator("env", mode="before")
    @classmethod
    def _env_from_nix_pairs(cls, value):
        if isinstance(value, list):
            return dict(value)
        return value

    @field_validator("input_drvs", mode="before")
    @classmethod
    def _input_drvs_from_nix_list(cls, value):
        if not isinstance(value, list):
            return value
        result = {}
        for entry in value:
            children = dict(entry.get("children", {}))
            result[str(entry["path"])] = {
                "outputs": list(entry.get("outputs", [])),
                "dynamic_outputs": {str(k): ",".join(str(o) for o in v) for k, v in children.items()},
            }
        return result
