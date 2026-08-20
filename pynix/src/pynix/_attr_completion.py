"""Answer a Tab with the attributes of the file or flake the caller named.

**The baseline is Nix, and it is a real program that can be asked.** ``nix``
answers its own completion through ``NIX_GET_COMPLETIONS=<n>``: it prints the
kind of completion on the first line and then one candidate for each line
after it. So an equivalence between this and that is measurable, and
``pynix/completions/tests/test_nix_equivalence.py`` measures it::

    $ NIX_GET_COMPLETIONS=4 nix build --file ./tmp/autocomplete.nix nixos.
    attrs
    nixos._type
    nixos.class
    nixos.config

Three things in that answer are decisions this module copies.

**A candidate is the whole path, and not the last component.** Nix answers
``nixos.config`` and not ``config``. A shell replaces the word under the
cursor, and the word is the whole dotted path, so a last component alone would
delete what the caller already typed.

**No value is forced.** ``nixos.config.system`` comes back although
``config.system.built.toplevel`` is ``builtins.derivation {}``, which throws
when it is forced. Listing the names of an attribute set forces the set and
not what is in it, and this module asks for nothing else.

**The two spellings agree.** ``--file F --attr a.b`` and ``--file F#a.b`` are
the same selection -- :meth:`pynix.target.EvaluationTarget.selected_attr`
joins the fragment and the option -- so the completion of each has to be the
same set. That is why `complete_file` below completes the part after a ``#``
itself rather than leaving it to the shell.

**A completion runs while a person holds a key down, so it gives up.** Issue
#223 is the question this answers, and a budget is the answer. Evaluating a
Nix file has no upper bound: it can fetch a flake input, or walk nixpkgs. When
the budget runs out this returns nothing at all, which is what the shell shows
when no program answers. A wrong answer is worse than no answer here, because
the shell writes a candidate onto the command line.
"""

from __future__ import annotations

import contextlib
import io
import os
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Sequence

#: Seconds a completion may take before it answers with nothing.
#:
#: **Long enough for a local file, short enough to feel like a keypress.**
#: A small `default.nix` answers in well under a second. nixpkgs does not, and
#: a caller who asks for it gets nothing rather than a wedged terminal.
BUDGET_SECONDS = float(os.environ.get("PYNIX_COMPLETION_BUDGET", "2.0"))

#: The variable that overrides the budget, named here so a test can set it.
BUDGET_VARIABLE = "PYNIX_COMPLETION_BUDGET"


def _split(attr_prefix: str) -> tuple[tuple[str, ...], str]:
    """The complete components of *attr_prefix*, and the one being typed.

    ``"a.b.c"`` is ``("a", "b")`` and ``"c"``: the caller has finished ``a``
    and ``b``, and ``c`` is a prefix to match. ``"a."`` is ``("a",)`` and
    ``""``, which matches every attribute of ``a``.
    """
    parts = attr_prefix.split(".")
    return tuple(parts[:-1]), parts[-1]


async def _names(source: str, attr_prefix: str) -> list[str]:
    """The attribute paths of *source* that start with *attr_prefix*."""
    # Imported here and not at the top of the module. Issue #123 measured what
    # `pynix.target` costs a start that evaluates nothing -- 101 ms, because it
    # pulls structlog and the exception tree of nanopynix. A parser that only
    # lists an option must not pay that, and a completion is the one caller
    # that needs it.
    from nanopynix_helpers import select_attr_path  # noqa: PLC0415 -- see above

    from pynix._impl.settings import DEFAULT_STORE  # noqa: PLC0415 -- see above
    from pynix._util import eval_session  # noqa: PLC0415 -- see above
    from pynix.target import EvaluationTarget, base_attr_search, evaluate_target  # noqa: PLC0415 -- see above

    stem, tail = _split(attr_prefix)
    target = EvaluationTarget(file=source, attr=None, flake=None)
    # `"auto"`, which is what `--store` defaults to, and `verbosity="error"`
    # so the evaluator says nothing while a person is holding a key down.
    async with eval_session(DEFAULT_STORE, verbosity="error") as (_nix, _store, session):
        root = await evaluate_target(target, session, auto_call_file=True, attr_search=base_attr_search())
        value = await select_attr_path(root, stem) if stem else root
        names = await value.attr_names()

    prefix = ".".join(stem)
    head = f"{prefix}." if prefix else ""
    return [f"{head}{name}" for name in names if name.startswith(tail)]


def _answer(source: str, attr_prefix: str) -> list[str]:
    """Run :func:`_names` under the budget, and answer nothing when it fails.

    **Every failure is the same answer, and that is on purpose.** A file that
    does not parse, an attribute path that leads nowhere, a value that is not
    an attribute set, and a budget that ran out are all "no candidates" to a
    shell. A traceback here would land in the middle of a command line.
    """
    # Imported here for the reason `_names` gives.
    import anyio  # noqa: PLC0415 -- see `_names`

    async def bounded() -> list[str]:
        with anyio.fail_after(BUDGET_SECONDS):
            return await _names(source, attr_prefix)

    # **stderr goes nowhere while this runs.** The evaluator logs through
    # structlog, and a log line drawn into a command line is worse than a
    # missing candidate. The shell reads the answer off file descriptor 8, so
    # nothing here needs the two standard streams.
    try:
        with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
            return anyio.run(bounded)
    except Exception:
        # Every failure is one answer here; the docstring above says why. A
        # cancellation is not caught, so Ctrl-C still ends the process.
        return []


def complete_attr(*, prefix: str, parsed_args: Any = None, **_: Any) -> Sequence[str]:
    """Candidates for ``--attr``, read out of the ``--file`` beside it.

    argcomplete passes the namespace it has parsed so far, so the file is
    already there when the caller types ``--file F --attr <TAB>``. The other
    order, ``--attr <TAB> --file F``, answers nothing: the file is not on the
    line yet, and there is nothing to evaluate.
    """
    source = getattr(parsed_args, "file", None)
    if not source:
        return []
    # A `#` in `--file` carries the first components of the same path, which is
    # what `EvaluationTarget.selected_attr` joins at run time.
    path, _, fragment = source.partition("#")
    joined = f"{fragment}.{prefix}" if fragment else prefix
    candidates = _answer(path, joined)
    if not fragment:
        return candidates
    cut = len(fragment) + 1
    return [candidate[cut:] for candidate in candidates]


def complete_file(*, prefix: str, **_: Any) -> Sequence[str]:
    """Candidates for ``--file``, once the caller has typed a ``#``.

    Before the ``#`` this answers nothing, so the shell offers file names --
    which is the right answer, and the one Nix gives for the same position.
    After it, the fragment is an attribute path of that file.
    """
    path, separator, fragment = prefix.partition("#")
    if not separator:
        return []
    return [f"{path}#{candidate}" for candidate in _answer(path, fragment)]
