"""``pynix develop`` and ``pynix print-dev-env``.

The load-bearing test here is :func:`test_print_dev_env_json_matches_nix`. It
runs ``nix print-dev-env --json`` over the same derivation and compares the two
documents, so what proves the derivation rewrite is Nix itself and not an
expectation written by hand.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from typing import TYPE_CHECKING, Any, Never, cast

import pytest
from anyio import Path as AnyioPath, run_process

from nanopynix.models import LockedNode
from nanopynix.protocols import AsyncLockedFlake
from nanopynix_testing.nix_environment import with_nixpkgs
from pynix import parse
from pynix._dev_env import BuildEnvironment, make_rc_script, quote
from pynix._impl.develop import (  # pyright: ignore[reportPrivateUsage] -- the ref-selection decision is unit-tested directly; the end-to-end path builds bashInteractive
    InteractiveShell,
    _nixpkgs_flake_ref,
    compose_shell_script,
)
from pynix.develop import Develop
from support.nix_oracle import require_matching_nix_cli

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from nanopynix_testing.nix_environment import NixTestEnvironment

#: A store path, with the hash reduced to a placeholder. The ``-env``
#: derivation is content-addressed over ``get-env.sh``, so any difference in
#: that script changes every path. ``nanopynix-bindings`` ships the same
#: ``get-env.sh`` that Nix embeds, so the two derivations agree.
_STORE_PATH = re.compile(r"/nix/store/[a-z0-9]{32}-")

#: Variables that still differ between pynix and the oracle in an otherwise
#: identical environment. Kept for the case a future Nix changes the script
#: between versions:
#:
#: - ``LINENO`` is the line of ``get-env.sh`` that dumps the environment.
#: - ``NIX_CFLAGS_COMPILE`` carries ``-frandom-seed=``, which stdenv derives
#:   from the output path.
_EXPECTED_DIFFERENCES: frozenset[str] = frozenset({"LINENO", "NIX_CFLAGS_COMPILE"})

#: The variables that name the temporary directory of one build.
#:
#: **A Linux build makes them agree, and it is the chroot that does it.** The
#: builder there runs in a mount namespace where the build directory is
#: ``/build``, whatever directory Nix made outside it. pynix and nix therefore
#: read the same string from two different builds.
#:
#: Nothing maps the directory on another operating system. Nix makes
#: ``/nix/var/nix/builds/nix-<pid>-<random>`` for each build, so two builds
#: never agree, and the value says nothing about the code under test.
#:
#: ``NIX_ATTRS_JSON_FILE`` and ``NIX_ATTRS_SH_FILE`` are files inside that
#: directory, so a structured-attributes derivation adds them to the set.
_BUILD_DIRECTORY_VARIABLES = frozenset(
    {
        "NIX_ATTRS_JSON_FILE",
        "NIX_ATTRS_SH_FILE",
        "NIX_BUILD_TOP",
        "TEMP",
        "TEMPDIR",
        "TMP",
        "TMPDIR",
    }
)


def _acceptable_differences() -> frozenset[str]:
    """Which variables may differ between pynix and the oracle, on this host."""
    if sys.platform == "linux":
        return _EXPECTED_DIFFERENCES
    return _EXPECTED_DIFFERENCES | _BUILD_DIRECTORY_VARIABLES


_PLAIN_DERIVATION = """
let
  pkgs = import <nixpkgs> {};
in
pkgs.stdenvNoCC.mkDerivation {
  pname = "pynix-develop-plain";
  version = "1";
  dontUnpack = true;
  PYNIX_MARKER = "a marker with 'quotes' and $dollars";
  PYNIX_LIST = [ "one" "two" ];
  shellHook = ''
    export PYNIX_SHELL_HOOK_RAN=1
  '';
  installPhase = ''
    echo hi > "$out"
  '';
}
"""

_STRUCTURED_DERIVATION = """
let
  pkgs = import <nixpkgs> {};
in
pkgs.stdenvNoCC.mkDerivation {
  pname = "pynix-develop-structured";
  version = "1";
  __structuredAttrs = true;
  dontUnpack = true;
  PYNIX_MARKER = "structured";
  PYNIX_LIST = [ "one" "two" ];
  installPhase = ''
    echo hi > "$out"
  '';
}
"""

_NON_BASH_DERIVATION = """
let
  pkgs = import <nixpkgs> {};
