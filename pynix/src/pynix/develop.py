"""``pynix develop`` and ``pynix print-dev-env``, the counterparts of ``nix``'s.

Both commands compute the build environment of a derivation, which
``src/nix/develop.cc`` does in six steps: read the derivation, refuse a builder
that is not ``bash``, rewrite the derivation so that its builder is
``get-env.sh``, write the rewritten derivation, build it, and read the JSON
that the builder wrote.

The first four steps are one binding,
:meth:`~nanopynix.protocols.AsyncStore.write_dev_shell_derivation`, because the
three supported Nix versions disagree about how a derivation is written. The
last two are here. :mod:`pynix._dev_env` turns the JSON back into bash.

``develop`` takes its command after ``--``, and not from a ``--command``
option. clypi stops parsing at ``--`` and gives the tail back through
``get_unparsed()``, so ``pynix develop -f . -- make -j4`` passes ``-j4``
through untouched. A pipeline belongs inside the command, as ``-- bash -c
'make | less'``, exactly as with ``nix develop --command``.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from contextlib import AsyncExitStack
from dataclasses import dataclass

# A real import, not a TYPE_CHECKING one: clypi resolves the annotations on the
# commands below at runtime to build their argument parsers, so `Path` has to
# exist as an object and not just as a lazy PEP 563 string.
from pathlib import Path
from typing import TYPE_CHECKING, Any, override

import anyio.to_thread
import structlog
from anyio import Path as AnyioPath
from clypi import Command, arg

# A private name of clypi, and clypi offers no public way to clear it -- see
# take_unparsed. Imported rather than spelled out as a string, so a rename in
# clypi fails here at import time instead of leaving take_unparsed silently
# clearing nothing.
from clypi._cli.main import CLYPI_UNPARSED
from rich.text import Text

import nanopynix
from nanopynix import store_exec_prefix
from nanopynix._typechecking import BEARTYPING
from nanopynix.exceptions import NixError
from pynix._dev_env import BuildEnvironment, DevEnvError, make_rc_script, quote
from pynix._settings import (
    ConfiguredCommand,
    eval_store_option,
    print_build_logs_option,
    store_option,
    verbosity_option,
)
from pynix._util import error_exit, nix_session, print_json, report_and_exit
from pynix.target import (
    EvaluationTarget,
    EvaluationTargetError,
    attr_option,
    derivation_path,
    evaluate_target_locked,
    file_option,
    flake_option,
    select_attr,
)

if TYPE_CHECKING or BEARTYPING:
    from collections.abc import Callable

    from nanopynix.protocols import AsyncLockedFlake, AsyncStore

logger = structlog.get_logger(__name__)

#: What Nix puts in front of an error message of its own.
_NIX_ERROR_PREFIX = "error: "

#: The vendored copy of Nix's own environment dumper. Its header gives the
#: provenance and the licence.
_GET_ENV_SH = Path(__file__).parent / "get-env.sh"

#: Where an output path is rewritten to. ``develop.cc:351`` uses the same
#: directory, and it is relative to the working directory rather than to a
#: temporary one, so that a phase which writes to ``$out`` writes somewhere the
#: caller can find.
_OUTPUTS_DIR_NAME = "outputs"

#: Wrapped around the environment for an interactive shell, mirroring
#: ``develop.cc:625``. ``~/.bashrc`` runs first so that a prompt survives, and
#: alias expansion is off while the environment is applied because an alias
#: would otherwise rewrite what the environment defines.
_INTERACTIVE_PROLOGUE = '[ -n "$PS1" ] && [ -e ~/.bashrc ] && source ~/.bashrc;\nshopt -u expand_aliases\n'
_INTERACTIVE_EPILOGUE = "\nshopt -s expand_aliases\n"

#: Where the interactive bash comes from. ``defaultNixpkgsFlakeRef()``, an
#: indirect reference that the registry resolves.
_NIXPKGS_FLAKE_REF = "nixpkgs"
_INTERACTIVE_BASH_ATTR = "bashInteractive"


@dataclass(frozen=True)
class InteractiveShell:
    """The bash that an interactive dev shell runs.

    *from_nixpkgs* separates the two cases that ``develop.cc`` also separates:
    a shell it chose, whose directory it prepends to ``PATH``, and the bare
    ``bash`` it falls back to, which is on ``PATH`` already.
    """

    path: str
    from_nixpkgs: bool
    exec_prefix: list[str]


class PrintDevEnv(ConfiguredCommand):
    """Print the build environment of a derivation

    Examples:
      pynix print-dev-env --file default.nix --attr hello
      pynix print-dev-env --flake .#hello --json"""

    file: Path | None = file_option()
    attr: str | None = attr_option()
    flake: str | None = flake_option()
    store: str = store_option("Store URI to build with.")
    eval_store: str | None = eval_store_option()
    verbosity: str | None = verbosity_option()
    print_build_logs: bool = print_build_logs_option()
    json: bool = arg(False, help="Print the environment as JSON, instead of the bash that restores it.")

    @override
    async def run(self) -> None:
        environment, _ = await build_dev_env(self)
        if self.json:
            print_json(environment.to_json())
            return
        script = make_rc_script(environment, outputs_dir=Path.cwd() / _OUTPUTS_DIR_NAME)
        # The trailing newline is not part of the script. Nix's
        # `logger->writeToStdout` adds one, and this output is compared against
        # `nix print-dev-env` byte for byte.
        sys.stdout.write(script + "\n")


