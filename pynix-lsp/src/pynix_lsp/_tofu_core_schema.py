"""Loads OpenTofu's built-in ("core") HCL block schema for terranix hover/completion.

``tofu providers schema -json`` (see ``_terranix_schema.py``) only covers
*provider*-specific resource/data-source attributes (e.g. ``random_id``'s
``byte_length``) -- it says nothing about the meta-arguments OpenTofu itself
understands on every resource/data/module block regardless of provider
(``count``, ``for_each``, ``depends_on``, ``lifecycle { ... }``, ...), nor
about the shape of blocks with no provider at all (``variable``, ``output``,
``terraform``, ``locals``, ...). That data isn't a Nix value, and there is no
``tofu <something> -json`` command that exports it either -- it only exists
inside `opentofu-schema <https://github.com/opentofu/opentofu-schema>`_, a Go
library (a dependency of terraform-ls/opentofu-ls) that hand-encodes it,
version by version.

``tools/tofu-core-schema/`` (this repo) is a small Go program that imports
that library, calls its ``CoreModuleSchemaForVersion``, and dumps the result
as JSON shaped exactly like ``tofu providers schema -json``'s own per-
resource ``block`` shape (``attributes`` + ``block_types``, each nested block
wrapping a further ``block``) -- so it reuses ``_terranix_schema.py``'s
``SchemaBlock``/``SchemaAttribute`` models and
``walk_schema_block``/``list_block_attributes``/``render_schema_attribute``
helpers unmodified; only *where* a ``SchemaBlock`` comes from differs.

Unlike the provider schema (``_terranix_schema.py``), which needs an offline
``tofu init`` + subprocess run against a specific project's lock file, the
core schema needs nothing project-specific -- ``CoreModuleSchemaForVersion``
takes only a version string. So this module just invokes the Go tool
directly, live, with whatever exact version ``detect_tofu_version`` detected,
caching the parsed result in memory per exact version string for the
server's lifetime. ``tools/tofu-core-schema/package.nix`` builds the tool as
an ordinary Nix package (``tofuCoreSchemaTool`` in ``default.nix``); it's put
on ``PATH`` for both the real build (``pynix/package.nix``'s
``makeWrapperArgs``) and the editable-install dev shell
(``nix/shell.nix``'s ``packages``), so Go itself is only ever a build-time
dependency of that one package -- never something a human runs by hand, and
never something committed to git.
"""

from __future__ import annotations

import json
import subprocess
from typing import TYPE_CHECKING

import anyio
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from nanopynix._typechecking import BEARTYPING
from pynix_lsp._terranix_schema import SchemaBlock

if TYPE_CHECKING or BEARTYPING:
    from collections.abc import Mapping, Sequence


class CoreBlockSchema(BaseModel):
    """One top-level core block type's schema (``resource``, ``variable``, ...)."""

    model_config = ConfigDict(extra="ignore")

    labels: list[str] = Field(default_factory=list)
    description: str | None = None
    block: SchemaBlock = Field(default_factory=SchemaBlock)


_CORE_SCHEMA_ADAPTER = TypeAdapter(dict[str, CoreBlockSchema])
_MEM_CACHE: dict[str, Mapping[str, CoreBlockSchema]] = {}
_TOOL_BIN = "nanopynix-tofu-core-schema"


async def get_core_schema(tofu_version: str) -> Mapping[str, CoreBlockSchema] | None:
    """Return the core-schema for exactly *tofu_version*, or ``None`` on failure.

    Cached in-memory, keyed by the exact version string -- ``tools/tofu-core-
    schema``'s ``CoreModuleSchemaForVersion`` call is cheap and deterministic
    (no network, no project-specific state), so there's no need to round a
    version down to some coarser bucket the way ``_closest_version`` used to;
    every distinct version a caller ever asks for gets its own real answer,
    computed once per server lifetime.
    """
    cached = _MEM_CACHE.get(tofu_version)
    if cached is not None:
        return cached
    try:
        completed = await anyio.run_process([_TOOL_BIN, tofu_version])
    except (OSError, subprocess.CalledProcessError):
        return None
    schema = _CORE_SCHEMA_ADAPTER.validate_json(completed.stdout)
    _MEM_CACHE[tofu_version] = schema
    return schema


async def detect_tofu_version(tofu_bin: str, store_exec_prefix: Sequence[str] = ()) -> str | None:
    """Run ``tofu version -json`` and return its ``terraform_version`` field, or None on failure.

    *tofu_bin* is a path inside the Nix store the caller evaluated against, so
    it is only directly executable when that store sits at its own logical
    location. *store_exec_prefix* -- from ``nanopynix.store_exec_prefix`` --
    is what makes it runnable when the store is relocated instead; it is empty
    for an ordinary store, so passing it is unconditional.
    """
    try:
        completed = await anyio.run_process([*store_exec_prefix, tofu_bin, "version", "-json"])
    except (OSError, subprocess.CalledProcessError):
        return None
    try:
        version = json.loads(completed.stdout).get("terraform_version")
    except json.JSONDecodeError:
        return None
    return version if isinstance(version, str) else None


def merge_schema_blocks(primary: SchemaBlock, fallback: SchemaBlock) -> SchemaBlock:
    """Union two ``SchemaBlock``s' attributes/block_types, *primary* winning on key conflicts.

    Used to blend a provider's resource-specific attributes (*primary*, more
    specific/current data) with the core schema's universal meta-arguments
    like ``count``/``for_each``/``lifecycle`` (*fallback*) into one block that
    ``walk_schema_block``/``list_block_attributes`` can query unmodified. Not
    recursive: nested blocks like ``lifecycle`` only ever come from one
    source in practice (core schema; no provider defines a resource-level
    ``lifecycle`` block itself), so a shallow per-key merge is sufficient.
    """
    return SchemaBlock(
        attributes={**fallback.attributes, **primary.attributes},
        block_types={**fallback.block_types, **primary.block_types},
    )
