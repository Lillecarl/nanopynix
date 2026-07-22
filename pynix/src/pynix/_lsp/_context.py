"""Per-file evaluation context for pynix's Nix language server.

A file opts in to real evaluation with one or more header comments near its
top:

    # pynix-lsp: cfg = (import ./flake.nix).nixosConfigurations.myhost.config.services.foo
    # pynix-lsp: options = (import ./flake.nix).nixosConfigurations.myhost.options.services.foo

Each becomes a bound name: any attribute path rooted at that name elsewhere
in the file (e.g. ``cfg.enable``) is resolved through the expression's
evaluated value for hover and completion. ``options`` is a conventional name,
not a hardcoded one -- it is used as a fallback root for attribute paths that
don't start with any bound name at all, which is the shape a module's own
option *definitions* take (e.g. typing ``services.foo`` inside that module's
``config = { ... }`` block has no ``cfg.``/``options.`` prefix to match).
Each expression is evaluated with the file's own directory as its base path,
so relative imports inside it (``./flake.nix`` above) resolve exactly as they
would if written directly in the file.

A NixOS module file is a special case worth its own directive,
``moduleEntry`` -- see ``_module_system.py``'s module docstring for what it
does and why. This module itself has no dialect-specific knowledge: after
``reload()`` evaluates every directive's expression, ``_handlers.py`` gives
each registered ``Dialect`` (see ``_dialect.py``) a chance to derive further
roots (e.g. ``moduleEntry`` -> ``config``/``options``).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from nanopynix.exceptions import NixError

if TYPE_CHECKING:
    import nanopynix

_HEADER_LINE_RE = re.compile(
    r"^\s*#\s*pynix-lsp:\s*(?P<name>[A-Za-z_][A-Za-z0-9_'-]*)\s*=\s*(?P<expr>.+?)\s*$"
)
_HEADER_SCAN_LINES = 5


@dataclass(frozen=True)
class ContextDirective:
    """A parsed ``# pynix-lsp: name = expr`` header comment.

    ``line`` is the directive's 0-indexed source line, so a failed
    evaluation can be reported as a diagnostic at the line that actually
    caused it instead of always pointing at the top of the file.
    """

    name: str
    expr: str
    line: int


def parse_directives(source: str) -> list[ContextDirective]:
    """Return every ``# pynix-lsp:`` header-comment directive this file declares.

    Only the first few non-blank lines are scanned, so a license header may
    precede them. Returns an empty list if the file has none.
    """
    directives: list[ContextDirective] = []
    scanned = 0
    for line_number, line in enumerate(source.splitlines()):
        if not line.strip():
            continue
        if scanned >= _HEADER_SCAN_LINES:
            break
        scanned += 1
        match = _HEADER_LINE_RE.match(line)
        if match is not None:
            directives.append(ContextDirective(match.group("name"), match.group("expr"), line_number))
    return directives


class FileContext:
    """One open file's evaluation context.

    Owns one dedicated ``EvalSession`` per bound directive (from a shared,
    server-lifetime ``Session``/``Store``) that evaluates that directive's
    expression. Re-evaluating is deliberately not automatic on every edit --
    only ``reload()`` (called when the directive text itself changes) re-runs
    the expressions, matching nixd's own eval-caching stance: once evaluated,
    a context is assumed not to change until something explicitly
    invalidates it.
    """

    def __init__(
        self,
        nix_session: nanopynix.Session,
        store: nanopynix.Store,
        directives: list[ContextDirective],
        file_dir: str,
    ) -> None:
        self._nix_session = nix_session
        self._store = store
        self.directives = directives
        self._file_dir = file_dir
        self._evals: dict[str, nanopynix.EvalSession] = {}
        self.roots: dict[str, nanopynix.ValueProxy] = {}
        self.errors: dict[str, NixError] = {}

    async def reload(self) -> None:
        """(Re-)evaluate every directive's expression."""
        await self.close()
        for directive in self.directives:
            eval_session = self._nix_session.eval(self._store)
            await eval_session.open()
            self._evals[directive.name] = eval_session
            try:
                self.roots[directive.name] = await eval_session.string(directive.expr, path=self._file_dir)
            except NixError as exc:
                self.errors[directive.name] = exc

    async def close(self) -> None:
        """Release every directive's evaluator. Idempotent."""
        for eval_session in self._evals.values():
            await eval_session.close()
        self._evals.clear()
        self.roots.clear()
        self.errors.clear()


async def resolve_root_path(context: FileContext, path: list[str]) -> nanopynix.ValueProxy | None:
    """Walk *path* through one of *context*'s bound roots by name (``path[0]``).

    Works for any name a directive bound directly, or a ``Dialect.derive_roots``
    hook added afterward (e.g. NixOS's derived ``config``/``options``, or
    terranix's derived ``resource``/``output``/...). Anything else -- a path
    whose first segment isn't a bound root at all -- is a dialect's own
    concern (see ``_dialect.Dialect.hover``/``.complete``).
    """
    if not path or path[0] not in context.roots:
        return None
    value = context.roots[path[0]]
    for segment in path[1:]:
        value = value.attr(segment)
    return value