class Develop(ConfiguredCommand):
    """Run a command, or an interactive bash, in a derivation's build environment

    Everything after -- is the command. Without a command, this starts an
    interactive bash.

    Examples:
      pynix develop --file default.nix --attr hello
      pynix develop --flake .# -- make -j4
      pynix develop --flake .# -- bash -c 'make | less'"""

    file: Path | None = file_option()
    attr: str | None = attr_option()
    flake: str | None = flake_option()
    store: str = store_option("Store URI to build with.")
    eval_store: str | None = eval_store_option()
    verbosity: str | None = verbosity_option()
    print_build_logs: bool = print_build_logs_option()

    @override
    async def run(self) -> None:
        command = take_unparsed(type(self))
        # Only an interactive shell needs one. develop.cc:688 appends its
        # `SHELL=` line after the `exec` of a command, where nothing runs it, so
        # resolving a shell for a command would cost a nixpkgs evaluation and
        # change nothing.
        environment, shell = await build_dev_env(self, resolve_shell=not command)

        # mkdtemp rather than TemporaryDirectory: execvp replaces this process,
        # so nothing of ours ever cleans up. The script removes the directory
        # itself, which is what develop.cc:599 does for the same reason.
        directory = await anyio.to_thread.run_sync(tempfile.mkdtemp, "", "pynix-develop-")
        script = compose_shell_script(
            environment,
            command=command,
            outputs_dir=Path.cwd() / _OUTPUTS_DIR_NAME,
            cleanup=Path(directory),
            shell=shell,
        )
        rc_path = Path(directory) / "rc"
        await AnyioPath(rc_path).write_text(script)

        logger.info("pynix develop entering the environment", command=command or None)
        _exec_bash(rc_path, interactive=not command, shell=shell)


def take_unparsed(command_type: type[Command]) -> list[str]:
    """Return the words after ``--``, and clear them.

    clypi keeps them on the *class* and never clears them
    (``clypi/_cli/main.py:608``), so a second parse in the same process with no
    ``--`` of its own still reports the first one's tail. A command-line run
    parses once and would never see this. A test, or any program that embeds
    the parser, parses more than once and sees it every time.
    """
    words = [str(word) for word in command_type.get_unparsed()]
    setattr(command_type, CLYPI_UNPARSED, [])
    return words