in
derivation {
  name = "pynix-develop-non-bash";
  system = builtins.currentSystem;
  builder = "${pkgs.coreutils}/bin/true";
}
"""


def _write(tmp_path: Path, name: str, source: str, nixpkgs_path: str) -> Path:
    nix_file = tmp_path / name
    nix_file.write_text(with_nixpkgs(source, nixpkgs_path))
    return nix_file


def _normalise(value: object) -> object:
    """Replace every store hash, so only what must agree is compared."""
    if isinstance(value, str):
        return _STORE_PATH.sub("/nix/store/<hash>-", value)
    if isinstance(value, dict):
        entries = cast("dict[str, object]", value)
        return {key: _normalise(item) for key, item in entries.items()}
    if isinstance(value, list):
        items = cast("list[object]", value)
        return [_normalise(item) for item in items]
    return value


async def _nix_print_dev_env(nix_file: Path, store_uri: str) -> dict[str, Any]:
    """The oracle: what ``nix print-dev-env --json`` says about the same file."""
    await require_matching_nix_cli()
    result = await run_process(
        [
            "nix",
            "--extra-experimental-features",
            "nix-command",
            "print-dev-env",
            "--store",
            store_uri,
            "--file",
            str(nix_file),
            "--json",
        ],
        check=False,
    )
    if result.returncode != 0:
        pytest.fail(f"the oracle failed: nix print-dev-env exited {result.returncode}\n{result.stderr.decode()}")
    return json.loads(result.stdout)


def _compare(mine: dict[str, Any], theirs: dict[str, Any]) -> None:
    assert sorted(mine) == sorted(theirs)

    for section in ("bashFunctions", "structuredAttrs"):
        if section not in mine:
            continue
        assert _normalise(mine[section]) == _normalise(theirs[section]), section

    my_variables: dict[str, Any] = mine["variables"]
    their_variables: dict[str, Any] = theirs["variables"]
    assert sorted(my_variables) == sorted(their_variables)
    differing = {name for name in my_variables if _normalise(my_variables[name]) != _normalise(their_variables[name])}
    acceptable = _acceptable_differences()
    assert differing <= acceptable, {
        name: (my_variables[name], their_variables[name]) for name in sorted(differing - acceptable)
    }


# --- the assumption everything rests on -----------------------------------


def test_a_command_after_the_double_dash_reaches_the_command_untouched() -> None:
    """argparse stops parsing options at ``--``, so nothing after it is one.

    This is what lets ``develop`` take its command the way the shell would,
    rather than through a ``--command`` option as ``nix develop`` does. Every
    other test here assumes it.

    Measured across the move of issue #214: clypi and argparse hand back the
    same list, ``-- literal`` as one word included.
    """
    cmd = parse(["develop", "--file", "x.nix", "--", "make", "-j4", "--jobs=2", "-- literal"])
    assert isinstance(cmd, Develop)
    assert cmd.command == ["make", "-j4", "--jobs=2", "-- literal"]


def test_a_second_parse_does_not_inherit_the_first_command() -> None:
    """A second parse in one process sees only its own tail.

    clypi kept the tail on the *class* and never cleared it, so
    ``pynix._impl.develop.take_unparsed`` had to reach into
    ``clypi._cli.main.CLYPI_UNPARSED`` and reset it. A command line parses once
    and would never have shown that; a test, or any program that embeds the
    parser, saw it every time. argparse keeps nothing on the class, and issue
    #214 deleted the workaround. This test stays, because the property is what
    matters and not the mechanism.
    """
    first = parse(["develop", "--file", "x.nix", "--", "make"])
    assert isinstance(first, Develop)
    assert first.command == ["make"]

    second = parse(["develop", "--file", "x.nix"])
    assert isinstance(second, Develop)
    assert second.command == []


# --- which nixpkgs the interactive shell comes from ------------------------


class _FakeLockedFlake(AsyncLockedFlake):
    """A lock that answers ``find_input`` and nothing else.

    The whole protocol is implemented, and not just the one method used, on
    purpose: ``AsyncLockedFlake`` is ``@runtime_checkable`` and
    ``_nixpkgs_flake_ref`` is annotated with it, so beartype does an
    ``isinstance`` check on the way in. A partial double would fail that check
    rather than the assertion the test is making.
    """

    description = ""

    def __init__(self, node: LockedNode | None) -> None:
        self._node = node

    async def find_input(self, path: Sequence[str], /) -> LockedNode | None:
        assert list(path) == ["nixpkgs"], "the question is about nixpkgs, and nothing else"
        return self._node

    async def eval(self) -> Never:
        raise AssertionError("_nixpkgs_flake_ref must not evaluate the flake")

    async def metadata_json(self) -> Never:
        raise AssertionError("_nixpkgs_flake_ref must not need the whole metadata object")

    async def write_lock_file(self) -> Never:
        raise AssertionError("_nixpkgs_flake_ref must not write a lock file")

    async def release(self) -> None:
        """The caller in ``develop.py`` owns the lock, and releases it there."""


async def test_a_flake_target_uses_the_nixpkgs_that_the_flake_locks() -> None:
    """``InstallableFlake::nixpkgsFlakeRef``: the flake's own input wins.

    This is the deviation issue #79 exists to close. Before the lock graph was
    exposed, ``pynix develop`` took the registry's ``nixpkgs`` in every case, so
    on a machine whose registry points elsewhere it gave an interactive bash
    from a different nixpkgs than ``nix develop`` gives.

    A unit test rather than a full run: the end-to-end proof builds
    ``bashInteractive``, and this pins the decision itself.
    """
    locked = _FakeLockedFlake(
        LockedNode(locked_ref="github:NixOS/nixpkgs/abc123", original_ref="nixpkgs", is_flake=True),
    )
    assert await _nixpkgs_flake_ref(locked) == "github:NixOS/nixpkgs/abc123"


async def test_a_file_target_falls_back_to_the_registry() -> None:
    """``--file`` has no lock at all, which is ``defaultNixpkgsFlakeRef()``."""
    assert await _nixpkgs_flake_ref(None) == "nixpkgs"


async def test_a_flake_that_locks_no_nixpkgs_falls_back_to_the_registry() -> None:
    """``findInput`` answers nothing, and Nix falls back the same way."""
    assert await _nixpkgs_flake_ref(_FakeLockedFlake(None)) == "nixpkgs"


async def test_a_nixpkgs_input_that_is_not_a_flake_falls_back() -> None:
    """Nix checks ``isFlake`` before it takes the node, and so does this.

    An input declared ``flake = false`` is a source tree, not something with
    ``legacyPackages.<system>.bashInteractive`` to evaluate.
    """
    locked = _FakeLockedFlake(
        LockedNode(locked_ref="github:NixOS/nixpkgs/abc123", original_ref="nixpkgs", is_flake=False),
    )
    assert await _nixpkgs_flake_ref(locked) == "nixpkgs"


# --- the oracle -----------------------------------------------------------


async def test_print_dev_env_json_matches_nix(
    shared_nix_environment: NixTestEnvironment,
    nixpkgs_path: str,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    nix_file = _write(tmp_path, "plain.nix", _PLAIN_DERIVATION, nixpkgs_path)
    theirs = await _nix_print_dev_env(nix_file, shared_nix_environment.store_uri)

    cmd = parse(
        ["print-dev-env", "--file", str(nix_file), "--json", *shared_nix_environment.pynix_store_args()],
    )
    await cmd.run()
    mine = json.loads(capsys.readouterr().out)

    _compare(mine, theirs)
    assert mine["variables"]["PYNIX_MARKER"]["value"] == "a marker with 'quotes' and $dollars"


async def test_print_dev_env_json_matches_nix_for_structured_attrs(
    shared_nix_environment: NixTestEnvironment,
    nixpkgs_path: str,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    nix_file = _write(tmp_path, "structured.nix", _STRUCTURED_DERIVATION, nixpkgs_path)
    theirs = await _nix_print_dev_env(nix_file, shared_nix_environment.store_uri)

    cmd = parse(
        ["print-dev-env", "--file", str(nix_file), "--json", *shared_nix_environment.pynix_store_args()],
    )
    await cmd.run()
    mine = json.loads(capsys.readouterr().out)

    assert "structuredAttrs" in mine, "a __structuredAttrs derivation must report them"
    _compare(mine, theirs)


# --- the script, and what it restores --------------------------------------


async def test_print_dev_env_prints_bash_that_restores_the_environment(
    shared_nix_environment: NixTestEnvironment,
    nixpkgs_path: str,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Run the printed script under bash, and read the variables back out."""
    nix_file = _write(tmp_path, "plain.nix", _PLAIN_DERIVATION, nixpkgs_path)
    cmd = parse(["print-dev-env", "--file", str(nix_file), *shared_nix_environment.pynix_store_args()])
    await cmd.run()
    script = capsys.readouterr().out

    # A bash array, and a bash function, both restored. Nix flattens a Nix list
    # to a space-separated string, so PYNIX_LIST is not the array: the arrays in
    # a plain derivation come from stdenv, and `pkgsBuildHost` is one of them.
    probe = (
        '\necho "[$PYNIX_MARKER][$PYNIX_SHELL_HOOK_RAN][$PYNIX_LIST]"\n'
        'echo "[$(type -t runHook)][${#pkgsBuildHost[@]}]"\n'
    )
    rc_file = tmp_path / "rc"
    await AnyioPath(rc_file).write_text(script + probe)
    result = await run_process(["bash", str(rc_file)], check=False)
    assert result.returncode == 0, result.stderr.decode()
    output = result.stdout.decode()
    assert "[a marker with 'quotes' and $dollars][1][one two]" in output
    assert "[function][" in output
    assert "declare -a " in script


