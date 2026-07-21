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

from typing import TYPE_CHECKING

from nanopynix.exceptions import NixError
from pynix._lsp._syntax import top_level_lambda_formals
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