def compose_shell_script(
    environment: BuildEnvironment,
    *,
    command: list[str],
    outputs_dir: Path,
    cleanup: Path | None = None,
    shell: InteractiveShell | None = None,
) -> str:
    """Build the bash that ``develop`` hands to the shell.

    A command ends the script with ``exec``, which is what gives the command
    the exit status of the shell, and therefore of ``pynix``. Without one the
    script is wrapped for interactive use.

    *cleanup* is removed by the script itself, before the command runs.
    ``exec`` never returns, so a line after it would never run -- which is why
    develop.cc:599 also puts this line before the branch.

    *shell* adds the two lines of ``develop.cc:688``, and only for an
    interactive shell. Nix appends them after its own ``exec`` line, where they
    never run, so a command sees no ``SHELL`` of Nix's either.
    """
    script = make_rc_script(environment, outputs_dir=outputs_dir)
    if cleanup is not None:
        script += f"command rm -rf {quote(str(cleanup))}\n"
    if command:
        # quote(), not shlex.join(): develop.cc:620 quotes every word, and a
        # word that shlex leaves bare would be re-split by the shell.
        return script + "exec {}\n".format(" ".join(quote(word) for word in command))
    script = _INTERACTIVE_PROLOGUE + script + _INTERACTIVE_EPILOGUE
    if shell is not None:
        # The build's own bash is not interactive: it is built without
        # readline. Leaving it in SHELL hands it to everything that spawns the
        # user's shell -- vim's :sh, a git editor -- from inside the dev shell.
        script += f'SHELL="{shell.path}"\n'
        if shell.from_nixpkgs:
            # Only when the lookup succeeded, as at develop.cc:689. The
            # fallback shell is already on PATH by definition.
            script += f'PATH="{Path(shell.path).parent}${{PATH:+:$PATH}}"\n'
    return script


def _exec_bash(rc_path: Path, *, interactive: bool, shell: InteractiveShell | None = None) -> None:
    """Replace this process with the bash that reads *rc_path*.

    ``exec``, so the shell owns the terminal and its exit status is the exit
    status of ``pynix``.

    A command runs the file as a script, and an interactive shell reads it as
    an rc file. ``develop.cc:698`` gives the reason for the difference: with
    ``--rcfile``, Ctrl-C would leave an interactive shell behind after the
    command it interrupted.

    ``argv[0]`` stays ``bash`` whatever binary runs, which is what
    ``shell.filename()`` does at develop.cc:698.
    """
    tail = ["--rcfile", str(rc_path)] if interactive else [str(rc_path)]
    if shell is None or not shell.from_nixpkgs:
        os.execvp("bash", ["bash", *tail])  # noqa: S606, S607 -- the bare name is the point: it is what `nix develop` falls back to at develop.cc:642
        return
    argv = [*shell.exec_prefix, shell.path, *tail]
    if shell.exec_prefix:
        # The helper takes the program as its own argument, so there is no
        # argv[0] of ours to set. See nanopynix.store_exec.
        os.execvp(argv[0], argv)  # noqa: S606 -- the helper that nanopynix ships, resolved off PATH like the rest of its tools
        return
    os.execv(shell.path, ["bash", *tail])  # noqa: S606 -- an absolute store path that this process just built, and argv[0] is bash as at develop.cc:698


