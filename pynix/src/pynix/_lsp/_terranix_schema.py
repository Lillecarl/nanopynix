"""Fetches and caches ``tofu providers schema -json`` output for terranix files.

Kept separate from ``_terranix.py``'s ``Dialect`` glue so this can be
unit-tested against a fake JSON blob and a stubbed subprocess runner, with no
LSP server or Nix evaluator involved at all.

Modeled with pydantic (already a ``nanopynix`` dependency, see
``nanopynix.settings``) rather than raw ``dict``/``isinstance`` walking: only
the fields this module actually reads are declared, and every model ignores
unknown extra fields by default, so a newer ``tofu`` adding schema fields
doesn't break parsing.

Cache key is the pair of Nix store output paths for a terranix directive's
``tofu``/``module`` derivations (see ``_terranix.py``'s module docstring for
that directive contract) -- both are already content-addressed by Nix, so a
changed ``.terraform.lock.hcl`` or provider version necessarily produces a
different store path. No separate mtime/hash bookkeeping of the lock file
itself is needed on top of that.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

import anyio
from pydantic import BaseModel, ConfigDict, Field


class SchemaAttribute(BaseModel):
    """One resource/data-source attribute's schema (``block.attributes.<name>``)."""

    model_config = ConfigDict(extra="ignore")

    type: Any = None
    description: str | None = None
    required: bool = False
    optional: bool = False
    computed: bool = False


class SchemaNestedBlock(BaseModel):
    """One entry of ``block.block_types.<name>``, wrapping a further nested block."""

    model_config = ConfigDict(extra="ignore")

    block: SchemaBlock = Field(default_factory=lambda: SchemaBlock())


class SchemaBlock(BaseModel):
    """A resource/data-source (or nested-block) schema block."""

    model_config = ConfigDict(extra="ignore")

    attributes: dict[str, SchemaAttribute] = Field(default_factory=dict)
    block_types: dict[str, SchemaNestedBlock] = Field(default_factory=dict)


SchemaBlock.model_rebuild()


class _ResourceSchemaEntry(BaseModel):
    model_config = ConfigDict(extra="ignore")

    block: SchemaBlock = Field(default_factory=SchemaBlock)


class _ProviderSchemaEntry(BaseModel):
    model_config = ConfigDict(extra="ignore")

    resource_schemas: dict[str, _ResourceSchemaEntry] = Field(default_factory=dict)
    data_source_schemas: dict[str, _ResourceSchemaEntry] = Field(default_factory=dict)


class ProviderSchemas(BaseModel):
    """The parsed shape of ``tofu providers schema -json``'s output.

    Real shape (confirmed against the ``tests/pynix/test_lsp/terranix/``
    fixture): top-level key is ``provider_schemas``, keyed by
    ``<registry-host>/<namespace>/<type>`` (e.g.
    ``registry.opentofu.org/hashicorp/random``).
    """

    model_config = ConfigDict(extra="ignore")

    provider_schemas: dict[str, _ProviderSchemaEntry] = Field(default_factory=dict)


_MEM_CACHE: dict[str, ProviderSchemas] = {}
_CACHE_LOCK = anyio.Lock()


def _cache_dir() -> anyio.Path:
    """Return the XDG cache directory for terranix provider-schema JSON."""
    cache_home = os.environ.get("XDG_CACHE_HOME") or str(Path("~/.cache").expanduser())
    return anyio.Path(cache_home) / "pynix" / "terranix"


def _cache_key(tofu_out: str, module_out: str) -> str:
    return hashlib.sha256(f"{tofu_out}:{module_out}".encode()).hexdigest()[:16]


async def get_provider_schemas(tofu_out: str, module_out: str) -> ProviderSchemas:
    """Return the parsed ``tofu providers schema -json`` output for this pair of outputs.

    Cached in-memory for the server's lifetime, and on disk across restarts.
    """
    cache_key = _cache_key(tofu_out, module_out)
    cached = _MEM_CACHE.get(cache_key)
    if cached is not None:
        return cached
    async with _CACHE_LOCK:
        cached = _MEM_CACHE.get(cache_key)
        if cached is not None:
            return cached
        disk_path = _cache_dir() / f"{cache_key}.json"
        if await disk_path.exists():
            schemas = ProviderSchemas.model_validate_json(await disk_path.read_text())
        else:
            schemas = await _run_tofu_schema(tofu_out, module_out)
            await disk_path.parent.mkdir(parents=True, exist_ok=True)
            await disk_path.write_text(schemas.model_dump_json())
        _MEM_CACHE[cache_key] = schemas
        return schemas


_INIT_OK_MARKER = ".pynix-init-ok"


