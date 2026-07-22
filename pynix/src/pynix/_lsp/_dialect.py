"""Extension point for pluggable, per-"flavor" Nix LSP support.

A ``Dialect`` gives a schema-aware capability (NixOS modules today; terranix,
and later something like easykubenix, as peers) a place to hook into hover/
completion without ``_handlers.py``/``_context.py`` special-casing it by
name. Every method defaults to a no-op -- a dialect overrides only what it
needs. Registered explicitly in ``_dialects.DIALECTS``; see that module's
docstring for why this is a plain list rather than plugin discovery.

``dialects: list[Dialect]`` is always passed down as a parameter rather than
imported from ``_dialects`` by a dialect module itself: importing the
registry from within one of its own entries would be a real import cycle
(``_dialects`` -> e.g. ``_terranix`` -> ``_dialects``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from lsprotocol import types

    import nanopynix
    from pynix._lsp._context import FileContext


class Dialect:
    """Base class for a pluggable Nix dialect. Override only what you need."""

    async def derive_roots(self, context: FileContext) -> None:
        """Mutate ``context.roots`` after a ``reload()``.

        Must only ``setdefault`` -- never override a user's own explicit
        ``# pynix-lsp:`` binding.
        """
        return

    async def extra_hover_sections(self, value: nanopynix.ValueProxy) -> list[str] | None:
        """Extra Markdown sections for a resolved *value*, replacing the generic type/JSON dump.

        Returns None to mean "nothing to add here, use the generic renderer."
        """
        return None

    async def hover(
        self,
        context: FileContext,
        source: str,
        byte_offset: int,
        dialects: list[Dialect],
    ) -> str | None:
        """Resolve hover for a path the generic ``resolve_root_path`` walk can't reach.

        *dialects* is threaded through so an implementation can reuse the
        shared value renderer (``_render.render_value``) without importing
        the registry itself.
        """
        return None

    async def complete(
        self,
        context: FileContext,
        source: str,
        byte_offset: int,
        dialects: list[Dialect],
    ) -> list[types.CompletionItem] | None:
        """Resolve completion for a path the generic ``resolve_root_path`` walk can't reach."""
        return None