async def test_the_original_derivation_is_untouched(
    shared_nix_environment: NixTestEnvironment,
    nixpkgs_path: str,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The rewrite writes a new derivation; it does not edit the old one."""
    nix_file = _write(tmp_path, "plain.nix", _PLAIN_DERIVATION, nixpkgs_path)
    store_args = shared_nix_environment.pynix_store_args()

    await parse(["derivation", "show", "--file", str(nix_file), *store_args]).run()
    before = json.loads(capsys.readouterr().out)

    await parse(["print-dev-env", "--file", str(nix_file), "--json", *store_args]).run()
    capsys.readouterr()

    await parse(["derivation", "show", "--file", str(nix_file), *store_args]).run()
    after = json.loads(capsys.readouterr().out)

    assert before == after
    drv_path = next(iter(before))
    assert not drv_path.endswith("-env.drv")
    assert "get-env.sh" not in json.dumps(before)


async def test_a_derivation_whose_builder_is_not_bash_is_refused(
    shared_nix_environment: NixTestEnvironment,
    nixpkgs_path: str,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    nix_file = _write(tmp_path, "non-bash.nix", _NON_BASH_DERIVATION, nixpkgs_path)
    cmd = parse(
        ["print-dev-env", "--file", str(nix_file), "--json", *shared_nix_environment.pynix_store_args()],
    )
    with pytest.raises(SystemExit):
        await cmd.run()
    assert "only works on derivations that use 'bash' as their builder" in capsys.readouterr().err


# --- develop, which execs ---------------------------------------------------


def _run_develop(
    nix_file: Path, store_uri: str, command: list[str], nixpkgs_path: str
) -> subprocess.CompletedProcess[str]:
    """Run ``pynix develop`` in a subprocess.

    A subprocess, and not ``astart()``: ``develop`` ends in ``os.execvp``, so
    running it in-process would replace the pytest interpreter with bash.
    """
    argv = [
        sys.executable,
        "-c",
        "import sys; from pynix import main; sys.argv = ['pynix', *sys.argv[1:]]; main()",
        "develop",
        "--file",
        str(nix_file),
        "--store",
        store_uri,
        "--",
        *command,
    ]
    return subprocess.run(  # noqa: S603 -- fixed argv, no shell, every path from the test itself
        argv,
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "NIX_PATH": f"nixpkgs={nixpkgs_path}"},
    )


def test_develop_runs_a_command_in_the_environment(
    shared_nix_environment: NixTestEnvironment,
    nixpkgs_path: str,
    tmp_path: Path,
) -> None:
    nix_file = _write(tmp_path, "plain.nix", _PLAIN_DERIVATION, nixpkgs_path)
    result = _run_develop(
        nix_file,
        shared_nix_environment.store_uri,
        ["bash", "-c", 'echo "[$PYNIX_MARKER][$PYNIX_SHELL_HOOK_RAN]"'],
        nixpkgs_path,
    )
    assert result.returncode == 0, result.stderr
    assert "[a marker with 'quotes' and $dollars][1]" in result.stdout


def test_develop_reports_the_exit_status_of_its_command(
    shared_nix_environment: NixTestEnvironment,
    nixpkgs_path: str,
    tmp_path: Path,
) -> None:
    """``exec`` is what makes the command's status the status of pynix."""
    nix_file = _write(tmp_path, "plain.nix", _PLAIN_DERIVATION, nixpkgs_path)
    result = _run_develop(nix_file, shared_nix_environment.store_uri, ["bash", "-c", "exit 42"], nixpkgs_path)
    assert result.returncode == 42, result.stderr


# --- the composition, without a store --------------------------------------


def _environment(**variables: str) -> BuildEnvironment:
    document = {
        "variables": {
            "outputs": {"type": "var", "value": "out"},
            "out": {"type": "var", "value": "/nix/store/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-thing"},
            **{name: {"type": "exported", "value": value} for name, value in variables.items()},
        },
        "bashFunctions": {},
    }
    return BuildEnvironment.from_json(json.dumps(document))


def test_the_script_ends_in_exec_so_the_command_keeps_its_status(tmp_path: Path) -> None:
    script = compose_shell_script(_environment(), command=["make", "-j4"], outputs_dir=tmp_path / "outputs")
    assert script.endswith("exec 'make' '-j4'\n")


def test_the_script_removes_its_own_directory_before_the_command_runs(tmp_path: Path) -> None:
    """After ``exec`` nothing runs, so the cleanup line has to come first."""
    script = compose_shell_script(
        _environment(),
        command=["true"],
        outputs_dir=tmp_path / "outputs",
        cleanup=tmp_path / "rcdir",
    )
    lines = script.splitlines()
    assert lines.index(f"command rm -rf '{tmp_path / 'rcdir'}'") < lines.index("exec 'true'")


def test_an_interactive_script_reads_bashrc_first(tmp_path: Path) -> None:
    script = compose_shell_script(_environment(), command=[], outputs_dir=tmp_path / "outputs")
    assert script.startswith('[ -n "$PS1" ] && [ -e ~/.bashrc ] && source ~/.bashrc;\n')
    assert script.endswith("shopt -s expand_aliases\n")
    assert "exec " not in script


# --- the interactive shell, and the two lines it adds -----------------------


def _nixpkgs_shell() -> InteractiveShell:
    return InteractiveShell(path="/nix/store/bbbb-bash-interactive/bin/bash", from_nixpkgs=True, exec_prefix=[])


def test_an_interactive_script_overrides_shell_and_path(tmp_path: Path) -> None:
    """Otherwise the build's bash, which has no readline, becomes ``$SHELL``.

    ``develop.cc:688`` and ``:690``. The ``PATH`` line puts the chosen bash
    ahead of the build's, so ``command -v bash`` agrees with ``$SHELL``.
    """
    script = compose_shell_script(
        _environment(),
        command=[],
        outputs_dir=tmp_path / "outputs",
        shell=_nixpkgs_shell(),
    )
    assert 'SHELL="/nix/store/bbbb-bash-interactive/bin/bash"\n' in script
    assert 'PATH="/nix/store/bbbb-bash-interactive/bin${PATH:+:$PATH}"\n' in script


def test_the_fallback_shell_does_not_touch_path(tmp_path: Path) -> None:
    """``develop.cc:689`` guards the PATH line on the lookup succeeding.

    A bash found on PATH is on PATH already, so prepending its directory would
    reorder the caller's PATH for nothing.
    """
    shell = InteractiveShell(path="/usr/bin/bash", from_nixpkgs=False, exec_prefix=[])
    script = compose_shell_script(_environment(), command=[], outputs_dir=tmp_path / "outputs", shell=shell)
    assert 'SHELL="/usr/bin/bash"\n' in script
    assert 'PATH="/usr/bin${PATH:+:$PATH}"\n' not in script


def test_a_command_gets_no_shell_line(tmp_path: Path) -> None:
    """Nix appends its SHELL line after ``exec``, where nothing runs it.

    So ``nix develop --command`` leaves ``$SHELL`` at the build's bash, and
    matching that is what keeps the two commands the same. Measured: both
    report the build's ``bash-5.3p15`` for a command.
    """
    script = compose_shell_script(
        _environment(),
        command=["true"],
        outputs_dir=tmp_path / "outputs",
        shell=_nixpkgs_shell(),
    )
    assert "SHELL=" not in script
    assert script.endswith("exec 'true'\n")


def test_an_output_path_is_rewritten_to_the_outputs_directory(tmp_path: Path) -> None:
    outputs = tmp_path / "outputs"
    script = make_rc_script(_environment(), outputs_dir=outputs)
    assert f"out='{outputs / 'out'}'" in script
    assert "/nix/store/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-thing" not in script


@pytest.mark.parametrize(
    ("word", "expected"),
    [
        ("plain", "'plain'"),
        ("", "''"),
        ("a b", "'a b'"),
        ("it's", "'it'\\''s'"),
        ("$HOME", "'$HOME'"),
    ],
)
def test_quote_always_quotes_like_nix(word: str, expected: str) -> None:
    """``escapeShellArgAlways``, not ``shlex.quote``.

    shlex leaves a word that needs no quoting bare, which would make every
    printed script differ from ``nix print-dev-env`` on most of its lines.
    """
    assert quote(word) == expected
