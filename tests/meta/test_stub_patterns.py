"""Every stubgen pattern key must be anchored, and must name a real attribute.

``nanopynix-bindings/src/*.pat`` corrects the type stub that nanobind's
stubgen writes. A pattern is a key, and the lines below it replace what
stubgen produced for the attribute the key names.

**The key is a regular expression, and stubgen applies it with ``re.search``**
(``nanobind/stubgen.py``, ``apply_pattern``). A key with no ``$`` is therefore
a substring pattern, and it claims every longer name that starts the same way.
That is not a theory: ``Store.read_derivation`` also claimed
``Store.read_derivation_typed``, so the typed method reported the return type
of the dictionary one. The stub still type-checked, and it described the wrong
type. ``query_path_info`` and ``query_missing`` did the same.

This file makes the two failures loud:

* **An unanchored key.** The next method whose name extends an existing one
  silently takes the wrong signature.
* **A key that names nothing.** stubgen prints a warning and continues, so a
  renamed attribute leaves a dead pattern behind and the real attribute keeps
  whatever stubgen guessed. ``store.pat`` carries one such key for every
  property that builds an ``nb::list``, and each one would go back to a bare
  ``list`` with no notice.

``AGENTS.md`` states the rule this file follows: a convention that a machine
can check belongs in ``tests/meta/``.
"""

from __future__ import annotations

import importlib
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
PATTERN_DIR = REPO_ROOT / "nanopynix-bindings" / "src"

# A key is a line that starts in the first column and ends with a colon.
# Everything else in the file is a comment, a blank line, or an indented
# replacement line.
KEY_LINE = re.compile(r"^(?P<key>\S.*):$")

# stubgen calls these on the module itself, and they name no attribute.
MODULE_HOOKS = ("__prefix__", "__suffix__")


def pattern_files() -> list[Path]:
    return sorted(PATTERN_DIR.glob("*.pat"))


def keys_of(path: Path) -> list[str]:
    return [m.group("key") for line in path.read_text().splitlines() if (m := KEY_LINE.match(line))]


def test_the_scanner_finds_the_pattern_files() -> None:
    """A scanner that matched nothing would leave this file green forever."""
    found = pattern_files()
    assert found, f"no *.pat under {PATTERN_DIR}; is the source tree present?"
    assert any(p.name == "store.pat" for p in found)


@pytest.mark.parametrize("path", pattern_files(), ids=lambda p: p.name)
class TestPatternKeys:
    def test_the_scanner_reads_real_keys(self, path: Path) -> None:
        """A file whose keys all failed to parse would excuse every defect."""
        keys = keys_of(path)
        assert len(keys) > 1, f"{path.name} parsed {len(keys)} keys, which is too few to be right"
        assert all(key.startswith("nanopynix_bindings.") for key in keys), (
            f"{path.name} has a key that names no module, so the line is not a key"
        )

    def test_every_key_is_anchored(self, path: Path) -> None:
        """An unanchored key claims every longer name that starts the same."""
        loose = [key for key in keys_of(path) if not key.endswith("$")]
        assert not loose, (
            f"{path.name}: {loose} end with no `$`. stubgen matches a key with "
            f"`re.search`, so each one also claims a longer name -- `foo` takes "
            f"`foo_typed` and gives it the wrong signature. Add a `$`."
        )

    def test_every_key_names_something_real(self, path: Path) -> None:
        """A dead key leaves the real attribute with whatever stubgen guessed."""
        dead: list[str] = []
        for key in keys_of(path):
            attribute = key.removesuffix("$").rpartition(".")[2]
            if attribute in MODULE_HOOKS:
                continue
            # A key of a class member is `module.Class.member`, so walk the
            # parts until one of them imports.
            parts = key.removesuffix("$").split(".")
            module = None
            for split in range(len(parts) - 1, 0, -1):
                try:
                    module = importlib.import_module(".".join(parts[:split]))
                except ImportError:
                    continue
                rest = parts[split:]
                break
            else:
                dead.append(f"{key} (no module of it imports)")
                continue
            target = module
            for name in rest:
                target = getattr(target, name, None)
                if target is None:
                    dead.append(key)
                    break
        assert not dead, (
            f"{path.name}: {dead} names nothing in the built module. stubgen "
            f"prints a warning and continues, so the attribute this key was "
            f"written for keeps whatever stubgen guessed. Correct the key, or "
            f"delete it."
        )
