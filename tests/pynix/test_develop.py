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
import shutil
import subprocess
import sys
from typing import TYPE_CHECKING, Any, cast

import pytest
from anyio import Path as AnyioPath, run_process
from pynix._dev_env import BuildEnvironment, make_rc_script, quote
from pynix.develop import compose_shell_script, take_unparsed

from pynix import Pynix
from tests.support.nix_environment import with_nixpkgs

if TYPE_CHECKING:
    from pathlib import Path

    from tests.support.nix_environment import NixTestEnvironment

#: A store path, with the hash reduced to a placeholder. pynix and nix build a
#: *different* ``-env`` derivation, because pynix's vendored ``get-env.sh``
#: carries a provenance header, so every path derived from that derivation
#: differs by its hash alone.
_STORE_PATH = re.compile(r"/nix/store/[a-z0-9]{32}-")

#: The two variables that cannot agree, and why. Both follow from the header on
#: the vendored script, which is a deliberate choice and not a defect:
#:
#: - ``LINENO`` is the line of ``get-env.sh`` that dumps the environment, and
#:   the header moves it down the file.
#: - ``NIX_CFLAGS_COMPILE`` carries ``-frandom-seed=``, which stdenv derives
#:   from the output path, and the output path follows the derivation.
_EXPECTED_DIFFERENCES = frozenset({"LINENO", "NIX_CFLAGS_COMPILE"})

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
    if shutil.which("nix") is None:
        pytest.skip("the nix CLI is the oracle for this test, and it is not on PATH")
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
    assert differing <= _EXPECTED_DIFFERENCES, {
        name: (my_variables[name], their_variables[name]) for name in sorted(differing - _EXPECTED_DIFFERENCES)
    }


# --- the assumption everything rests on -----------------------------------


def test_a_command_after_the_double_dash_reaches_the_command_untouched() -> None:
    """clypi stops parsing at ``--``, so no option after it is interpreted.

    This is what lets ``develop`` take its command the way the shell would,
    rather than through a ``--command`` option as ``nix develop`` does. Every
    other test here assumes it.
    """
    cmd = Pynix.parse(["develop", "--file", "x.nix", "--", "make", "-j4", "--jobs=2", "-- literal"])
    assert take_unparsed(type(cmd.subcommand)) == ["make", "-j4", "--jobs=2", "-- literal"]


def test_a_second_parse_does_not_inherit_the_first_command() -> None:
    """clypi keeps the tail on the class and never clears it.

    ``take_unparsed`` is what clears it. Without that, a ``develop`` with no
    ``--`` of its own runs whatever the previous parse asked for -- which a
    one-parse-per-process command line would never reveal.
    """
    first = Pynix.parse(["develop", "--file", "x.nix", "--", "make"])
    assert take_unparsed(type(first.subcommand)) == ["make"]

    second = Pynix.parse(["develop", "--file", "x.nix"])
    assert take_unparsed(type(second.subcommand)) == []


# --- the oracle -----------------------------------------------------------


async def test_print_dev_env_json_matches_nix(
    shared_nix_environment: NixTestEnvironment,
    nixpkgs_path: str,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    nix_file = _write(tmp_path, "plain.nix", _PLAIN_DERIVATION, nixpkgs_path)
    theirs = await _nix_print_dev_env(nix_file, shared_nix_environment.store_uri)

    cmd = Pynix.parse(
        ["print-dev-env", "--file", str(nix_file), "--json", *shared_nix_environment.pynix_store_args()],
    )
    await cmd.astart()
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

    cmd = Pynix.parse(
        ["print-dev-env", "--file", str(nix_file), "--json", *shared_nix_environment.pynix_store_args()],
    )
    await cmd.astart()
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
    cmd = Pynix.parse(["print-dev-env", "--file", str(nix_file), *shared_nix_environment.pynix_store_args()])
    await cmd.astart()
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

    await Pynix.parse(["derivation", "show", "--file", str(nix_file), *store_args]).astart()
    before = json.loads(capsys.readouterr().out)

    await Pynix.parse(["print-dev-env", "--file", str(nix_file), "--json", *store_args]).astart()
    capsys.readouterr()

    await Pynix.parse(["derivation", "show", "--file", str(nix_file), *store_args]).astart()
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
    cmd = Pynix.parse(
        ["print-dev-env", "--file", str(nix_file), "--json", *shared_nix_environment.pynix_store_args()],
    )
    with pytest.raises(SystemExit):
        await cmd.astart()
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
