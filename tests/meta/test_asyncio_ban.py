"""The ban on the raw asyncio primitives must reach every project here.

`ruff-strict.toml` bans `asyncio.sleep`, `asyncio.Event` and the rest, and it
routes each one to its anyio equivalent. That gate does not read `pynixd/`
yet: `extend-exclude` holds it out while issue #131 brings that tree to the
conventions of this repository, one gate at a time.

The asyncio question of #131 was answered with a port, and not with an
exemption. `pynixd/ruff.toml` therefore repeats the ban, so that `checks.lint`
holds the port. A repeated list decays, so this module keeps the two equal.

The third project under `pynixd/` is `nix-daemon-protocol`, which is a codec
library with no asyncio primitive at all. It carries its own ruff
configuration and no ban, so the last check below reads its source instead.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

from tests.support.suite_roots import REPO_ROOT

STRICT = REPO_ROOT / "ruff-strict.toml"
PYNIXD = REPO_ROOT / "pynixd" / "ruff.toml"
PROTOCOL_SOURCE = REPO_ROOT / "pynixd" / "nix-daemon-protocol" / "src"


def _banned_names(config: Path) -> set[str]:
    """Every name of the `banned-api` table of one ruff configuration."""
    loaded = tomllib.loads(config.read_text())
    table = loaded.get("lint", {}).get("flake8-tidy-imports", {}).get("banned-api", {})
    return {name for name in table if name.startswith("asyncio.")}


def test_pynixd_bans_the_same_asyncio_names_as_the_strict_gate() -> None:
    """A name added to one list and not to the other is a ban with a hole.

    The failure to expect: `ruff-strict.toml` gains a ban, nanopynix meets it,
    and pynixd keeps the primitive because no gate of pynixd knows about it.
    """
    strict = _banned_names(STRICT)
    pynixd = _banned_names(PYNIXD)
    assert strict, f"{STRICT} names no asyncio ban, and it is the source of this list"
    missing = sorted(strict - pynixd)
    extra = sorted(pynixd - strict)
    assert not missing, (
        f"pynixd/ruff.toml does not ban {missing}. "
        "Add each name there, with the same message, or record why pynixd is different."
    )
    assert not extra, (
        f"pynixd/ruff.toml bans {extra}, and ruff-strict.toml does not. "
        "Add each name to ruff-strict.toml, so that nanopynix meets the same rule."
    )


def test_the_protocol_library_uses_no_banned_asyncio_primitive() -> None:
    """`nix-daemon-protocol` has its own ruff configuration and no ban.

    It needs none, because it is a codec library and calls no asyncio
    primitive. This check is what makes that a fact rather than a memory. Ban
    the names in its `pyproject.toml` when it grows one.
    """
    banned = _banned_names(STRICT)
    offenders = [
        f"{path.relative_to(REPO_ROOT)}: {name}"
        for path in sorted(PROTOCOL_SOURCE.rglob("*.py"))
        for name in sorted(banned)
        if name in path.read_text()
    ]
    assert not offenders, (
        f"nix-daemon-protocol uses a banned asyncio primitive: {offenders}. "
        "Either use the anyio equivalent, or give that project the banned-api "
        "table of ruff-strict.toml and delete this check."
    )
