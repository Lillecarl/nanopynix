"""Pydantic models for all nanopynix data types crossing the C++/Python boundary.

Canonical representation is attrs-based — URL/string forms are computed at the
facade boundary via ``FlakeRef::fromAttrs(attrs).to_string()`` and
``parseFlakeRef(url).toAttrs()``.  No C++ dependency in this module.
"""

from __future__ import annotations

from pydantic import BaseModel, computed_field, Field


class StorePath(BaseModel):
    """A Nix store path (e.g. ``/nix/store/<hash>-<name>``)."""

    hash_part: str = Field(description="The 32-character hash portion")
    name: str = Field(description="The name portion (e.g. 'bash-5.2')")
    to_string: str = Field(description="Full basename: '<hash>-<name>'")

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

    description: str = Field(default="", description="Flake description from meta.description")
    inputs: dict[str, LockedInput] = Field(default_factory=dict, description="Locked inputs, keyed by id")


class LogEvent(BaseModel):
    """A single log event from Nix's internal logger.

    Wire-format ``id`` field is mapped to ``request_id`` during validation
    in ``Nix.log_stream()``.
    """

    request_id: int = Field(default=0, description="RPC request ID for multiplexing")
    action: str = Field(description="'msg', 'warn', 'error', 'start', 'stop', or 'result'")
    args: list = Field(default_factory=list, description="Action-specific arguments")
