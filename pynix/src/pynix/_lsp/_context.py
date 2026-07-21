"""Per-file evaluation context for pynix's Nix language server.

A file opts in to real evaluation with a header comment near its top:

    # pynix-lsp: cfg = (import ./flake.nix).nixosConfigurations.myhost.config.services.foo

``cfg`` becomes a bound name: any attribute path rooted at ``cfg`` elsewhere
in the file (e.g. ``cfg.enable``) is resolved through the expression's
evaluated value for hover and completion. The expression is evaluated with
the file's own directory as its base path, so relative imports inside it
(``./flake.nix`` above) resolve exactly as they would if written directly in
the file.
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
    """A parsed ``# pynix-lsp: name = expr`` header comment."""

    name: str
    expr: str


def parse_directive(source: str) -> ContextDirective | None:
    """Return this file's context directive, if it has one.

    Only the first few non-blank lines are scanned, so a license header may
    precede the directive.
    """
    non_blank = [line for line in source.splitlines() if line.strip()]
    for line in non_blank[:_HEADER_SCAN_LINES]:
        match = _HEADER_LINE_RE.match(line)
        if match is not None:
            return ContextDirective(match.group("name"), match.group("expr"))
    return None


class FileContext:
    """One open file's evaluation context.

    Owns a dedicated ``EvalSession`` (from a shared, server-lifetime
    ``Session``/``Store``) that evaluates this file's directive expression.
    Re-evaluating is deliberately not automatic on every edit — only
    ``reload()`` (called when the directive text itself changes) re-runs the
    expression, matching nixd's own eval-caching stance: once evaluated, a
    context is assumed not to change until something explicitly invalidates
    it.
    """

    def __init__(
        self,
        nix_session: nanopynix.Session,
        store: nanopynix.Store,
        directive: ContextDirective,
        file_dir: str,
    ) -> None:
        self._nix_session = nix_session
        self._store = store
        self.directive = directive
        self._file_dir = file_dir
        self._eval: nanopynix.EvalSession | None = None
        self.root: nanopynix.ValueProxy | None = None
        self.error: NixError | None = None

    async def reload(self) -> None:
        """(Re-)evaluate this context's directive expression."""
        await self.close()
        eval_session = self._nix_session.eval(self._store)
        await eval_session.open()
        self._eval = eval_session
        try:
            self.root = await eval_session.string(self.directive.expr, path=self._file_dir)
            self.error = None
        except NixError as exc:
            self.root = None
            self.error = exc

    async def close(self) -> None:
        """Release this context's evaluator. Idempotent."""
        if self._eval is not None:
            await self._eval.close()
            self._eval = None
        self.root = None
