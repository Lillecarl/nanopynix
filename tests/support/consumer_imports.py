"""Find every import that a first-party consumer makes from ``nanopynix``.

CLAUDE.md says that ``pynix`` must depend on the public APIs of ``nanopynix``,
and that a narrow dependency on a private module is acceptable only when a
redesign is not justified. Nothing checked either half, and the private half
had drifted to 30 sites in three packages with no record of the decision.
``tests/meta/test_consumer_surface.py`` is where this scanner runs, for the
same reason as ``tests/support/suppressions.py``: pytest is the only gate that
CI executes on every Nix version and both backends.

An AST walk rather than a regular expression, because the rule is about the
import graph and not about the text. A regular expression cannot tell
``from nanopynix._core import CoreStore`` from the same words in a docstring,
and it cannot see that ``from nanopynix import _wire`` is also a private
import although the module named is public.

Three kinds of internal, and this module reports all three:

* a private **module**, where a component after ``nanopynix`` starts with an
  underscore, for example ``nanopynix._core._objects``;
* a private **name** out of a public module, for example
  ``from nanopynix import _wire``;
* a module on ``INTERNAL_PREFIXES``, which is internal although every
  component of its name looks public.

The third kind is why this module absorbed the older
``tests/pynix/test_import_boundaries.py`` rather than replacing it. That guard
named ``nanopynix.rpc.client`` and ``nanopynix_proto``, and the underscore rule
alone sees neither: the first has no underscore in it, and the second is a
different distribution. The underscore rule is the wider net, the denylist
catches what a naming convention cannot express, and the two together are what
the old guard plus this one covered separately.

``tests/`` is deliberately not a consumer. The suite is white-box by design: it
imports 38 private names over 17 modules to reach worker state and handle
registries, and ``ruff-strict.toml`` already grants ``tests/**`` the ``SLF001``
ignore for the same reason. Only the packages that consume nanopynix as a
library are scanned.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from tests.support.suppressions import iter_python_files as iter_python_files

if TYPE_CHECKING:
    from collections.abc import Iterable

# The packages that consume nanopynix as a library, relative to the repository
# root. `tests/` is absent on purpose -- see the module docstring.
CONSUMER_ROOTS = (
    "pynix/src",
    "ekn/src",
    "nanopynix-helpers/src",
    "docs",
)

# Modules that are internal although no component of the name says so, and
# which the underscore rule therefore cannot catch. Each entry matches the
# module itself and everything under it.
INTERNAL_PREFIXES = (
    # The rpc client's implementation modules. `nanopynix.rpc` is the public
    # door, and it re-exports what a caller needs.
    "nanopynix.rpc.client",
    # The generated protobuf package. It is a build product of the wire
    # format, not an API, and it is a separate distribution -- so it is not
    # under `nanopynix` at all and needs naming here to be seen.
    "nanopynix_proto",
)

# The name that stands in for `import nanopynix._core`, which imports the whole
# module rather than a name out of it.
WHOLE_MODULE = "*"


@dataclass(frozen=True)
class ConsumerImport:
    """One name that a consumer imports from ``nanopynix``."""

    path: Path
    line: int
    module: str
    name: str

    @property
    def key(self) -> tuple[str, str]:
        """The ledger key. A new use of an approved name is not a new key."""
        return (self.module, self.name)

    @property
    def is_private(self) -> bool:
        """True for any of the three kinds of internal -- see the docstring."""
        return _module_is_internal(self.module) or self.name.startswith("_")

    def __str__(self) -> str:
        target = self.module if self.name == WHOLE_MODULE else f"{self.module}.{self.name}"
        return f"{self.path}:{self.line}: {target}"


def _under(module: str, prefix: str) -> bool:
    """True when ``module`` is ``prefix`` itself, or a module under it."""
    return module == prefix or module.startswith(f"{prefix}.")


def _module_is_internal(module: str) -> bool:
    """True for an underscore component after ``nanopynix``, or a denylisted module."""
    if any(_under(module, prefix) for prefix in INTERNAL_PREFIXES):
        return True
    return any(part.startswith("_") for part in module.split(".")[1:])


def _is_nanopynix(module: str | None) -> bool:
    """True for a module this scanner tracks at all.

    ``nanopynix_proto`` is not under ``nanopynix``, so the prefix test alone
    would not see it. It is tracked because it is on ``INTERNAL_PREFIXES``.
    A package that merely starts with the same letters, such as
    ``nanopynix_helpers``, is a separate distribution and is not tracked.
    """
    if module is None:
        return False
    if _under(module, "nanopynix"):
        return True
    return any(_under(module, prefix) for prefix in INTERNAL_PREFIXES)


def scan_source(source: str, path: Path) -> list[ConsumerImport]:
    """Return every ``nanopynix`` import in ``source``.

    Raises rather than skipping on unreadable input: a file this cannot parse
    is a file whose imports went unchecked, and that must be loud.
    """
    found: list[ConsumerImport] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom):
            # A relative import has `module is None`, and no consumer reaches
            # nanopynix that way, because nanopynix is a separate package.
            if node.module is None or not _is_nanopynix(node.module):
                continue
            found.extend(ConsumerImport(path, node.lineno, node.module, alias.name) for alias in node.names)
        elif isinstance(node, ast.Import):
            found.extend(
                ConsumerImport(path, node.lineno, alias.name, WHOLE_MODULE)
                for alias in node.names
                if _is_nanopynix(alias.name)
            )
    return found


def scan_consumers(repo_root: Path, roots: Iterable[str] = CONSUMER_ROOTS) -> list[ConsumerImport]:
    """Return every ``nanopynix`` import that a consumer package makes."""
    found: list[ConsumerImport] = []
    for root in roots:
        base = repo_root / root
        for path in iter_python_files(base):
            found.extend(scan_source(path.read_text(encoding="utf-8"), path.relative_to(repo_root)))
    return found


def format_report(imports: Iterable[ConsumerImport]) -> str:
    """Render imports one per line, for a test failure."""
    return "\n".join(str(i) for i in sorted(imports, key=str))
