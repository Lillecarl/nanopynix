"""Generate docs/pynix/reference.md from pynix's live command tree.

Regenerated on every Sphinx build (see ``docs/conf.py``'s ``setup()``), so the
*rendered* site can never drift from the actual commands, arguments, and help
text. The checked-in ``reference.md`` is a different matter -- it only refreshes
when someone runs the generator and commits the result, and it had silently
drifted 252 lines behind before ``tests/meta/test_docs_reference.py`` started
gating it. Run standalone with::

    python docs/_generate_pynix_reference.py
"""

from __future__ import annotations

import sys
import types
import typing
from pathlib import Path
from typing import Any

_DOCS_DIR = Path(__file__).resolve().parent
_OUTPUT = _DOCS_DIR / "pynix" / "reference.md"
_PYNIX_SRC = _DOCS_DIR.parent / "pynix" / "src"
# The language server, which issue #107 made an optional subcommand: `pynix`
# mounts `lsp` when `pynix-lsp` imports and leaves it out when it does not.
# Naming the source directory here makes this generator read the whole CLI
# whichever environment runs it, so the reference does not gain and lose a
# command with the venv that builds it.
_PYNIX_LSP_SRC = _DOCS_DIR.parent / "pynix-lsp" / "src"
_PYTHON_SRC = _DOCS_DIR.parent / "python" / "src"

for _path in (_PYNIX_SRC, _PYNIX_LSP_SRC, _PYTHON_SRC):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from pynix import Pynix  # noqa: E402 -- sys.path must be extended before pynix is importable
from pynix._cli import (  # noqa: E402 -- see above; the name of a command on the command line, which this file must spell the same way the parser does
    MISSING,
    Command,
    command_name,
)
from pynix._impl.settings import PynixDefaults  # noqa: E402 -- see above


def _render_type(tp: Any) -> str:
    """Render a declared annotation as short, readable text.

    Renders unions as "A or B" rather than "A | B" — a literal ``|`` inside a
    Markdown table cell splits the cell even inside backtick code spans.
    """
    origin = typing.get_origin(tp)
    if origin is typing.Annotated:
        return _render_type(typing.get_args(tp)[0])
    if tp is type(None):
        return "None"
    if isinstance(tp, type):
        return tp.__name__
    args = typing.get_args(tp)
    if origin is types.UnionType or origin is typing.Union:
        return " or ".join(_render_type(arg) for arg in args)
    if origin is not None and args:
        base = getattr(origin, "__name__", str(origin))
        return f"{base}[{', '.join(_render_type(arg) for arg in args)}]"
    return str(tp).replace("typing.", "").replace("pathlib.", "")


def _render_default_suffix(name: str, spec: Any) -> str:
    """Render a trailing " (default: ...)" / " *(required)*" note for the Help column.

    Some defaults (e.g. the trusted-public-keys signing key) are very long —
    giving them their own table column stretches that column and drags every
    other row wide with it. Folding the note into Help lets it wrap normally.
    """
    if spec.positional and spec.default is MISSING:
        return " *(required)*"
    if spec.configured:
        # A configuration-backed option, whose value comes from the environment
        # or the configuration file when the caller names no flag. The reference
        # is checked in and gated, so it states the built-in default, which is
        # what a reader with no configuration gets. Reading the model rather
        # than a built command is what keeps this file the same on every
        # machine.
        default = PynixDefaults.model_fields[name].get_default(call_default_factory=False)
    else:
        default = None if spec.default is MISSING else spec.default
    rendered = "None" if default is None else repr(default)
    return f" (default: `{rendered}`)"


def _display_name(name: str, spec: Any) -> str:
    """What the caller types: a flag for an option, the bare name otherwise."""
    return name if spec.positional else "--" + name.replace("_", "-")


def _render_args_table(cmd: type[Command]) -> list[str]:
    if not cmd.specs:
        return []
    lines = ["| Argument | Type | Help |", "| --- | --- | --- |"]
    for name, spec in cmd.specs.items():
        help_text = spec.help.replace("|", "\\|")
        type_text = _render_type(cmd.types[name])
        lines.append(
            f"| `{_display_name(name, spec)}` | `{type_text}` | {help_text}{_render_default_suffix(name, spec)} |"
        )
    lines.append("")
    return lines


def _render_command(cmd: type[Command], path: list[str]) -> list[str]:
    full_path = [*path, command_name(cmd)]
    heading = "#" * min(len(full_path) + 1, 6)
    lines = [f"{heading} `{' '.join(full_path)}`", ""]
    doc = (cmd.__doc__ or "").strip()
    if doc:
        lines += [doc, ""]
    lines += _render_args_table(cmd)
    for sub in cmd.subcommands:
        lines += _render_command(sub, full_path)
    return lines


def render() -> str:
    """Render the whole reference from the live pynix command tree.

    Split out from :func:`generate` so a test can compare it against the
    checked-in file without writing anything -- see
    ``tests/meta/test_docs_reference.py``.
    """
    lines = [
        "# CLI reference",
        "",
        (
            "Generated from pynix's live command tree — see"
            " `docs/_generate_pynix_reference.py`. Every command also accepts"
            " `--help` for the same information at the terminal."
        ),
        "",
    ]
    lines += _render_command(Pynix, [])
    return "\n".join(lines) + "\n"


def generate() -> None:
    """Regenerate ``docs/pynix/reference.md`` from the live pynix command tree."""
    _OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    _OUTPUT.write_text(render())


if __name__ == "__main__":
    generate()
