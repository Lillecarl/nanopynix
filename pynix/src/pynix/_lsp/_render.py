"""Generic hover rendering for a resolved Nix value, dialect-extensible."""

from __future__ import annotations

from typing import TYPE_CHECKING

import nanopynix
from nanopynix.exceptions import NixError
from pynix._value_render import format_json

if TYPE_CHECKING:
    from pynix._lsp._dialect import Dialect


async def render_value(value: nanopynix.rpc.ValueProxy, dialects: list[Dialect]) -> str:  # noqa: C901 tracked complexity/arg-count debt, see TODO.md
    """Render *value* as hover Markdown, letting *dialects* supply extra sections.

    The first dialect whose ``extra_hover_sections`` returns non-None wins,
    replacing the generic JSON dump (e.g. a NixOS ``mkOption`` declaration's
    description/type/default, rendered by ``ModuleSystemDialect`` instead of
    a useless ``__toString``'d location string).
    """
    nix_type = await value.get_type()
    sections = [f"```\n{nix_type.name.lower()}\n```"]
    extra: list[str] | None = None
    if nix_type == nanopynix.NixType.ATTRS:
        for dialect in dialects:
            extra = await dialect.extra_hover_sections(value)
            if extra is not None:
                break
    if extra is not None:
        sections.extend(extra)
    elif nix_type != nanopynix.NixType.FUNCTION:
        try:
            json_value = await value.to_python()
        except NixError:
            pass
        else:
            sections.append(f"```json\n{format_json(json_value)}\n```")
    try:
        edit_path, edit_line = await value.edit_location()
    except NixError:
        pass
    else:
        if edit_path:
            sections.append(f"defined at `{edit_path}:{edit_line}`")
    return "\n\n".join(sections)
