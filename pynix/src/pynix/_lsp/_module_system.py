"""NixOS module-system-specific inference for pynix's language server.

Writing a NixOS module is common enough to deserve first-class support
rather than making users spell out ``config``/``options``/every lambda
argument by hand via generic ``# pynix-lsp:`` directives (see
``_context.py``'s module docstring for those). A file opts in with one
directive:

    # pynix-lsp: moduleEntry = import ./flake.nix { }

Its expression must evaluate to the *raw* ``lib.evalModules`` result (the
attrset with ``.config``/``.options``/``._module`` siblings -- what
``nixosSystem`` calls internally, not its friendlier wrapper). Everything in
this module derives from that one root:

- ``config``/``options`` roots (``derive_module_roots``).
- Any other top-level lambda argument a module file declares, e.g. ``pkgs``
  (``resolve_formal_arg``/``resolve_module_arg``).
- Rendering an ``mkOption``-produced option declaration for hover
  (``is_option_declaration``/``render_option_declaration``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from lsprotocol import types
from pygls.uris import from_fs_path

from nanopynix.exceptions import NixError
from pynix._lsp._dialect import Dialect
from pynix._lsp._render import render_value
from pynix._lsp._syntax import completion_target_at, identifier_path_at, top_level_lambda_formals
from pynix._value_render import format_json

if TYPE_CHECKING:
    import nanopynix
    from pynix._lsp._context import FileContext

MODULE_ENTRY_NAME = "moduleEntry"
CONFIG_NAME = "config"
OPTIONS_NAME = "options"


def derive_module_roots(context: FileContext) -> None:
    """Derive ``config``/``options`` roots from a bound ``moduleEntry``, if any.

    A no-op if the file didn't bind ``moduleEntry``, or already bound
    ``config``/``options`` explicitly (those take precedence).
    """
    module_entry = context.roots.get(MODULE_ENTRY_NAME)
    if module_entry is None:
        return
    context.roots.setdefault(CONFIG_NAME, module_entry.attr(CONFIG_NAME))
    context.roots.setdefault(OPTIONS_NAME, module_entry.attr(OPTIONS_NAME))


async def resolve_module_arg(context: FileContext, name: str) -> nanopynix.ValueProxy | None:
    """Resolve *name* as a NixOS module-system top-level argument (e.g. ``pkgs``).

    Requires a ``moduleEntry`` root. A real module receives each of its own
    formal arguments as ``args.${name} or config._module.args.${name}``
    (nixpkgs' ``lib/modules.nix``, ``applyModuleArgs``), where ``args`` is
    built from ``specialArgs`` -- so ``_module.specialArgs`` is tried first,
    falling back to a module-contributed ``_module.args`` (the
    NixOS-idiomatic mechanism, e.g. how ``nixos/modules/misc/nixpkgs.nix``
    supplies ``pkgs`` by default in real configurations). Returns None if
    *name* is in neither, or there is no ``moduleEntry`` root at all.
    """
    module_entry = context.roots.get(MODULE_ENTRY_NAME)
    if module_entry is None:
        return None
    for source_attr in ("specialArgs", "args"):
        candidate = module_entry.attr("_module").attr(source_attr).attr(name)
        try:
            await candidate.get_type()
        except NixError:
            continue
        return candidate
    return None


async def resolve_formal_arg(context: FileContext, source: str, name: str) -> nanopynix.ValueProxy | None:
    """Resolve *name* as a module argument, gated on the file's own lambda declaring it.

    Combines ``top_level_lambda_formals`` (does this file's own top-level
    ``{ ..., name, ... }:`` actually take this argument?) with
    ``resolve_module_arg`` -- offering completion/hover for a name the file
    never imported would suggest something that isn't actually in scope,
    since real Nix would reject it as an undefined variable too.
    """
    if name not in (top_level_lambda_formals(source) or []):
        return None
    return await resolve_module_arg(context, name)


async def is_option_declaration(value: nanopynix.ValueProxy) -> bool:
    """True if *value* looks like an ``mkOption``-produced option declaration.

    These attrsets implement nixpkgs' ``__toString`` (returning their dotted
    location, e.g. ``"services.foo.port"``), so a plain ``force_json()`` just
    prints that location string instead of anything useful for hover --
    render their actual ``description``/``type``/``default`` fields
    explicitly instead, in ``render_option_declaration``.
    """
    try:
        marker = await value.attr("_type").force_json()
    except NixError:
        return False
    return marker == "option"


async def render_option_declaration(value: nanopynix.ValueProxy) -> list[str]:
    sections: list[str] = []
    try:
        description = await value.attr("description").force_json()
    except NixError:
        pass
    else:
        if isinstance(description, str):
            sections.append(description)
    try:
        type_description = await value.attr("type").attr("description").force_json()
    except NixError:
        pass
    else:
        if isinstance(type_description, str):
            sections.append(f"type: `{type_description}`")
    try:
        default = await value.attr("default").force_json()
    except NixError:
        pass
    else:
        sections.append(f"default:\n```json\n{format_json(default)}\n```")
    return sections


def _position_from_json(entry: object) -> types.Location | None:
    """Build a ``Location`` from a nixpkgs-shaped ``{file, line, column}`` dict (1-based line/column)."""
    if not isinstance(entry, dict):
        return None
    # isinstance narrows to the bare, generic-less `dict[Unknown, Unknown]`
    # (isinstance can't check generic parameters at runtime) -- re-asserting
    # the type here is what stops that Unknown from propagating into the
    # .get() calls below (same idiom as _easykubenix_schema.py's _gvk_index).
    entry = cast("dict[str, Any]", entry)
    file = entry.get("file")
    line = entry.get("line")
    column = entry.get("column")
    if not isinstance(file, str) or not isinstance(line, int) or not isinstance(column, int):
        return None
    uri = from_fs_path(file)
    if uri is None:
        return None
    position = types.Position(max(line - 1, 0), max(column - 1, 0))
    return types.Location(uri=uri, range=types.Range(position, position))


async def _option_declaration_locations(value: nanopynix.ValueProxy) -> list[types.Location] | None:
    """Every declaration site of an ``mkOption``-produced *value*, from its own ``declarationPositions``.

    nixpkgs' ``lib/options.nix`` already tracks this explicitly (an option
    can be declared, in the plain ``mkOption`` sense, in only one place, but
    the field is still a list for symmetry with ``definitionsWithLocations``)
    -- more reliable than calling ``builtins.unsafeGetAttrPos`` ourselves,
    which returns null for most values that pass through the module
    system's fixed-point merging (see this feature's design notes).
    """
    try:
        positions = await value.attr("declarationPositions").force_json()
    except NixError:
        return None
    if not isinstance(positions, list):
        return None
    locations = [location for entry in positions if (location := _position_from_json(entry)) is not None]
    return locations or None


class ModuleSystemDialect(Dialect):
    """NixOS module-system support, as a peer ``Dialect`` (see ``_dialect.py``).

    ``_handlers.py`` tries every dialect's ``hover``/``complete`` before
    falling back to the generic ``resolve_root_path`` walk (some dialects,
    e.g. terranix's schema lookup, must override what a plain root walk
    would show). This dialect must therefore explicitly defer -- return None
    -- whenever *path* already starts with a bound root name, so that
    ``moduleEntry``-derived ``config``/``options`` paths (e.g. ``config.foo``)
    still resolve via the generic walk exactly as before; it only ever
    handles what that generic walk can't reach on its own: a module-argument
    reference (e.g. ``pkgs.hello``) and the bare-attrpath-definition fallback
    to ``options``.
    """

    async def derive_roots(self, context: FileContext) -> None:
        derive_module_roots(context)

    async def extra_hover_sections(self, value: nanopynix.ValueProxy) -> list[str] | None:
        if await is_option_declaration(value):
            return await render_option_declaration(value)
        return None

    async def _resolve(self, context: FileContext, source: str, path: list[str]) -> nanopynix.ValueProxy | None:
        if path and path[0] in context.roots:
            return None
        if path:
            module_arg_root = await resolve_formal_arg(context, source, path[0])
            if module_arg_root is not None:
                value = module_arg_root
                for segment in path[1:]:
                    value = value.attr(segment)
                return value
        if OPTIONS_NAME in context.roots:
            value = context.roots[OPTIONS_NAME]
            for segment in path:
                value = value.attr(segment)
            return value
        return None

    async def hover(
        self, context: FileContext, source: str, byte_offset: int, dialects: list[Dialect]
    ) -> str | None:
        path = identifier_path_at(source, byte_offset)
        if path is None:
            return None
        value = await self._resolve(context, source, path)
        if value is None:
            return None
        try:
            return await render_value(value, dialects)
        except NixError:
            return None

    async def _best_effort_location(
        self, context: FileContext, source: str, path: list[str]
    ) -> types.Location | None:
        """Best-effort ``builtins.unsafeGetAttrPos`` lookup for a non-option *path* (e.g. a module arg like ``pkgs.hello``).

        Only attempted when *path* has a parent to look the last segment up
        in, and only when this context has a ``moduleEntry``-backed eval
        session to run it in (a ``ValueProxy`` and the function it's passed
        to must share the same ``EvalSession`` -- see
        ``FileContext.eval_session``'s docstring). Returns None on any
        failure, including nixpkgs' own composed package sets (built via
        ``makeScope``/overlays) not preserving plain-attrset position
        tracking -- a graceful "no definition available" is the correct
        outcome, not a bug, for most of nixpkgs' own attributes.
        """
        if len(path) < 2:
            return None
        eval_session = context.eval_session(MODULE_ENTRY_NAME)
        if eval_session is None:
            return None
        parent_value = await self._resolve(context, source, path[:-1])
        if parent_value is None:
            return None
        try:
            unsafe_get_attr_pos = await eval_session.string("builtins.unsafeGetAttrPos")
            position_value = await unsafe_get_attr_pos.call(path[-1], parent_value)
            position = await position_value.force_json()
        except NixError:
            return None
        return _position_from_json(position)

    async def definition(
        self, context: FileContext, source: str, byte_offset: int, dialects: list[Dialect]
    ) -> types.Location | list[types.Location] | None:
        del dialects
        path = identifier_path_at(source, byte_offset)
        if path is None:
            return None
        value = await self._resolve(context, source, path)
        if value is None:
            return None
        if await is_option_declaration(value):
            return await _option_declaration_locations(value)
        return await self._best_effort_location(context, source, path)

    async def complete(
        self, context: FileContext, source: str, byte_offset: int, dialects: list[Dialect]
    ) -> list[types.CompletionItem] | None:
        del dialects
        target = completion_target_at(source, byte_offset)
        if target is None:
            return None
        prefix, partial = target
        value = await self._resolve(context, source, prefix)
        if value is None:
            return None
        if await is_option_declaration(value):
            # `prefix` landed exactly ON a leaf mkOption declaration (e.g.
            # `options.kubernetes.objects`, whose *value* type happens to be
            # `attrsOf (submodule ...)` but whose own options-tree node is
            # still just one option, not a namespace of further named
            # sub-options -- see the module docstring's note on
            # `attrsOf`-typed options not exposing per-instance `options.
            # <path>` recursion at all). Completing "further" here via plain
            # `attr_names()` would surface the option declaration's own
            # internal mkOption fields (`description`/`type`/`default`/...),
            # never a real completion candidate -- so this defers instead of
            # returning that noise, most importantly letting a later,
            # dialect-specific completion (e.g. EasykubenixDialect's
            # namespace/Kind-name completion) win instead of being shadowed
            # by this generic fallback "succeeding" with the wrong thing.
            return None
        try:
            names = await value.attr_names()
        except NixError:
            return None
        return [types.CompletionItem(label=name) for name in names if name.startswith(partial)]