async def build_dev_env(
    command: Any, *, resolve_shell: bool = False
) -> tuple[BuildEnvironment, InteractiveShell | None]:
    """Compute the build environment of *command*'s evaluation target.

    *resolve_shell* also picks the interactive bash, in the same session, which
    is where ``CmdDevelop::run`` picks it.
    """
    target = EvaluationTarget.from_command(command)
    try:
        target.validate(required=True)
    except EvaluationTargetError as exc:
        report_and_exit(exc)

    get_env_script = await AnyioPath(_GET_ENV_SH).read_text()

    async with AsyncExitStack() as stack:
        nix = await stack.enter_async_context(
            nix_session(verbosity=command.verbosity, print_build_logs=command.print_build_logs),
        )
        build_store = await stack.enter_async_context(nix.store(command.store))
        # One store, and no `eval_store` argument, unless the caller named a
        # second one. Opening the same URI twice would give two Store objects
        # that Nix has no reason to believe are the same store.
        eval_store = build_store
        if command.eval_store is not None:
            eval_store = await stack.enter_async_context(nix.store(command.eval_store))

        shell: InteractiveShell | None = None
        async with nix.eval(eval_store) as session:
            # The lock is kept past the evaluation because the interactive
            # shell needs it: it decides which nixpkgs `bashInteractive` comes
            # from. One `finally` covers every way out, including the
            # `report_and_exit` above, which raises.
            locked: AsyncLockedFlake | None = None
            try:
                try:
                    root, locked = await evaluate_target_locked(target, session, auto_call_file=True)
                    drv_path = await derivation_path(root)
                except EvaluationTargetError as exc:
                    report_and_exit(exc)
                if resolve_shell:
                    shell = await _resolve_interactive_shell(session, eval_store, build_store, locked)
            finally:
                if locked is not None:
                    await locked.release()

        raw = await _read_dev_env(
            eval_store,
            build_store,
            drv_path=drv_path,
            get_env_script=get_env_script,
        )

    try:
        return BuildEnvironment.from_json(raw), shell
    except DevEnvError as exc:
        error_exit(str(exc), cause=exc)


async def _read_dev_env(
    eval_store: AsyncStore,
    build_store: AsyncStore,
    *,
    drv_path: str,
    get_env_script: str,
) -> str:
    """Rewrite the derivation, build it, and read the JSON that it wrote."""
    try:
        shell_drv = await eval_store.write_dev_shell_derivation(drv_path, get_env_script)
    except NixError as exc:
        # The one Nix error this command expects: a derivation whose builder is
        # not bash cannot dump its environment. A traceback would say the same
        # thing at ten times the length, so report the reason and stop.
        # `Text.from_ansi` first, and `removeprefix` after: the "error: " of
        # Nix carries its own colour, so the prefix is not at the start of
        # `exc.msg` and it is at the start of the plain text of the `Text`.
        # "Error:" comes from `error_exit`, so this drops the second one.
        text = Text.from_ansi(exc.msg)
        error_exit(text[len(_NIX_ERROR_PREFIX) :] if text.plain.startswith(_NIX_ERROR_PREFIX) else text, cause=exc)
    logger.info("pynix develop building the environment", drv_path=shell_drv)
    separate_eval_store = eval_store if eval_store is not build_store else None
    results = await build_store.build_paths_with_results([shell_drv], eval_store=separate_eval_store)
    for result in results:
        if not result.success:
            error_exit(f"failed to build the development environment: {result.error_msg or result.status}")

    # `get-env.sh` writes to whichever output it is given, so take the first
    # output that has anything in it. develop.cc:303 does the same.
    to_disk = await _physical_path_mapper(build_store)
    for path in await eval_store.query_derivation_outputs(shell_drv):
        candidate = AnyioPath(to_disk(path))
        if await candidate.is_file() and (await candidate.stat()).st_size:
            return await candidate.read_text()
    # `return`, though error_exit never returns: NoReturn is assignable to
    # `str`, and it is what tells ruff that this branch ends the function.
    return error_exit(f"the build environment of {drv_path} is empty")


async def _nixpkgs_flake_ref(locked: AsyncLockedFlake | None) -> str:
    """Return the flake reference that ``bashInteractive`` comes from.

    ``InstallableFlake::nixpkgsFlakeRef`` (``installable-flake.cc:194``): the
    ``nixpkgs`` that the target flake locks, when the target is a flake that
    locks one and that input is itself a flake. That is the reference the
    ``flake.lock`` of the target pins, so the dev shell of a flake gets the
    bash of the same nixpkgs the flake was built against, whatever the registry
    of the machine says.

    Otherwise the indirect ``nixpkgs``, which is exactly
    ``defaultNixpkgsFlakeRef()`` (``installable-flake.hh:85``). A ``--file``
    target has no lock, and a flake need not declare ``nixpkgs`` at all.
    """
    if locked is None:
        return _NIXPKGS_FLAKE_REF
    node = await locked.find_input([_NIXPKGS_FLAKE_REF])
    if node is None or not node.is_flake:
        return _NIXPKGS_FLAKE_REF
    return node.locked_ref


