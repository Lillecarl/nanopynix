"""terranix-specific inference for pynix's language server.

`terranix <https://github.com/terranix/terranix>`_ renders Nix modules into a
Terraform/OpenTofu ``config.tf.json``. A file opts in with one directive:

    # pynix-lsp: terranixEntry = import ./default.nix { }

Its expression must evaluate to an attrset shaped ``{ terranix, module, tofu
}``:

- ``terranix`` -- the *raw* ``${pkgs.terranix.src}/core`` result (an
  ``lib.evalModules``-shaped attrset with a ``.config`` walkable exactly like
  NixOS's ``moduleEntry.config``, see ``_module_system.py``).
- ``module`` -- a derivation whose output directory contains
  ``config.tf.json`` and a real ``.terraform.lock.hcl``, produced by an
  offline ``tofu init``.
- ``tofu`` -- a derivation with a ``bin/tofu``-shaped executable already
  wired (via ``-chdir`` or equivalent) to operate on ``module``.

(``tests/pynix/test_lsp/terranix/default.nix`` is the reference
implementation of this contract.)

Unlike NixOS's options tree, Terraform provider resource/attribute schemas
are not Nix values at all -- they only exist in ``tofu providers schema
-json``'s external JSON output (see ``_terranix_schema.py``, which fetches
and caches it, keyed by ``tofu``/``module``'s Nix store output paths). This
module bridges the two: ``derive_roots`` binds terranix's own already-a-Nix-
value config tree (for value-level hover/completion of already-typed
attributes, reusing the generic root walk in ``_handlers.py``/``_context.py``
for free), while ``hover``/``complete`` resolve schema-only knowledge (an
attribute's description/type) that no Nix value carries.
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING

from lsprotocol import types

from nanopynix.exceptions import NixError
from pynix._lsp._dialect import Dialect
from pynix._lsp._render import render_value
from pynix._lsp._syntax import (
    completion_target_at,
    identifier_path_at,
    string_literal_completion_target_at,
    string_literal_path_at,
)
from pynix._lsp._terranix_schema import (
    SchemaBlock,
    find_resource_block,
    get_provider_schemas,
    list_block_attributes,
    list_resource_type_names,
    render_schema_attribute,
    walk_schema_block,
)
from pynix._lsp._tofu_core_schema import detect_tofu_version, get_core_schema, merge_schema_blocks

if TYPE_CHECKING:
    from collections.abc import Mapping

    import nanopynix
    from pynix._lsp._context import FileContext
    from pynix._lsp._terranix_schema import ProviderSchemas
    from pynix._lsp._tofu_core_schema import CoreBlockSchema

TERRANIX_ENTRY_NAME = "terranixEntry"
_BLOCK_KINDS = ("resource", "data")
_DEFAULT_TOFU_VERSION = "1.12"


class TerranixDialect(Dialect):
    """terranix support, as a peer ``Dialect`` (see ``_dialect.py``)."""

    def __init__(self) -> None:
        self._version_cache: dict[str, str] = {}

    async def derive_roots(self, context: FileContext) -> None:
        """Bind every top-level HCL block type (``resource``, ``output``, ...) as its own root.

        Unlike NixOS's fixed ``config``/``options`` pair, terranix has no
        fixed set of top-level names -- this binds whatever the project
        actually declared. terranix source text always carries its own
        block-type prefix (e.g. ``resource.random_id.suffix.byte_length =
        4;``), so this one hook is enough for the generic root walk to
        handle already-typed attribute paths; no bare-path fallback (unlike
        ``OPTIONS_NAME``) is needed here.
        """
        entry = context.roots.get(TERRANIX_ENTRY_NAME)
        if entry is None:
            return
        config_root = entry.attr("terranix").attr("config")
        try:
            names = await config_root.attr_names()
        except NixError:
            return
        for name in names:
            context.roots.setdefault(name, config_root.attr(name))

    async def _resource_output(self, entry: nanopynix.ValueProxy, attr_name: str) -> str | None:
        try:
            outputs = await entry.attr(attr_name).build()
        except NixError:
            return None
        return outputs.get("out")

    async def _outputs(self, context: FileContext) -> tuple[str, str] | None:
        entry = context.roots.get(TERRANIX_ENTRY_NAME)
        if entry is None:
            return None
        tofu_out = await self._resource_output(entry, "tofu")
        module_out = await self._resource_output(entry, "module")
        if tofu_out is None or module_out is None:
            return None
        return tofu_out, module_out

    async def _schema_for(self, tofu_out: str, module_out: str) -> ProviderSchemas | None:
        return await get_provider_schemas(tofu_out, module_out)

    async def _core_schema_for(self, tofu_out: str) -> Mapping[str, CoreBlockSchema] | None:
        """Core (built-in) HCL block schema for whichever OpenTofu version *tofu_out* is.

        Falls back to ``_DEFAULT_TOFU_VERSION`` if the real binary's version
        can't be detected (e.g. an unusual wrapper) -- a slightly-off core
        schema is still far more useful for meta-arguments like
        ``count``/``for_each``/``lifecycle`` than none at all, and the core
        schema barely changes between versions anyway.
        """
        version = await self._detect_tofu_version_cached(tofu_out)
        return await get_core_schema(version)

    async def _detect_tofu_version_cached(self, tofu_out: str) -> str:
        """Memoized ``detect_tofu_version``, keyed by *tofu_out*'s (content-addressed) store path.

        Without this, every hover/completion request re-spawns ``tofu
        version -json`` -- a real subprocess, not free on every keystroke.
        The store path never changes without a different derivation, so this
        cache can never go stale.
        """
        cached = self._version_cache.get(tofu_out)
        if cached is not None:
            return cached
        version = await detect_tofu_version(f"{tofu_out}/bin/tofu") or _DEFAULT_TOFU_VERSION
        self._version_cache[tofu_out] = version
        return version

    def _schema_path_at(self, source: str, byte_offset: int) -> list[str] | None:
        path = identifier_path_at(source, byte_offset)
        if path is None:
            string_path = string_literal_path_at(source, byte_offset)
            if string_path is None:
                return None
            path = ["resource", *string_path]
        if len(path) < 4 or path[0] not in _BLOCK_KINDS:
            return None
        return path

    async def _block_for(self, context: FileContext, block_kind: str, resource_type: str) -> SchemaBlock | None:
        """The merged provider+core schema block for ``<block_kind>.<resource_type>``.

        Provider schema (``byte_length``, ...) and core schema
        (``count``/``for_each``/``depends_on``/``lifecycle``, ...) are
        genuinely complementary -- a provider never redeclares OpenTofu's own
        meta-arguments, and core schema knows nothing about a specific
        provider's attributes -- so either alone being unavailable (e.g. an
        unrecognized resource type) shouldn't block hover/completion on what
        the other one does know.
        """
        outputs = await self._outputs(context)
        if outputs is None:
            return None
        tofu_out, module_out = outputs
        schemas = await self._schema_for(tofu_out, module_out)
        provider_block = find_resource_block(schemas, block_kind, resource_type) if schemas is not None else None
        core_schema = await self._core_schema_for(tofu_out)
        core_block = core_schema.get(block_kind) if core_schema is not None else None
        if provider_block is None:
            return core_block.block if core_block is not None else None
        if core_block is None:
            return provider_block
        return merge_schema_blocks(provider_block, core_block.block)

    async def hover(
        self, context: FileContext, source: str, byte_offset: int, dialects: list[Dialect]
    ) -> str | None:
        path = self._schema_path_at(source, byte_offset)
        if path is None:
            return None
        block = await self._block_for(context, path[0], path[1])
        if block is None:
            return None
        attribute = walk_schema_block(block, path[3:])
        if attribute is None:
            return None
        sections = [render_schema_attribute(attribute)]
        value = await self._resolve_assigned_value(context, path)
        if value is not None:
            # Blending in the assigned value is a bonus, not required -- the
            # schema section above is already a complete hover on its own.
            with contextlib.suppress(NixError):
                sections.append(await render_value(value, dialects))
        return "\n\n".join(sections)

    async def _resolve_assigned_value(
        self, context: FileContext, path: list[str]
    ) -> nanopynix.ValueProxy | None:
        if path[0] not in context.roots:
            return None
        value = context.roots[path[0]]
        try:
            for segment in path[1:]:
                value = value.attr(segment)
            await value.get_type()
        except NixError:
            return None
        return value

    async def complete(
        self, context: FileContext, source: str, byte_offset: int, dialects: list[Dialect]
    ) -> list[types.CompletionItem] | None:
        del dialects
        # The string-literal check is tried first: it's strictly AST-gated
        # (only matches inside a real, non-interpolated string_fragment), so
        # it can't misfire outside a string. completion_target_at can't be
        # trusted to signal "not in a string" via returning None -- its
        # lexical fallback tier is text-only and doesn't know about quotes,
        # so it happily (and wrongly) treats the opening `"` as a word
        # boundary and returns a same-shaped-but-wrong (too-short, missing
        # the implicit "resource" prefix) result for text inside a string
        # too.
        string_target = string_literal_completion_target_at(source, byte_offset)
        if string_target is not None:
            string_prefix, partial = string_target
            prefix = ["resource", *string_prefix]
        else:
            target = completion_target_at(source, byte_offset)
            if target is None:
                return None
            prefix, partial = target
        if not prefix:
            # Bare top-level keyword completion (e.g. typing "res" -> "resource")
            # never needs a Nix build -- ``tools/tofu-core-schema`` is a plain
            # subprocess call keyed only on a version string, so it stays
            # fast even before the project's `tofu` output has ever been
            # built.
            core_schema = await get_core_schema(_DEFAULT_TOFU_VERSION)
            if core_schema is None:
                return None
            return [types.CompletionItem(label=name) for name in core_schema if name.startswith(partial)]
        if len(prefix) == 1 and prefix[0] in _BLOCK_KINDS:
            # Resource/data *type* name completion (e.g. "resource.rand" ->
            # "random_id", "random_password", ...), sourced from every locked
            # provider's full schema -- a superset of whatever's already
            # configured in the file. Returning None here (rather than an
            # empty list) on any failure lets `_handlers.py`'s generic
            # root-value fallback still offer the narrower "already
            # configured types" list from a real Nix value, instead of
            # silently offering nothing.
            outputs = await self._outputs(context)
            if outputs is None:
                return None
            tofu_out, module_out = outputs
            schemas = await self._schema_for(tofu_out, module_out)
            if schemas is None:
                return None
            names = list_resource_type_names(schemas, prefix[0])
            return [types.CompletionItem(label=name) for name in names if name.startswith(partial)]
        if len(prefix) < 3 or prefix[0] not in _BLOCK_KINDS:
            return None
        block = await self._block_for(context, prefix[0], prefix[1])
        if block is None:
            return None
        names = list_block_attributes(block, prefix[3:])
        if names is None:
            return None
        return [types.CompletionItem(label=name) for name in names if name.startswith(partial)]
