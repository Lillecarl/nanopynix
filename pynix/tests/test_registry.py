"""``pynix registry``, against a registry file and a flake that this test owns.

Issue #87. Four things have to hold: an added entry appears in the list, a
removed one does not, a resolution through an entry reaches the flake the
entry names, and the list agrees with ``nix registry list``.

**Each case owns its registry file, and no case writes the registry of the
person running the suite.** Two mechanisms give that, and each covers one
half:

* A write takes ``--registry``, which names the file directly. The binding
  reads that file from disk each time, so a write in this process is not
  affected by what another test read first.
* A read goes through ``NIX_CONFIG_HOME``, which is where ``getConfigDir``
  (``libutil/users.cc``) looks first and so is what makes the file the *user*
  layer. ``pynix registry list`` reads the layers that Nix caches, because
  that is what makes it agree with ``nix registry list``.

**So a read runs in a subprocess and a write runs in this process.** Nix keeps
each registry layer in a function-local static, so the first read of a process
decides for the whole process. A second case that read in this process would
get the first case's answer. ``nanopynix/tests/test_flake_registry.py`` states
the same rule for the engines.

The fixture flake is a local directory with no inputs, so no case needs the
network.
"""

from __future__ import annotations

import json
import sys
from typing import TYPE_CHECKING, Any

import anyio
import pytest

from nanopynix import strip_ansi
from pynix import parse
from support.nix_oracle import require_matching_nix_cli
from test_support.subprocess_output import run_process

if TYPE_CHECKING:
    from pathlib import Path

    from nanopynix_testing.nix_environment import NixTestEnvironment

#: A flake with no inputs and one attribute, so a resolution through the
#: registry can be observed without a fetch of anything remote.
_FIXTURE_FLAKE = """{
  description = "the fixture flake of pynix registry";
  outputs = { self }: { probe = "a-probe-value"; };
}
"""

#: The indirect reference each case adds. Not `nixpkgs`: an entry for that
#: name exists on most machines, and a case has to see its own.
PROBE = "apynixregistryprobe"

#: The length of the hexadecimal git revision that a pin writes.
_SHA1_LENGTH = 40

#: The fields of one line of `nix registry list`: the layer, `from` and `to`.
_LIST_LINE_FIELDS = 3

#: One pynix command, in a process of its own.
#:
#: The real parser and the real command, so a change to a declaration is a
#: change this sees. `python -m pynix` is not it: the package has no
#: `__main__`, and the console script is a name on the PATH rather than a
#: thing this suite installs.
_CLI_SCRIPT = """
import sys

import anyio

from pynix import parse


async def main() -> None:
    await parse(sys.argv[1:]).run()


anyio.run(main)
"""


@pytest.fixture
def fixture_flake(tmp_path: Path) -> str:
    directory = tmp_path / "fixture"
    directory.mkdir()
    (directory / "flake.nix").write_text(_FIXTURE_FLAKE, encoding="utf-8")
    return f"path:{directory}"


@pytest.fixture
def own_registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The registry file of this case, where Nix reads the user layer.

    The directory holds nothing else. ``getUserRegistryPath`` is
    ``getConfigDir() / "registry.json"``, and ``NIX_CONFIG_HOME`` is the first
    thing ``getConfigDir`` reads.
    """
    directory = tmp_path / "config"
    directory.mkdir()
    monkeypatch.setenv("NIX_CONFIG_HOME", str(directory))
    return directory / "registry.json"


async def _run(arguments: list[str], capsys: pytest.CaptureFixture[str]) -> dict[str, Any]:
    """One pynix command in this process, and its JSON answer."""
    command = parse(arguments)
    await command.run()
    return json.loads(capsys.readouterr().out)


async def _list_in_a_subprocess() -> dict[str, Any]:
    """``pynix registry list`` in a process that has read no layer yet."""
    result = await run_process([sys.executable, "-c", _CLI_SCRIPT, "registry", "list"])
    assert result.returncode == 0, result.describe()
    return json.loads(result.stdout)


def _user_entries(listing: dict[str, Any]) -> list[dict[str, Any]]:
    return [entry for entry in listing["entries"] if entry["type"] == "user"]


async def test_an_added_entry_appears_in_the_list(
    own_registry: Path,
    fixture_flake: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    added = await _run(["registry", "add", PROBE, fixture_flake, "--registry", str(own_registry)], capsys)

    assert added["path"] == str(own_registry)
    assert added["replaced"] == 0
    assert added["to"] == fixture_flake

    listing = await _list_in_a_subprocess()
    assert listing["userRegistry"] == str(own_registry)
    assert _user_entries(listing) == [
        {"type": "user", "from": f"flake:{PROBE}", "to": fixture_flake, "exact": False, "extraAttrs": {}},
    ]


async def test_a_removed_entry_is_gone_from_the_list(
    own_registry: Path,
    fixture_flake: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    await _run(["registry", "add", PROBE, fixture_flake, "--registry", str(own_registry)], capsys)

    removed = await _run(["registry", "remove", PROBE, "--registry", str(own_registry)], capsys)

    assert removed["removed"] == 1
    assert _user_entries(await _list_in_a_subprocess()) == []


async def test_removing_nothing_says_so(
    own_registry: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Nix's own command is silent either way, so a caller cannot tell.

    ``removed`` is the whole reason ``RegistryWrite`` carries a count.
    """
    result = await _run(["registry", "remove", PROBE, "--registry", str(own_registry)], capsys)

    assert result["removed"] == 0


