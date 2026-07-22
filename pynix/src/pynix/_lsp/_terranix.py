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
from pynix._lsp._syntax import completion_target_at, identifier_path_at, string_literal_path_at
from pynix._lsp._terranix_schema import (
    find_resource_block,
    get_provider_schemas,
    list_block_attributes,
    render_schema_attribute,
    walk_schema_block,
)

if TYPE_CHECKING:
    import nanopynix
    from pynix._lsp._context import FileContext
    from pynix._lsp._terranix_schema import ProviderSchemas

TERRANIX_ENTRY_NAME = "terranixEntry"
_BLOCK_KINDS = ("resource", "data")


class TerranixDialect(Dialect):
    """terranix support, as a peer ``Dialect`` (see ``_dialect.py``)."""

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

    async def _schema_for(self, context: FileContext) -> ProviderSchemas | None:
        entry = context.roots.get(TERRANIX_ENTRY_NAME)
        if entry is None:
            return None
        tofu_out = await self._resource_output(entry, "tofu")
        module_out = await self._resource_output(entry, "module")
        if tofu_out is None or module_out is None:
            return None
        return await get_provider_schemas(tofu_out, module_out)

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

    async def hover(
        self, context: FileContext, source: str, byte_offset: int, dialects: list[Dialect]
    ) -> str | None:
        path = self._schema_path_at(source, byte_offset)
        if path is None:
            return None
        schemas = await self._schema_for(context)
        if schemas is None:
            return None
        block = find_resource_block(schemas, path[0], path[1])
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
        target = completion_target_at(source, byte_offset)
        if target is None:
            return None
        prefix, partial = target
        if len(prefix) < 3 or prefix[0] not in _BLOCK_KINDS:
            return None
        schemas = await self._schema_for(context)
        if schemas is None:
            return None
        block = find_resource_block(schemas, prefix[0], prefix[1])
        if block is None:
            return None
        names = list_block_attributes(block, prefix[3:])
        if names is None:
            return None
        return [types.CompletionItem(label=name) for name in names if name.startswith(partial)]