async def _resolve_interactive_shell(
    session: Any,
    eval_store: AsyncStore,
    build_store: AsyncStore,
    locked: AsyncLockedFlake | None,
) -> InteractiveShell:
    """Pick the bash an interactive dev shell runs, as ``develop.cc:645`` does.

    ``bashInteractive``, built, because the bash of the build environment is
    stdenv's and carries no readline. :func:`_nixpkgs_flake_ref` decides which
    nixpkgs it comes from. Nix wraps the whole lookup in a ``try`` and falls
    back to the bare word ``bash`` on failure (``develop.cc:679``), so an
    evaluation error, a missing registry entry or an offline machine costs the
    readline, not the shell.
    """
    try:
        found = await _nixpkgs_bash(session, eval_store, build_store, locked)
    except (NixError, EvaluationTargetError, RuntimeError) as exc:
        # Never fatal, as at develop.cc:679: the shell still starts, and only
        # its line editing is the poorer for it. RuntimeError covers the
        # relocated store with no store-exec helper -- see nanopynix.store_exec,
        # which raises rather than hand back a prefix that cannot work.
        logger.info("pynix develop is falling back to the bash on PATH", reason=str(exc))
        return _fallback_shell()
    if found is None:
        logger.info("pynix develop found no bin/bash in nixpkgs, so it is falling back to PATH")
        return _fallback_shell()
    return found


async def _nixpkgs_bash(
    session: Any,
    eval_store: AsyncStore,
    build_store: AsyncStore,
    locked: AsyncLockedFlake | None,
) -> InteractiveShell | None:
    """Build ``bashInteractive`` from nixpkgs, and find its ``bin/bash``.

    ``None`` when the build produced no ``bin/bash``, which
    ``develop.cc:676`` treats the same as a failed lookup.
    """
    outputs = await session.eval_flake(await _nixpkgs_flake_ref(locked), write_lock_file=False)
    attrpath = f"legacyPackages.{nanopynix.current_system()}.{_INTERACTIVE_BASH_ATTR}"
    value = await select_attr(outputs, attrpath)
    drv_path = await derivation_path(value)
    logger.info("pynix develop is building the interactive shell", attrpath=attrpath)
    results = await build_store.build_paths_with_results(
        [drv_path],
        eval_store=eval_store if eval_store is not build_store else None,
    )
    if any(not result.success for result in results):
        return None

    to_disk = await _physical_path_mapper(build_store)
    for output in await eval_store.query_derivation_outputs(drv_path):
        candidate = f"{output}/bin/bash"
        if await AnyioPath(to_disk(candidate)).is_file():
            return InteractiveShell(
                path=candidate,
                from_nixpkgs=True,
                # Empty for an ordinary store. `nix develop` reaches the same
                # problem through execProgramInStore.
                exec_prefix=await store_exec_prefix(build_store),
            )
    return None


def _fallback_shell() -> InteractiveShell:
    """The bash on PATH, which is what Nix falls back to at ``develop.cc:642``."""
    return InteractiveShell(path=shutil.which("bash") or "bash", from_nixpkgs=False, exec_prefix=[])


async def _physical_path_mapper(store: AsyncStore) -> Callable[[str], str]:
    """Return a function that maps a store path to where its bytes really are.

    A store opened with a root reports logical ``/nix/store/...`` paths while
    the bytes live under that root, so reading the reported path finds nothing.
    :mod:`nanopynix.store_exec` gives the same reason at length, for the harder
    case of *running* such a path; reading one needs only the two directories.

    The identity function for an ordinary store, which is the common case.
    """
    dirs = await store.store_dirs()
    store_dir = dirs.store_dir.rstrip("/")
    real_store_dir = (dirs.real_store_dir or store_dir).rstrip("/")
    if real_store_dir == store_dir:
        return lambda path: path
    return lambda path: real_store_dir + path[len(store_dir) :] if path.startswith(store_dir) else path
