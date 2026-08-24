"""Tests for the binary-name index that Hydra ships.

The fixture is a real SQLite file, built here with the schema and the rows that
`programs.sqlite` really has. **No test fetches from the network**, which is
what issue #85 asks for, and the rows below are copied from the published
index: `ssh-keygen` really does come from `openssh`, and `convert` from
`imagemagick`.
"""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING

import pytest

from pynix._programs import ProgramIndex

if TYPE_CHECKING:
    from pathlib import Path

#: `(binary, system, package)`, as the published index stores them.
_ROWS = [
    ("rg", "x86_64-linux", "ripgrep"),
    ("rg", "aarch64-linux", "ripgrep"),
    # Neither of the next two is its package's `mainProgram`, and that is the
    # whole reason this index exists.
    ("ssh-keygen", "x86_64-linux", "openssh"),
    ("ssh", "x86_64-linux", "openssh"),
    ("sshd", "x86_64-linux", "openssh"),
    ("convert", "x86_64-linux", "imagemagick"),
    ("convert", "x86_64-linux", "graphicsmagick-imagemagick-compat"),
    # One binary that only another system has.
    ("only-on-arm", "aarch64-linux", "somepackage"),
]


@pytest.fixture
def index(tmp_path: Path) -> ProgramIndex:
    database = tmp_path / "programs.sqlite"
    connection = sqlite3.connect(database)
    with connection:
        connection.execute(
            "create table Programs ("
            " name text not null, system text not null, package text not null,"
            " primary key (name, system, package))"
        )
        connection.executemany("insert into Programs values (?, ?, ?)", _ROWS)
    connection.close()
    return ProgramIndex(path=database, system="x86_64-linux")


def test_a_binary_resolves_to_the_package_that_gives_it(index: ProgramIndex) -> None:
    assert index.packages_for_binary("rg") == ["ripgrep"]


def test_a_binary_that_is_not_a_main_program_resolves(index: ProgramIndex) -> None:
    """The question `meta.mainProgram` cannot answer, and this index can.

    `openssh` names `ssh` as its `mainProgram`, so a search over that field
    alone finds nothing for `ssh-keygen`.
    """
    assert index.packages_for_binary("ssh-keygen") == ["openssh"]


def test_one_binary_can_come_from_several_packages(index: ProgramIndex) -> None:
    """`convert` is the example, and the answer has to name both."""
    assert index.packages_for_binary("convert") == [
        "graphicsmagick-imagemagick-compat",
        "imagemagick",
    ]


def test_a_binary_of_another_system_does_not_answer(index: ProgramIndex) -> None:
    """The rows are per system, and a query that ignores that lies.

    Measured on the published index: 83 048 rows for `x86_64-linux` and
    77 917 for `aarch64-linux`.
    """
    assert index.packages_for_binary("only-on-arm") == []
    assert ProgramIndex(path=index.path, system="aarch64-linux").packages_for_binary("only-on-arm") == ["somepackage"]


def test_an_unknown_binary_answers_with_nothing(index: ProgramIndex) -> None:
    assert index.packages_for_binary("no-such-program") == []


def test_a_package_reports_every_binary_it_gives(index: ProgramIndex) -> None:
    """A page of results says what arrives on PATH, and this fills that in."""
    assert index.binaries_for_packages(["openssh"]) == {"openssh": ["ssh", "ssh-keygen", "sshd"]}


def test_several_packages_resolve_in_one_query(index: ProgramIndex) -> None:
    """The caller passes the page it shows, and not the whole index."""
    found = index.binaries_for_packages(["openssh", "ripgrep", "imagemagick"])
    assert found == {
        "openssh": ["ssh", "ssh-keygen", "sshd"],
        "ripgrep": ["rg"],
        "imagemagick": ["convert"],
    }


def test_a_package_with_no_binary_is_still_in_the_answer(index: ProgramIndex) -> None:
    """A caller reads the result by key, so a missing key would raise on it."""
    assert index.binaries_for_packages(["not-in-the-index"]) == {"not-in-the-index": []}


def test_no_package_asks_nothing_of_the_database(index: ProgramIndex) -> None:
    assert index.binaries_for_packages([]) == {}


def test_a_repeated_package_is_asked_for_once(index: ProgramIndex) -> None:
    assert index.binaries_for_packages(["ripgrep", "ripgrep"]) == {"ripgrep": ["rg"]}


def test_the_index_never_writes_to_the_database(index: ProgramIndex) -> None:
    """The file is in the Nix store, which is read-only, so a write would fail.

    The connection is opened `mode=ro` for that reason, and this test is what
    says so.
    """
    index.packages_for_binary("rg")
    with sqlite3.connect(f"file:{index.path}?mode=ro", uri=True) as connection, pytest.raises(sqlite3.OperationalError):
        connection.execute("insert into Programs values ('x', 'x86_64-linux', 'y')")
