"""Generate docs/pynix/reference.md from pynix's live ``clypi`` command tree.

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
from typing import TYPE_CHECKING, Any

_DOCS_DIR = Path(__file__).resolve().parent
_OUTPUT = _DOCS_DIR / "pynix" / "reference.md"
_PYNIX_SRC = _DOCS_DIR.parent / "pynix" / "src"
_PYTHON_SRC = _DOCS_DIR.parent / "python" / "src"

for _path in (_PYNIX_SRC, _PYTHON_SRC):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from pynix import Pynix  # noqa: E402 -- sys.path must be extended before pynix is importable

if TYPE_CHECKING:
    from clypi import Command


def _render_type(tp: Any) -> str:
    """Render a clypi ``arg_type`` annotation as short, readable text.

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


def _render_default_suffix(conf: Any) -> str:
    """Render a trailing " (default: ...)" / " *(required)*" note for the Help column.

    Some defaults (e.g. the trusted-public-keys signing key) are very long —
    giving them their own table column stretches that column and drags every
    other row wide with it. Folding the note into Help lets it wrap normally.
    """
    if not conf.has_default():
        return " *(required)*"
    default = conf.get_default()
    rendered = "None" if default is None else repr(default)
    return f" (default: `{rendered}`)"


def _render_args_table(cmd: type[Command]) -> list[str]:
    names = [name for name in cmd.field_names() if name != "subcommand"]
    if not names:
        return []
    lines = ["| Argument | Type | Help |", "| --- | --- | --- |"]
    for name in names:
        conf = cmd.get_field_conf(name)
        help_text = (conf.help or "").replace("|", "\\|")
        type_text = _render_type(conf.arg_type)
        lines.append(f"| `{conf.display_name}` | `{type_text}` | {help_text}{_render_default_suffix(conf)} |")
    lines.append("")
    return lines


def _render_command(cmd: type[Command], path: list[str]) -> list[str]:
    full_path = [*path, cmd.prog()]
    heading = "#" * min(len(full_path) + 1, 6)
    lines = [f"{heading} `{' '.join(full_path)}`", ""]
    doc = (cmd.__doc__ or "").strip()
    if doc:
        lines += [doc, ""]
    lines += _render_args_table(cmd)
    for name, sub in cmd.subcommands().items():
        del name  # the dict key is redundant with sub.prog(); keep our own path instead
        if sub is not None:
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
            "Generated from pynix's live `clypi` command tree — see"
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
