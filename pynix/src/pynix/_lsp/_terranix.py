"""terranix-specific inference for pynix's language server.

`terranix <https://github.com/terranix/terranix>`_ renders Nix modules into a
Terraform/OpenTofu ``config.tf.json``. A file opts in with one directive:

    # pynix-lsp: terranixEntry = import ./default.nix { }

Its expression must evaluate to an attrset shaped ``{ terranix, module,
moduleSystem, tofu }``:

- ``terranix`` -- the *raw* ``${pkgs.terranix.src}/core`` result: an attrset
  with only a ``.config`` (terranix's own wrapper strips ``_module``/``_ref``/
  ``__functor`` and never returns ``.options`` at all -- see ``moduleSystem``
  below for the fix).
- ``module`` -- a derivation whose output directory contains
  ``config.tf.json`` and a real ``.terraform.lock.hcl``, produced by an
  offline ``tofu init``.
- ``moduleSystem`` -- the *un*-sanitized ``lib.evalModules`` result for the
  same module list ``terranix`` was built from (same modules, same
  ``helpers.nix``-extended ``lib'`` terranix's own ``core/default.nix``
  builds internally) -- ``.config``/``.options``/``._module`` all present,
  exactly the shape ``_module_system.py`` expects from a NixOS
  ``moduleEntry``. terranix's own public wrapper never exposes this (see
  ``terranix`` above), so a directive has to build it by hand; see
  ``tests/pynix/test_lsp/terranix/default.nix`` for the reference
  construction.
- ``tofu`` -- a derivation with a ``bin/tofu``-shaped executable already
  wired (via ``-chdir`` or equivalent) to operate on ``module``.

(``tests/pynix/test_lsp/terranix/default.nix`` is the reference
implementation of this contract.)

Unlike NixOS's options tree, Terraform provider resource/attribute schemas
are not Nix values at all -- they only exist in ``tofu providers schema
-json``'s external JSON output (see ``_terranix_schema.py``, which fetches
and caches it, keyed by ``tofu``/``module``'s Nix store output paths). This
module bridges three things: ``derive_roots`` binds terranix's own already-
a-Nix-value config tree (for value-level hover/completion of already-typed
attributes, reusing the generic root walk in ``_handlers.py``/``_context.py``
for free) *and* ``moduleSystem`` as a ``moduleEntry`` root (reusing
``_module_system.derive_module_roots``/``ModuleSystemDialect`` as-is, so a
terranix file's own top-level lambda formals -- e.g. ``pkgs`` via
``_module.args`` -- resolve the same way a NixOS module's do), while
``hover``/``complete`` here resolve schema-only knowledge (an attribute's
description/type) that no Nix value carries.
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING, cast

from lsprotocol import types

from nanopynix._typechecking import BEARTYPING
from nanopynix.exceptions import NixError
from pynix._jsonschema import FindingKind, list_properties, render, validate
from pynix._lsp._dialect import Dialect
from pynix._lsp._module_system import MODULE_ENTRY_NAME, derive_module_roots
from pynix._lsp._render import render_value
from pynix._lsp._syntax import (
    attrpath_range,
    completion_target_at,
    identifier_path_at,
    string_literal_completion_target_at,
    string_literal_path_at,
    suppressed_codes_on_line,
)
from pynix._lsp._terranix_jsonschema import block_to_json_schema
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

if TYPE_CHECKING or BEARTYPING:
    from collections.abc import Mapping

    import nanopynix
    from pynix._lsp._context import FileContext
    from pynix._lsp._terranix_schema import ProviderSchemas
    from pynix._lsp._tofu_core_schema import CoreBlockSchema

TERRANIX_ENTRY_NAME = "terranixEntry"
_BLOCK_KINDS = ("resource", "data")
_DEFAULT_TOFU_VERSION = "1.12"

# Segment-count thresholds for `<kind>.<type>.<instance>.<field>...`.
_PATH_DEPTH_KIND_TYPE = 2  # [kind, type]
_PATH_DEPTH_INSTANCE = 3  # [kind, type, instance]
_PATH_DEPTH_FIELD = 4  # [kind, type, instance, field...]
_DIAGNOSTIC_SOURCE = "pynix-lsp"


def _rule_code_for(block: SchemaBlock, path: tuple[str | int, ...]) -> str | None:
    """Disambiguate a generic ``UNKNOWN_PROPERTY`` finding into TF001 (attribute) vs TF002 (nested block type).

    ``block_to_json_schema`` folds both ``attributes`` and ``block_types``
    into the same JSON Schema ``properties`` dict, so the generic engine
    can't tell them apart -- this walks the *original* ``SchemaBlock`` (not
    the converted JSON Schema) to check which one the offending key actually
    was. Returns None (no diagnostic) if *path* doesn't resolve to a plain
    string-keyed location within *block*.
    """
    current = block
    for segment in path[:-1]:
        if not isinstance(segment, str):
            return None
        nested = current.block_types.get(segment)
        if nested is None:
            return None
        current = nested.block
    final = path[-1]
    if not isinstance(final, str):
        return None
    return "TF002" if final in current.block_types else "TF001"


def _build_diagnostic(source: str, path: tuple[str, ...], code: str, message: str) -> types.Diagnostic | None:
    """Build one diagnostic at *path*'s resolved source range, or drop it if suppressed on that line.

    Falls back to ``Position(0, 0)`` (mirrors ``_context_error_diagnostic``'s
    existing whole-directive fallback shape in ``_handlers.py``) when
    ``attrpath_range`` can't resolve *path* -- a finding that can't be
    precisely anchored is still reported, just without a precise range.
    """
    span = attrpath_range(source, path)
    line = span[0] if span is not None else 0
    if code in suppressed_codes_on_line(source, line):
        return None
    if span is not None:
        start = types.Position(span[0], span[1])
        end = types.Position(span[2], span[3])
    else:
        start = end = types.Position(0, 0)
    return types.Diagnostic(
        range=types.Range(start=start, end=end),
        message=message,
        severity=types.DiagnosticSeverity.Warning,
        code=code,
        source=_DIAGNOSTIC_SOURCE,
    )


def _render_resource_type(block: SchemaBlock) -> str:
    """Render a resource/data-source *type*'s own hover, from its JSON Schema conversion.

    Reuses ``block_to_json_schema`` (already the diagnostics path's schema
    source) rather than a bespoke renderer: whatever block-level
    ``description``/``deprecated`` the provider actually set (few do -- see
    ``_terranix_schema.py``'s ``SchemaBlock``) surfaces here for free via the
    generic ``pynix._jsonschema.render``, with the declared attribute names
    listed alongside so hovering the type name is still useful even when the
    provider left no block-level prose.
    """
    schema = block_to_json_schema(block)
    sections = [render(schema)]
    names = list_properties(schema, ())
    if names:
        sections.append("attributes: " + ", ".join(f"`{name}`" for name in sorted(names)))
    return "\n\n".join(sections)


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

        Also binds ``moduleSystem`` as a ``moduleEntry`` root and delegates to
        ``derive_module_roots`` (the exact function ``ModuleSystemDialect``
        itself uses for real NixOS modules) -- since ``DIALECTS`` runs
        ``ModuleSystemDialect`` *before* ``TerranixDialect`` (see
        ``_dialects.py``), its own ``derive_roots`` would already have run
        and found no ``moduleEntry`` bound yet by the time this method sets
        one; calling ``derive_module_roots`` again here, now that it's bound,
        is what actually gets ``config``/``options`` populated for a terranix
        file, independent of dialect ordering.
        """
        entry = context.roots.get(TERRANIX_ENTRY_NAME)
        if entry is None:
            return
        context.roots.setdefault(MODULE_ENTRY_NAME, entry.attr("moduleSystem"))
        derive_module_roots(context)
        config_root = entry.attr("terranix").attr("config")
        try:
            names = await config_root.attr_names()
        except NixError:
            return
        for name in names:
            context.roots.setdefault(name, config_root.attr(name))

    async def _resource_output(self, entry: nanopynix.rpc.ValueProxy, attr_name: str) -> str | None:
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

    async def _schema_for(
        self, context: FileContext, tofu_out: str, module_out: str
    ) -> ProviderSchemas | None:
        return await get_provider_schemas(tofu_out, module_out, await context.store_exec_prefix())

    async def _core_schema_for(self, context: FileContext, tofu_out: str) -> Mapping[str, CoreBlockSchema] | None:
        """Core (built-in) HCL block schema for whichever OpenTofu version *tofu_out* is.

        Falls back to ``_DEFAULT_TOFU_VERSION`` if the real binary's version
        can't be detected (e.g. an unusual wrapper) -- a slightly-off core
        schema is still far more useful for meta-arguments like
        ``count``/``for_each``/``lifecycle`` than none at all, and the core
        schema barely changes between versions anyway.
        """
        version = await self._detect_tofu_version_cached(context, tofu_out)
        return await get_core_schema(version)

    async def _detect_tofu_version_cached(self, context: FileContext, tofu_out: str) -> str:
        """Memoized ``detect_tofu_version``, keyed by *tofu_out*'s (content-addressed) store path.

        Without this, every hover/completion request re-spawns ``tofu
        version -json`` -- a real subprocess, not free on every keystroke.
        The store path never changes without a different derivation, so this
        cache can never go stale.
        """
        cached = self._version_cache.get(tofu_out)
        if cached is not None:
            return cached
        version = (
            await detect_tofu_version(f"{tofu_out}/bin/tofu", await context.store_exec_prefix())
            or _DEFAULT_TOFU_VERSION
        )
        self._version_cache[tofu_out] = version
        return version

    def _schema_path_at(self, source: str, byte_offset: int) -> list[str] | None:
        path = identifier_path_at(source, byte_offset)
        if path is None:
            string_path = string_literal_path_at(source, byte_offset)
            if string_path is None:
                return None
            path = ["resource", *string_path]
        if len(path) < _PATH_DEPTH_KIND_TYPE or path[0] not in _BLOCK_KINDS:
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
        schemas = await self._schema_for(context, tofu_out, module_out)
        provider_block = find_resource_block(schemas, block_kind, resource_type) if schemas is not None else None
        core_schema = await self._core_schema_for(context, tofu_out)
        core_block = core_schema.get(block_kind) if core_schema is not None else None
        if provider_block is None:
            return core_block.block if core_block is not None else None
        if core_block is None:
            return provider_block
        return merge_schema_blocks(provider_block, core_block.block)

    async def hover(
        self,
        context: FileContext,
        source: str,
        byte_offset: int,
        dialects: list[Dialect],
    ) -> str | None:
        path = self._schema_path_at(source, byte_offset)
        if path is None:
            return None
        block = await self._block_for(context, path[0], path[1])
        if block is None:
            return None
        if len(path) < _PATH_DEPTH_FIELD:
            # Cursor is on the resource/data *type* name itself (``path`` has
            # only ``[kind, type]`` or ``[kind, type, instance]`` segments) --
            # there's no specific attribute to describe, so this renders the
            # block as a whole via the same JSON Schema conversion the
            # diagnostics path already uses, rather than a separate bespoke
            # renderer.
            return _render_resource_type(block)
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
        self,
        context: FileContext,
        path: list[str],
    ) -> nanopynix.rpc.ValueProxy | None:
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

    async def complete(  # noqa: C901, PLR0911 tracked complexity/arg-count debt, see TODO.md
        self,
        context: FileContext,
        source: str,
        byte_offset: int,
        dialects: list[Dialect],
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
            schemas = await self._schema_for(context, tofu_out, module_out)
            if schemas is None:
                return None
            names = list_resource_type_names(schemas, prefix[0])
            return [types.CompletionItem(label=name) for name in names if name.startswith(partial)]
        if len(prefix) < _PATH_DEPTH_INSTANCE or prefix[0] not in _BLOCK_KINDS:
            return None
        block = await self._block_for(context, prefix[0], prefix[1])
        if block is None:
            return None
        names = list_block_attributes(block, prefix[3:])
        if names is None:
            return None
        return [types.CompletionItem(label=name) for name in names if name.startswith(partial)]

    async def diagnostics(self, context: FileContext, source: str) -> list[types.Diagnostic] | None:  # noqa: C901, PLR0912 tracked complexity/arg-count debt, see TODO.md
        """Flag unknown attributes/nested-block types on every resource/data instance declared in *source*.

        ``context.roots`` reflects the *whole* terranix project (every
        module the directive's own ``import ../default.nix { }``-style
        expression pulls in), not just the currently open document -- a
        cross-resource reference in this repo's own fixtures resolves to a
        different file entirely. Diagnostics must NOT be reported against
        instances declared in some *other* file though (their positions
        would resolve against the wrong document's text) -- the
        ``attrpath_range(source, ...)`` existence check below filters the
        merged-project instance list down to just the ones actually written
        in *this* document.

        v1 only emits TF001 (unknown attribute) and TF002 (unknown nested
        block type) -- TF003 (missing required)/TF004 (type mismatch) are
        real findings ``pynix._jsonschema.validate`` already produces, but
        are deliberately filtered out here since they have no assigned,
        suppressible rule code yet.
        """
        found: list[types.Diagnostic] = []
        for block_kind in _BLOCK_KINDS:
            root = context.roots.get(block_kind)
            if root is None:
                continue
            try:
                resource_types = await root.attr_names()
            except NixError:
                continue
            for resource_type in resource_types:
                block = await self._block_for(context, block_kind, resource_type)
                if block is None:
                    continue
                schema = block_to_json_schema(block)
                type_root = root.attr(resource_type)
                try:
                    instance_names = await type_root.attr_names()
                except NixError:
                    continue
                for instance_name in instance_names:
                    if attrpath_range(source, (block_kind, resource_type, instance_name)) is None:
                        continue
                    try:
                        value = await type_root.attr(instance_name).to_python()
                    except NixError:
                        continue
                    for finding in validate(schema, value):
                        if finding.kind is not FindingKind.UNKNOWN_PROPERTY:
                            continue
                        code = _rule_code_for(block, finding.path)
                        if code is None:
                            continue
                        full_path_raw = (block_kind, resource_type, instance_name, *finding.path)
                        if not all(isinstance(segment, str) for segment in full_path_raw):
                            continue
                        full_path = cast("tuple[str, ...]", full_path_raw)
                        diagnostic = _build_diagnostic(source, full_path, code, finding.message)
                        if diagnostic is not None:
                            found.append(diagnostic)
        return found
