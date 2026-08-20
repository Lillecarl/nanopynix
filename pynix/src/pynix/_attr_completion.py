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

**The walk is in `nanopynix_helpers.attr_completion`, and this module is the
program around it.** That library holds the two rules Nix applies -- one for
``--file`` and one for the fragment of a flake -- because they need an
evaluator and nothing else, and a second Nix CLI that wants them already
depends on it. What is here is what belongs to a program: the store, the
session, the budget, the ``#`` spelling that is ours, and the argcomplete
glue.

**A completion runs while a person holds a key down, so it gives up.** Issue
#223 is the question this answers, and a budget is the answer. Evaluating a
Nix file has no upper bound: it can fetch a flake input, or walk nixpkgs. When
the budget runs out this returns nothing at all, which is what the shell shows
when no program answers. A wrong answer is worse than no answer here, because
the shell writes a candidate onto the command line.
"""

from __future__ import annotations

import contextlib
import functools
import io
import os
from typing import TYPE_CHECKING, Any, Literal

from nanopynix._typechecking import BEARTYPING

# `or BEARTYPING`, for the reason `nanopynix_helpers.attr_completion` gives:
# beartype resolves an annotation at call time, and a name that only a type
# checker imported is not there to resolve.
if TYPE_CHECKING or BEARTYPING:
    from collections.abc import AsyncGenerator, Awaitable, Callable, Sequence

    from nanopynix_helpers import AttrPathSearch

    from libpynix.command import Completer
    from nanopynix import AsyncEvalSession

#: Which attribute-path search a command applies to the fragment of ``--flake``.
#:
#: **A name, and not the search itself.** ``pynix.target`` builds each search,
#: and importing it costs 101 ms that a command which only lists an option must
#: not pay. So a command module names the one it wants and
#: :func:`_flake_search` resolves the name inside the completion.
type FlakeSearch = Literal["base", "dev-shell", "repl", "exact"]

#: Seconds a completion may take before it answers with nothing.
#:
#: **Long enough for a local file, short enough to feel like a keypress.**
#: A small `default.nix` answers in well under a second, and a whole command
#: costs 0.639 s measured. nixpkgs does not, and a caller who asks for it gets
#: nothing rather than a wedged terminal.
#:
#: **5.0 s, and it was 2.0 s.** The old figure was under the cost of the case
#: it existed for: issue #231 measured a flake whose input never answers, and
#: the call returned at 4.086 s. A budget that a real case overruns is a
#: budget that reports nothing about the case, so it moved above it.
#:
#: :data:`BUDGET_VARIABLE` overrides it. Read once, from the environment, and
#: not through the settings model of `pynix`: this is one number that every
#: keypress pays for, and a settings tree would cost the start it saves.
BUDGET_SECONDS = float(os.environ.get("PYNIX_COMPLETION_BUDGET", "5.0"))

#: The variable that overrides the budget, named here so a test can set it.
BUDGET_VARIABLE = "PYNIX_COMPLETION_BUDGET"

#: Set this to a file name to make a failed completion write its traceback there.
#:
#: **A completion that answers nothing looks the same whatever went wrong**, and
#: that is deliberate: a shell shows no candidates, and the caller keeps typing.
#: It also means a defect here is invisible. Measured: the gate ran this suite
#: with no `pynix` on its search path, every row answered an empty set, and the
#: failure read as a completer offering nothing rather than one that never ran.
#: So there is a way to look, and it writes to a file rather than to stderr,
#: because stderr during a completion lands in the command line.
DEBUG_VARIABLE = "PYNIX_COMPLETION_DEBUG"


@contextlib.asynccontextmanager
async def _completion_session() -> AsyncGenerator[AsyncEvalSession]:
    """An evaluator for one completion, in this process.

    **`inproc` and not `rpc`, and that is the whole latency of the feature.**
    Every command of `pynix` opens `pynix._util.eval_session`, which builds a
    `nanopynix.rpc.Session` and so starts a worker process. The child imports
    the stack and loads libnixexpr and libnixstore, and a gRPC handshake
    follows. Issue #226 measured what that costs a completion: 0.834 s of a
    1.536 s answer, against 0.007 s for the same three objects here.

    **The store is not what costs it, and the measurement says so.** Whichever
    store a process opens first pays about one second, and `auto`, `daemon`
    and `dummy://` each pay it. So `DEFAULT_STORE` stays: a completion answers
    what the command would answer, and a narrower store would not be faster.

    **A completion is the one caller that needs no worker.** The engines are
    the same API, and a separate process exists for a separate Nix
    configuration or an overlay namespace. A Tab evaluates one file, reads the
    names of one attribute set, and the process then exits.
    """
    # Imported here for the reason `_names` gives.
    from nanopynix import inproc  # noqa: PLC0415 -- see `_names`
    from pynix._impl.settings import DEFAULT_STORE  # noqa: PLC0415 -- see `_names`

    # `verbosity="error"` so the evaluator says nothing while a person is
    # holding a key down, and no log forwarding: `_guarded` sends both streams
    # to a buffer that nothing reads.
    async with (
        inproc.Session(verbosity="error") as nix,
        nix.store(DEFAULT_STORE) as store,
        nix.eval(store) as session,
    ):
        yield session


async def _names(source: str, attr_prefix: str) -> list[str]:
    """The attribute paths of the file *source* that *attr_prefix* could mean."""
    # Imported here and not at the top of the module. Issue #123 measured what
    # `pynix.target` costs a start that evaluates nothing -- 101 ms, because it
    # pulls structlog and the exception tree of nanopynix. A parser that only
    # lists an option must not pay that, and a completion is the one caller
    # that needs it.
    from nanopynix_helpers import complete_file_attr_path  # noqa: PLC0415 -- see above

    from pynix.target import EvaluationTarget, base_attr_search, evaluate_target  # noqa: PLC0415 -- see above

    target = EvaluationTarget(file=source, attr=None, flake=None)
    async with _completion_session() as session:
        root = await evaluate_target(target, session, auto_call_file=True, attr_search=base_attr_search())
        return await complete_file_attr_path(root, attr_prefix)


def _guarded(work: Callable[[], Awaitable[list[str]]]) -> list[str]:
    """Run *work* under the budget, and answer nothing when it fails.

    **Every failure is the same answer, and that is on purpose.** A file that
    does not parse, an attribute path that leads nowhere, a value that is not
    an attribute set, and a budget that ran out are all "no candidates" to a
    shell. A traceback here would land in the middle of a command line.
    """
    # Imported here for the reason `_names` gives.
    import anyio  # noqa: PLC0415 -- see `_names`

    async def bounded() -> list[str]:
        with anyio.fail_after(BUDGET_SECONDS):
            return await work()

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
        _record_the_failure()
        return []


def _answer(source: str, attr_prefix: str) -> list[str]:
    """The attribute paths of *source* under *attr_prefix*, or nothing."""
    return _guarded(functools.partial(_names, source, attr_prefix))


def _record_the_failure() -> None:
    """Write the current traceback to the file :data:`DEBUG_VARIABLE` names.

    Does nothing when the variable is unset, which is every ordinary run.
    A failure to write is itself ignored: this exists to explain a completion
    that answered nothing, and it must never become the reason one did.
    """
    destination = os.environ.get(DEBUG_VARIABLE)
    if not destination:
        return
    # Imported here so an ordinary completion never loads it.
    import traceback  # noqa: PLC0415 -- see above

    try:
        with open(destination, "a", encoding="utf-8") as record:  # noqa: PTH123 -- a plain file, and `anyio.Path` is async
            record.write(traceback.format_exc())
    except OSError:
        return


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


def _flake_search(name: FlakeSearch) -> AttrPathSearch:
    """The :class:`~nanopynix_helpers.AttrPathSearch` that *name* stands for.

    ``"exact"`` is the empty search, which reads the fragment as one path and
    applies no prefix. ``pynix osearch`` passes no search to
    :func:`~pynix.target.evaluate_target`, and that is what no search means.
    """
    # Imported here for the reason `_names` gives.
    from nanopynix_helpers import AttrPathSearch  # noqa: PLC0415 -- see `_names`

    from pynix.target import base_attr_search, dev_shell_attr_search, repl_attr_search  # noqa: PLC0415 -- see `_names`

    if name == "base":
        return base_attr_search()
    if name == "dev-shell":
        return dev_shell_attr_search()
    if name == "repl":
        return repl_attr_search()
    return AttrPathSearch()


async def _flake_names(reference: str, fragment: str, name: FlakeSearch) -> list[str]:
    """The fragments of the flake *reference* that *fragment* could mean."""
    # Imported here for the reason `_names` gives.
    from nanopynix_helpers import complete_flake_fragment, flake_outputs  # noqa: PLC0415 -- see `_names`

    search = _flake_search(name)
    async with _completion_session() as session:
        # **A completion never writes a lock file.** Nix locks with the flags
        # of the command it is completing, which may write one. A Tab is not a
        # command, and a keypress that changes the tree of the caller is a
        # surprise that no candidate is worth.
        locked = await session.lock_flake(reference, write_lock_file=False)
        return await complete_flake_fragment(await flake_outputs(await locked.eval()), search, fragment)


def complete_flake(prefix: str, name: FlakeSearch) -> list[str]:
    """Candidates for ``--flake``, once the caller has typed a ``#``.

    Before the ``#`` this answers nothing, so the shell offers file names.
    `nix` answers a flake reference there -- a registry entry, or a directory
    -- and issue #229 holds that half.
    """
    reference, separator, fragment = prefix.partition("#")
    if not separator:
        return []
    return [
        f"{reference}#{candidate}" for candidate in _guarded(functools.partial(_flake_names, reference, fragment, name))
    ]


def flake_completer(name: FlakeSearch) -> Completer:
    """A completer for ``--flake`` that applies the search *name* names.

    **Each command searches differently, so each command gets its own.**
    ``nix develop F#<TAB>`` offers the names under ``devShells.<system>`` and
    ``nix build F#<TAB>`` does not, because the two commands override
    `getDefaultFlakeAttrPathPrefixes` differently. Seven commands of this
    program declare the option, and a single completer carrying the base pair
    would be wrong for four of them: both forms of ``develop``, ``repl``, and
    ``osearch``, which applies no search at all.
    """

    def complete(*, prefix: str, **_: Any) -> Sequence[str]:
        return complete_flake(prefix, name)

    return complete
