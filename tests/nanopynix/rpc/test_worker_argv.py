"""The command line a stdio worker is started with, both directions.

The client writes it and the worker reads it, in two different processes, so
nothing else would notice that the two halves had drifted apart. A session
test would notice eventually, and only as a worker that died with no reason.
"""

from __future__ import annotations

import sys

import pytest
from pydantic import ValidationError

from nanopynix.namespace import OverlayNamespace
from nanopynix.rpc._worker_argv import (
    NAMESPACE_JSON,
    NAMESPACE_OPTION,
    WORKER_MODULE,
    parse_worker_argv,
    worker_argv,
)

_NAMESPACE = OverlayNamespace(
    upper_dir="/tmp/upper",
    work_dir="/tmp/work",
    state_dir="/tmp/state",
    log_dir="/tmp/log",
)


def test_the_plain_argv_names_the_package_and_this_interpreter() -> None:
    """``python -m nanopynix.rpc.worker``, and nothing else.

    The module is the package rather than ``...worker._worker``, because that
    module is already imported by the time ``python -m`` would run it: measured,
    it warns and executes twice. The interpreter is this one, so that the
    worker is the same installed build as the client.
    """
    assert worker_argv() == [sys.executable, "-m", WORKER_MODULE]
    assert WORKER_MODULE == "nanopynix.rpc.worker"


def test_a_namespace_survives_the_argument_vector() -> None:
    """The round trip is the whole contract, and this is the test of it."""
    argv = worker_argv(namespace=_NAMESPACE)

    assert argv[:3] == [sys.executable, "-m", WORKER_MODULE]
    assert argv[3] == NAMESPACE_OPTION
    assert parse_worker_argv(argv[3:]).namespace == _NAMESPACE


def test_no_namespace_gives_no_option() -> None:
    """A worker with nothing to enter is told nothing.

    ``connect_ssh_stdio`` runs the console script with no arguments at all, so
    the empty case has to be the one that needs no flag.
    """
    assert NAMESPACE_OPTION not in worker_argv()
    assert parse_worker_argv([]).namespace is None


def test_the_default_field_is_carried_rather_than_reconstructed() -> None:
    """``lower_store`` has a default, and the default is not the only value.

    Rendering only the fields without defaults would round-trip the namespace
    above and quietly lose this one.
    """
    named = OverlayNamespace(
        upper_dir="/tmp/upper",
        work_dir="/tmp/work",
        state_dir="/tmp/state",
        log_dir="/tmp/log",
        lower_store="local?root=/tmp/lower",
    )
    parsed = parse_worker_argv(worker_argv(namespace=named)[3:]).namespace
    assert parsed is not None
    assert parsed.lower_store == "local?root=/tmp/lower"


_REFUSED = [
    pytest.param(
        '{"upper_dir": "/a", "work_dir": "/b", "state_dir": "/c", "log_dir": "/d", "nope": "x"}',
        "nope",
        id="unknown field",
    ),
    pytest.param('{"upper_dir": "/a"}', "work_dir", id="missing field"),
    pytest.param(
        '{"upper_dir": 3, "work_dir": "/b", "state_dir": "/c", "log_dir": "/d"}', "upper_dir", id="wrong type"
    ),
    pytest.param("not json at all", "JSON", id="not json"),
    pytest.param("[1, 2]", "object", id="not an object"),
]


@pytest.mark.parametrize(("value", "expected"), _REFUSED)
def test_a_bad_namespace_ends_the_worker(value: str, expected: str) -> None:  # noqa: ARG001 -- one list serves this test and the one below; `expected` is that one's subject
    """argparse exits 2 rather than starting a worker on a value it cannot read.

    The unknown-field case is the one that needed work. pydantic accepts an
    unknown key and discards it by default, measured, so a misspelt field
    would have handed the worker a namespace that is not the one the client
    asked for and said nothing. ``OverlayNamespace.__pydantic_config__``
    carries the ``extra="forbid"`` that refuses it.
    """
    with pytest.raises(SystemExit) as raised:
        parse_worker_argv([NAMESPACE_OPTION, value])

    assert raised.value.code == 2


@pytest.mark.parametrize(("value", "expected"), _REFUSED)
def test_the_reason_names_the_field(value: str, expected: str) -> None:
    """What argparse prints comes from pydantic, and it is specific.

    Split from the test above because argparse writes its message to stderr
    and then exits, so the status is all that assertion can see. A refusal
    that said only "invalid value" would pass that test and be useless to the
    person who ran the console script by hand.
    """
    with pytest.raises(ValidationError) as raised:
        NAMESPACE_JSON.validate_json(value)

    assert expected in str(raised.value)
