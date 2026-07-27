"""Import-boundary drift guard for pynix's dependency on nanopynix.

pynix should depend on nanopynix's public surface only -- `nanopynix.*`,
`nanopynix.rpc.*`, `nanopynix.inproc.*` -- never nanopynix's internal
implementation modules (`nanopynix.rpc.client.*`) or the raw generated
proto package (`nanopynix_proto.*`), per nanopynix/AGENTS.md's narrow-
private-import rule. This walks every module under pynix/src/pynix via
`ast` (rather than grepping) so imports inside comments or docstrings
don't produce false positives.
"""

from __future__ import annotations

import ast
from pathlib import Path

_PYNIX_SRC = Path(__file__).resolve().parents[2] / "pynix" / "src" / "pynix"
_BANNED_PREFIXES = ("nanopynix.rpc.client", "nanopynix_proto")


def _banned_imports(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            names.add(node.module)
    return {
        name for name in names if any(name == prefix or name.startswith(f"{prefix}.") for prefix in _BANNED_PREFIXES)
    }


def test_pynix_does_not_import_nanopynix_internals_or_raw_proto() -> None:
    offenders = {
        str(path.relative_to(_PYNIX_SRC)): banned
        for path in sorted(_PYNIX_SRC.rglob("*.py"))
        for banned in [_banned_imports(ast.parse(path.read_text()))]
        if banned
    }
    assert not offenders, (
        "pynix must import nanopynix only through its public surface "
        "(nanopynix.*, nanopynix.rpc.*, nanopynix.inproc.*), not nanopynix.rpc.client.* "
        f"or nanopynix_proto.* directly: {offenders}"
    )
