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

import anyio

from nanopynix.exceptions import NixError

if TYPE_CHECKING:
    import nanopynix

_HEADER_LINE_RE = re.compile(
    r"^\s*#\s*pynix-lsp:\s*(?P<name>[A-Za-z_][A-Za-z0-9_'-]*)\s*=\s*(?P<expr>.+?)\s*$",
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


@dataclass
class _SharedEval:
    """One (expr, path) directive's shared evaluation outcome, plus how many open files currently depend on it."""

    session: nanopynix.EvalSession
    value: nanopynix.ValueProxy | None = None
    error: NixError | None = None
    refcount: int = 0


class SharedEvalCache:
    """Server-lifetime cache of already-evaluated directive expressions, keyed by ``(expr, base_dir)``.

    Two open files that declare the byte-identical ``# pynix-lsp: name =
    expr`` directive against the same base directory (e.g. two files under
    the same project sharing one ``moduleEntry``/``easykubenixEntry``
    import) evaluate to the exact same value -- a real module system's
    first ``_module.args``/``_module.specialArgs`` force can cost several
    real seconds (confirmed: ~8.7s cold vs. ~0.3s once the underlying
    ``EvalState`` has already forced it once), so re-paying that cost once
    per *file* rather than once per server session wastes real, user-visible
    time. Reference-counted so the underlying ``EvalSession`` is only closed
    once no open file still depends on it -- a fast open/close of one file
    while a sibling file sharing the same entry is still open must not tear
    down the value out from under it.
    """

    def __init__(self, nix_session: nanopynix.Session, store: nanopynix.Store) -> None:
        self._nix_session = nix_session
        self._store = store
        self._entries: dict[tuple[str, str], _SharedEval] = {}
        self._lock = anyio.Lock()

    async def acquire(
        self, expr: str, path: str
    ) -> tuple[nanopynix.EvalSession, nanopynix.ValueProxy | None, NixError | None]:
        """Evaluate *expr* (against base directory *path*) on first use, or hand back the already-evaluated result.

        Increments the entry's refcount either way -- callers must pair this
        with exactly one later ``release(expr, path)`` call.
        """
        key = (expr, path)
        async with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                session = self._nix_session.eval(self._store)
                await session.open()
                entry = _SharedEval(session)
                try:
                    entry.value = await session.string(expr, path=path)
                except NixError as exc:
                    entry.error = exc
                self._entries[key] = entry
            entry.refcount += 1
            return entry.session, entry.value, entry.error

    async def release(self, expr: str, path: str) -> None:
        """Drop one reference to the ``(expr, path)`` entry, closing its ``EvalSession`` once nothing else holds it."""
        key = (expr, path)
        async with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return
            entry.refcount -= 1
            if entry.refcount <= 0:
                del self._entries[key]
                await entry.session.close()

    async def close_all(self) -> None:
        """Force-close every remaining entry, regardless of refcount. Server shutdown only."""
        for entry in self._entries.values():
            await entry.session.close()
        self._entries.clear()


class FileContext:
    """One open file's evaluation context.

    Resolves one ``EvalSession``/root value per bound directive via a
    server-lifetime ``SharedEvalCache``, so two files sharing the same
    directive expression (and base directory) reuse the same already-warm
    evaluation instead of paying its cost again -- see
    ``SharedEvalCache``'s own docstring. Re-evaluating is deliberately not
    automatic on every edit -- only ``reload()`` (called when the directive
    text itself changes) re-runs the expressions, matching nixd's own
    eval-caching stance: once evaluated, a context is assumed not to change
    until something explicitly invalidates it.
    """

    def __init__(
        self,
        shared: SharedEvalCache,
        directives: list[ContextDirective],
        file_dir: str,
    ) -> None:
        self._shared = shared
        self.directives = directives
        self._file_dir = file_dir
        self._evals: dict[str, nanopynix.EvalSession] = {}
        self._acquired_keys: list[tuple[str, str]] = []
        self.roots: dict[str, nanopynix.ValueProxy] = {}
        self.errors: dict[str, NixError] = {}

    async def reload(self) -> None:
        """(Re-)evaluate every directive's expression, reusing a shared evaluation where one already exists."""
        await self.close()
        acquired_keys: list[tuple[str, str]] = []
        for directive in self.directives:
            key = (directive.expr, self._file_dir)
            eval_session, value, error = await self._shared.acquire(*key)
            acquired_keys.append(key)
            self._evals[directive.name] = eval_session
            if value is not None:
                self.roots[directive.name] = value
            if error is not None:
                self.errors[directive.name] = error
        self._acquired_keys = acquired_keys

    def eval_session(self, root_name: str) -> nanopynix.EvalSession | None:
        """The ``EvalSession`` that produced ``roots[root_name]``, if any.

        For auxiliary lookups against a value already known to live in that
        root's own evaluation graph (e.g. calling ``builtins.
        unsafeGetAttrPos`` with an existing ``ValueProxy`` as an argument) --
        a ``ValueProxy`` and the function it's passed to must come from the
        *same* ``EvalSession`` (each has its own independent ``EvalState``
        in the underlying worker, so value handles aren't interchangeable
        across sessions even within the same ``FileContext``).
        """
        return self._evals.get(root_name)

    async def close(self) -> None:
        """Release this context's reference to every directive's (possibly shared) evaluator. Idempotent."""
        for key in self._acquired_keys:
            await self._shared.release(*key)
        self._acquired_keys = []
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