async def test_a_second_add_replaces_the_first(
    own_registry: Path,
    fixture_flake: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``CmdRegistryAdd::run`` removes before it adds, so an entry does not join one."""
    await _run(["registry", "add", PROBE, fixture_flake, "--registry", str(own_registry)], capsys)

    again = await _run(
        ["registry", "add", PROBE, "github:an-owner/a-repo", "--registry", str(own_registry)],
        capsys,
    )

    assert again["replaced"] == 1
    assert again["to"] == "github:an-owner/a-repo"
    assert _user_entries(await _list_in_a_subprocess()) == [
        {
            "type": "user",
            "from": f"flake:{PROBE}",
            "to": "github:an-owner/a-repo",
            "exact": False,
            "extraAttrs": {},
        },
    ]


async def test_a_resolution_reaches_the_flake_that_the_entry_names(
    own_registry: Path,
    fixture_flake: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The entry is what makes the indirect reference mean anything.

    ``pynix eval`` runs in a subprocess for the reason this module gives: the
    resolution reads the user layer, and this process may already have read
    another one.
    """
    await _run(["registry", "add", PROBE, fixture_flake, "--registry", str(own_registry)], capsys)

    result = await run_process([sys.executable, "-c", _CLI_SCRIPT, "eval", "--flake", f"{PROBE}#probe"])

    assert result.returncode == 0, result.describe()
    assert json.loads(result.stdout) == "a-probe-value"


@pytest.fixture
async def git_fixture_flake(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """The same flake, in a git working tree, so a pin has a revision to take.

    A ``path:`` reference carries no revision, so pinning one writes an entry
    that still moves. A git one carries ``rev``, which is what
    ``nix registry pin`` exists to write.

    The identity comes from the environment rather than from a git
    configuration, because a sandboxed gate has neither a home directory to
    read one from nor a global one to fall back on.
    """
    directory = anyio.Path(tmp_path / "git-fixture")
    await directory.mkdir()
    await (directory / "flake.nix").write_text(_FIXTURE_FLAKE, encoding="utf-8")
    for name in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{name}_NAME", "pynix tests")
        monkeypatch.setenv(f"GIT_{name}_EMAIL", "tests@example.invalid")
    for arguments in (["init"], ["add", "flake.nix"], ["commit", "-m", "the fixture"]):
        result = await run_process(["git", *arguments], cwd=directory)
        assert result.returncode == 0, result.describe()
    return f"git+file://{directory}"


async def test_a_pin_writes_a_reference_that_carries_a_revision(
    shared_nix_environment: NixTestEnvironment,
    own_registry: Path,
    git_fixture_flake: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A pin resolves and fetches, and the fetch is what finds the revision.

    The second argument names what to pin to. It is a direct reference here,
    so the pin needs no lookup in the registry -- which matters, because this
    process may have read a registry layer already and Nix caches that.
    """
    store = shared_nix_environment.pynix_store_args()
    await _run(["registry", "add", PROBE, git_fixture_flake, "--registry", str(own_registry), *store], capsys)

    pinned = await _run(
        ["registry", "pin", PROBE, git_fixture_flake, "--registry", str(own_registry), *store],
        capsys,
    )

    assert pinned["replaced"] == 1
    assert pinned["locked"] is True
    written = json.loads(await anyio.Path(own_registry).read_text())["flakes"][0]["to"]
    assert written["type"] == "git"
    assert len(str(written["rev"])) == _SHA1_LENGTH


async def test_a_pin_of_a_path_reports_that_it_is_not_locked(
    shared_nix_environment: NixTestEnvironment,
    own_registry: Path,
    fixture_flake: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Nix warns here, and this reports it instead.

    ``Input::isLocked`` is false for a reference with no revision, and a
    caller is the one who can tell whether that matters.
    """
    store = shared_nix_environment.pynix_store_args()

    pinned = await _run(
        ["registry", "pin", PROBE, fixture_flake, "--registry", str(own_registry), *store],
        capsys,
    )

    assert pinned["locked"] is False


async def test_the_list_agrees_with_the_nix_command(
    own_registry: Path,
    fixture_flake: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The acceptance criterion of issue #87, stated against the oracle.

    ``nix registry list`` prints one entry per line as ``<layer> <from>
    <to>``, and it composes the extra attributes of an entry into a query on
    the target. This rebuilds that line from the JSON, so the two are compared
    as the same text.
    """
    await require_matching_nix_cli()
    await _run(["registry", "add", PROBE, fixture_flake, "--registry", str(own_registry)], capsys)

    listing = await _list_in_a_subprocess()
    oracle = await run_process(["nix", "--extra-experimental-features", "nix-command flakes", "registry", "list"])
    assert oracle.returncode == 0, oracle.describe()

    ours = [(entry["type"], entry["from"]) for entry in listing["entries"]]
    theirs = [
        # `nix registry list` pads the layer name into a column, and it prints
        # "flags" where the enumerator is `Flag`.
        (parts[0].strip().removesuffix("s") if parts[0].strip() == "flags" else parts[0].strip(), parts[1])
        for parts in (line.split(maxsplit=2) for line in strip_ansi(oracle.stdout).splitlines())
        if len(parts) == _LIST_LINE_FIELDS
    ]

    assert ours == theirs
    assert ("user", f"flake:{PROBE}") in ours