async def _run_tofu_schema(tofu_out: str, module_out: str) -> ProviderSchemas:
    """Run ``tofu init`` (if needed) then ``providers schema -json`` in a scratch dir.

    *tofu_out* is a directive's ``tofu`` output: a self-contained wrapper
    (see ``_terranix.py``'s module docstring) that already knows how to reach
    *module_out* on its own (typically via its own baked-in ``-chdir``) and
    already defaults ``$TF_DATA_DIR``/``$TF_STATE_PATH`` from ``$PWD`` if
    unset -- so this only needs to run it with a writable ``cwd``, never
    passing ``-chdir``/module path itself (that would double it up: the
    wrapper's own hardcoded ``-chdir`` plus this one, which OpenTofu rejects
    as two global flags after its subcommand).

    ``init`` is skipped whenever ``_INIT_OK_MARKER`` already exists in the
    scratch dir -- written by *this function itself*, only immediately after
    ``anyio.run_process``'s own successful (``check=True``, raises otherwise)
    completion of ``init``, so its presence really does mean init already
    succeeded here. (An earlier version instead checked for
    ``$TF_DATA_DIR`` existing as a *directory* as that signal, but that
    directory can exist without init ever having succeeded -- e.g. a prior
    invocation that failed after creating it but before finishing -- which
    silently left providers uninstalled for every later call reusing this
    scratch dir. Always unconditionally re-running init instead, the other
    alternative, was ruled out too: init isn't free, even when every
    provider is already pre-fetched into the Nix store via ``withPlugins``.)
    """
    state_dir = _cache_dir() / "state" / hashlib.sha256(module_out.encode()).hexdigest()[:16]
    await state_dir.mkdir(parents=True, exist_ok=True)
    tofu_bin = f"{tofu_out}/bin/tofu"
    init_ok_marker = state_dir / _INIT_OK_MARKER

    if not await init_ok_marker.exists():
        await anyio.run_process([tofu_bin, "init", "-input=false"], cwd=str(state_dir))
        await init_ok_marker.write_text("")

    completed = await anyio.run_process([tofu_bin, "providers", "schema", "-json"], cwd=str(state_dir))
    return ProviderSchemas.model_validate_json(completed.stdout)


def find_resource_block(schemas: ProviderSchemas, block_kind: str, resource_type: str) -> SchemaBlock | None:
    """Search every provider entry in *schemas* for *resource_type*'s schema block.

    *block_kind* is ``"resource"`` or ``"data"``, matching which of
    ``resource_schemas``/``data_source_schemas`` to search. Independent of
    which provider namespace actually declares the type.
    """
    for provider_entry in schemas.provider_schemas.values():
        table = provider_entry.resource_schemas if block_kind == "resource" else provider_entry.data_source_schemas
        entry = table.get(resource_type)
        if entry is not None:
            return entry.block
    return None


def list_resource_type_names(schemas: ProviderSchemas, block_kind: str) -> list[str]:
    """Every resource/data-source type name known across *every* provider in *schemas*.

    Unlike ``find_resource_block`` (one specific type), this is for
    completing the type name itself -- e.g. after typing ``resource.rand``,
    listing every ``random_*`` type any locked provider declares, not just
    ones already used somewhere in the file (that narrower "already
    configured" set is what the generic root-value fallback in
    ``_handlers.py`` already provides for free from a real Nix value; this is
    a superset sourced from the schema instead).
    """
    names: list[str] = []
    for provider_entry in schemas.provider_schemas.values():
        table = provider_entry.resource_schemas if block_kind == "resource" else provider_entry.data_source_schemas
        names.extend(table.keys())
    return names


def walk_schema_block(block: SchemaBlock, path: list[str]) -> SchemaAttribute | None:
    """Walk *path* (segments after ``resource.<type>.<name>``) through a schema block.

    A one-segment path (an attribute) resolves via ``block.attributes``. A
    longer path recurses into ``block.block_types[segment].block`` for each
    nested-block segment before the final attribute.
    """
    if not path:
        return None
    current = block
    for segment in path[:-1]:
        nested = current.block_types.get(segment)
        if nested is None:
            return None
        current = nested.block
    return current.attributes.get(path[-1])


def list_block_attributes(block: SchemaBlock, nested_path: list[str]) -> list[str] | None:
    """List attribute names available at *nested_path* (a sequence of nested-block segments) within *block*.

    Companion to ``walk_schema_block``, for completion rather than hover: no
    final attribute segment is consumed, since completion wants every name
    available at that level, not one specific one.
    """
    current = block
    for segment in nested_path:
        nested = current.block_types.get(segment)
        if nested is None:
            return None
        current = nested.block
    return list(current.attributes)


def render_schema_attribute(attribute: SchemaAttribute) -> str:
    """Render one schema attribute's description/type/required-ness as hover Markdown."""
    sections: list[str] = []
    if attribute.description:
        sections.append(attribute.description)
    if attribute.type is not None:
        sections.append(f"type: `{json.dumps(attribute.type)}`")
    if attribute.required:
        sections.append("required")
    elif attribute.optional:
        sections.append("optional")
    if attribute.computed:
        sections.append("computed")
    return "\n\n".join(sections) if sections else "```\nno schema description available\n```"
