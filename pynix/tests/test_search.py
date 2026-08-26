from __future__ import annotations

import contextlib
import json
import re
import sqlite3
import sys
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple, cast

import anyio
import pytest
from prompt_toolkit.application import create_app_session
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

import pynix._impl.search as search_module
from pynix import parse
from pynix._impl import options_tui
from pynix._impl._search_tui import SearchTui
from pynix._option_values import EvaluatorUnavailableError, OptionValues, Rendered, Trees, Value
from pynix._options import OptionRecord
from pynix._programs import ProgramIndex
from pynix.search import Search
from pynix.target import EvaluationTarget

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Sequence

    from prompt_toolkit.formatted_text import StyleAndTextTuples
    from prompt_toolkit.input import PipeInput

    from nanopynix_testing.nix_environment import NixTestEnvironment

_FIXTURE_DIR = Path(__file__).parent / "test_search"
_SYSTEM_NIX = _FIXTURE_DIR / "system.nix"
_TARGET_DIR = Path(__file__).parent / "test_search_target"

#: How long a write waits for the render that must come before it, in seconds.
_DRAWN = 30.0

#: How long a test waits for the pump to place its first request, in seconds.
#: It covers the index build that comes before the interface, which the daemon
#: backend takes seconds over. The deadline of `test_support.deadline` is 120,
#: so a test that reaches this one still fails rather than hangs.
_ASKED = 60.0

#: How long a test waits between one write to the input and the next.
_SETTLE = 0.05

#: The key that leaves the interface.
_QUIT = "\x03"


def _parse_json_output(out: str) -> object:
    """Extract the JSON portion from captured stdout, skipping structlog lines."""
    _structlog = re.compile(r"^\d{4}-\d{2}-\d{2}\s")
    lines = [line for line in out.splitlines() if not _structlog.match(line)]
    return json.loads("".join(lines))


def _results(out: str) -> list[dict[str, object]]:
    """Parse an ``search --json`` result array into typed records."""
    data = _parse_json_output(out)
    if not isinstance(data, list):
        raise TypeError("expected search --json to print a JSON array")
    records: list[dict[str, object]] = []
    for entry in cast("list[object]", data):
        if not isinstance(entry, dict):
            raise TypeError("expected each search result to be a JSON object")
        records.append(cast("dict[str, object]", entry))
    return records


@pytest.fixture(autouse=True)
def cache_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point every search cache at a fresh per-test directory.

    **`XDG_CACHE_HOME` as well, and not the target cache alone.** The package
    half writes a second cache, keyed by `pkgs.path`, and
    `pynix._packages.cache_path` reads the environment for it. Without this a
    test read the 24 571 packages of the machine's own nixpkgs in place of the
    eight of the fixture, and the merged search then had no room for an
    option. It also wrote into the cache of the person running the suite.
    """
    cache_home = tmp_path / "cache"
    search_dir = cache_home / "pynix" / "search"
    search_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("XDG_CACHE_HOME", str(cache_home))
    monkeypatch.setattr(search_module, "_cache_dir", lambda: search_dir)
    return cache_home


async def test_search_builds_index_and_finds_a_match(
    shared_nix_environment: NixTestEnvironment,
    cache_home: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cmd = parse(
        [
            "search",
            "--options",
            "--file",
            str(_SYSTEM_NIX),
            "--json-output",
            "services.example-daemon.port",
            *shared_nix_environment.pynix_store_args(),
        ],
    )
    await cmd.run()
    captured = capsys.readouterr()
    assert "indexed" in captured.err
    results = _results(captured.out)
    assert results
    assert str(results[0]["name"]) == "services.example-daemon.port"
    assert str(results[0]["type"]).startswith("16 bit unsigned integer")

    cache_files = list((cache_home / "pynix" / "search").glob("*.json"))
    assert len(cache_files) == 1


async def test_search_survives_an_option_whose_default_cannot_be_evaluated(
    shared_nix_environment: NixTestEnvironment,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Regression test: a real-world module (e.g. disko) can define an option's
    default as an expression over `config` that only resolves once a whole
    system is realized. Indexing must not evaluate `default`/`example` at
    all, so such an option (`brokenDefault` in the fixture module) doesn't
    abort the entire bulk fetch, and the rest of the options still index."""
    cmd = parse(
        [
            "search",
            "--options",
            "--file",
            str(_SYSTEM_NIX),
            "--json-output",
            "--limit",
            "100",
            "example-daemon",
            *shared_nix_environment.pynix_store_args(),
        ],
    )
    await cmd.run()
    captured = capsys.readouterr()
    results = _results(captured.out)
    names = {str(result["name"]) for result in results}
    assert "services.example-daemon.brokenDefault" in names
    assert "services.example-daemon.port" in names
    assert "default" not in results[0]


async def test_search_filters_out_internal_options(
    shared_nix_environment: NixTestEnvironment,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cmd = parse(
        [
            "search",
            "--options",
            "--file",
            str(_SYSTEM_NIX),
            "--json-output",
            "--limit",
            "100",
            "secretInternal",
            *shared_nix_environment.pynix_store_args(),
        ],
    )
    await cmd.run()
    captured = capsys.readouterr()
    results = _results(captured.out)
    assert all("secretInternal" not in str(result["name"]) for result in results)


async def test_search_second_run_hits_the_cache_without_a_working_store(
    shared_nix_environment: NixTestEnvironment,
    capsys: pytest.CaptureFixture[str],
) -> None:
    build_cmd = parse(["search", "--options", "--file", str(_SYSTEM_NIX), *shared_nix_environment.pynix_store_args()])
    await build_cmd.run()
    capsys.readouterr()

    # A bogus store URI would make evaluation fail if the cache were bypassed.
    cached_cmd = parse(
        [
            "search",
            "--options",
            "--file",
            str(_SYSTEM_NIX),
            "--json-output",
            "services.example-daemon.enable",
            "--store",
            "local://?root=/nonexistent-store-root",
        ],
    )
    await cached_cmd.run()
    captured = capsys.readouterr()
    results = _results(captured.out)
    assert str(results[0]["name"]) == "services.example-daemon.enable"


async def test_search_update_index_rebuilds_the_cache(
    shared_nix_environment: NixTestEnvironment,
    capsys: pytest.CaptureFixture[str],
) -> None:
    build_cmd = parse(["search", "--options", "--file", str(_SYSTEM_NIX), *shared_nix_environment.pynix_store_args()])
    await build_cmd.run()
    capsys.readouterr()

    rebuild_cmd = parse(
        [
            "search",
            "--options",
            "--file",
            str(_SYSTEM_NIX),
            "--update-index",
            *shared_nix_environment.pynix_store_args(),
        ],
    )
    await rebuild_cmd.run()
    captured = capsys.readouterr()
    assert "indexed" in captured.err


async def test_search_limit_truncates_results(
    shared_nix_environment: NixTestEnvironment,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cmd = parse(
        [
            "search",
            "--options",
            "--file",
            str(_SYSTEM_NIX),
            "--json-output",
            "--limit",
            "1",
            "example-daemon",
            *shared_nix_environment.pynix_store_args(),
        ],
    )
    await cmd.run()
    captured = capsys.readouterr()
    results = _results(captured.out)
    assert len(results) == 1


# -- the full-screen interface -------------------------------------------------
#
# `search --tui` is a second reader of the same cached index. These tests build
# that index from the real module fixture, so the ranking and the detail pane
# run against options that `lib.evalModules` produced, and not against a double.


class _ModeCase(NamedTuple):
    """One row of the table that decides which mode `search` runs."""

    tui: bool | None
    json_output: bool
    query: str | None
    human: bool
    expected: bool


@pytest.mark.parametrize(
    "case",
    [
        # `--tui` and `--no-tui` answer outright, whatever else is true.
        _ModeCase(tui=True, json_output=False, query="port", human=False, expected=True),
        _ModeCase(tui=True, json_output=True, query=None, human=False, expected=True),
        _ModeCase(tui=False, json_output=False, query=None, human=True, expected=False),
        # Without one of them, a person with no query gets the interface.
        _ModeCase(tui=None, json_output=False, query=None, human=True, expected=True),
        _ModeCase(tui=None, json_output=False, query=None, human=False, expected=False),
        # A query asks a question that a list answers.
        _ModeCase(tui=None, json_output=False, query="port", human=True, expected=False),
        # `--json-output` asks for a machine-readable answer.
        _ModeCase(tui=None, json_output=True, query=None, human=True, expected=False),
    ],
)
def test_the_mode_follows_the_flags_the_query_and_the_caller(
    case: _ModeCase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(search_module, "human_at_terminal", lambda: case.human)
    argv = ["search", "--file", str(_SYSTEM_NIX)]
    if case.tui is True:
        argv.append("--tui")
    elif case.tui is False:
        argv.append("--no-tui")
    if case.json_output:
        argv.append("--json-output")
    if case.query is not None:
        argv.append(case.query)
    command = parse(argv)
    if not isinstance(command, Search):
        raise TypeError("expected the parser to build an Search command")
    assert search_module._use_tui(command) is case.expected


@pytest.fixture
async def indexed_options(
    shared_nix_environment: NixTestEnvironment,
    cache_home: Path,
) -> list[OptionRecord]:
    """Every option of the fixture module, indexed by a real evaluation."""
    cmd = parse(
        [
            "search",
            "--options",
            "--file",
            str(_SYSTEM_NIX),
            "--no-tui",
            *shared_nix_environment.pynix_store_args(),
        ],
    )
    await cmd.run()
    (cache_file,) = (cache_home / "pynix" / "search").glob("*.json")
    options = search_module.load_cache(cache_file).options
    assert options is not None
    return options


def _by_name(records: list[OptionRecord], name: str) -> OptionRecord:
    return next(record for record in records if record.name == name)


def test_the_ranking_puts_the_options_in_order_for_an_empty_query(
    indexed_options: list[OptionRecord],
) -> None:
    """An empty query has nothing to rank against, so the names are sorted."""
    ranked = options_tui.rank(indexed_options)("")
    names = [record.name for record in ranked]
    assert names == sorted(names)
    assert len(names) == len(indexed_options)


def test_the_ranking_finds_an_option_by_a_part_of_its_name(
    indexed_options: list[OptionRecord],
) -> None:
    ranked = options_tui.rank(indexed_options)("configFiles")
    assert ranked[0].name == "services.example-daemon.configFiles"


def test_the_ranking_narrows_and_does_not_keep_every_option(
    indexed_options: list[OptionRecord],
) -> None:
    """A query must remove the options that do not match.

    Regression test. `process.extract` returns its best candidates whatever
    they score, so the list held every option of the index in a different
    order and the footer counted all of them. The score cutoff is what makes
    the interface narrow.
    """
    ranked = options_tui.rank(indexed_options)("configFiles")
    assert len(ranked) < len(indexed_options)
    assert all("onfig" in record.name for record in ranked)


def _named(*names: str) -> list[OptionRecord]:
    return [OptionRecord(name=name, type="bool", description=None, declarations=[], read_only=False) for name in names]


def test_the_ranking_finds_a_short_query_inside_a_long_option_name() -> None:
    """A caller who is still typing gives a query far shorter than the name."""
    long_name = "services.nginx.virtualHosts.<name>.locations.<name>.proxyWebsockets"
    ranked = options_tui.rank(_named(long_name, "boot.loader.grub.enable"))("websock")
    assert [record.name for record in ranked] == [long_name]


def test_the_ranking_ignores_the_case_of_the_query() -> None:
    assert len(options_tui.rank(_named("services.a.proxyWebsockets"))("WEBSOCKETS")) == 1


def test_one_more_letter_never_gives_more_options() -> None:
    """Typing must narrow, at every letter.

    Regression test. The ranking scored each name with `partial_ratio` and kept
    everything above a cutoff. A short query matches almost any name that way:
    over a real index of 14 752 options, `vsc` gave 27 matches and `vsco` gave
    500. A caller watched the list grow as they typed.
    """
    records = _named(
        "programs.vscode.enable",
        "programs.vscode.package",
        "programs.vscode.enterprisePolicies",
        "services.openvscode-server.enable",
        "boot.devSize",
        "boot.devShmSize",
        "networking.vswitches",
        "services.openssh.enable",
        "networking.firewall.allowPing",
    )
    rank_query = options_tui.rank(records)
    counts = [len(rank_query(query)) for query in ("v", "vs", "vsc", "vsco", "vscod", "vscode")]
    assert counts == sorted(counts, reverse=True), counts


def test_every_word_of_the_query_must_appear() -> None:
    """Two words narrow, which one long word cannot do."""
    records = _named(
        "programs.vscode.enable",
        "programs.vscode.package",
        "services.openssh.enable",
    )
    rank_query = options_tui.rank(records)
    assert len(rank_query("vscode")) == 2
    assert [record.name for record in rank_query("vscode enable")] == ["programs.vscode.enable"]


def test_the_shorter_and_more_specific_option_ranks_first() -> None:
    records = _named(
        "programs.vscode.profiles.<name>.userSettings.editorAssociations",
        "programs.vscode.enable",
    )
    ranked = options_tui.rank(records)("vscode")
    assert ranked[0].name == "programs.vscode.enable"


def test_a_typo_falls_back_to_a_near_match() -> None:
    """An empty screen answers nothing, so a query no name holds still ranks.

    `vscodee` appears in no option name. The fallback is what keeps the
    interface useful while the caller corrects the spelling.
    """
    records = _named("programs.vscode.enable", "networking.firewall.allowPing")
    ranked = options_tui.rank(records)("vscodee")
    assert [record.name for record in ranked] == ["programs.vscode.enable"]


def test_the_ranking_finds_nothing_for_a_query_that_matches_nothing(
    indexed_options: list[OptionRecord],
) -> None:
    assert options_tui.rank(indexed_options)("zzzzzzzz") == []


def test_the_detail_pane_renders_a_real_myst_description(
    indexed_options: list[OptionRecord],
) -> None:
    """The fixture option carries paragraphs, a code fence and a colon fence.

    `pynix._markdown` is the renderer, and this is the only test that drives it
    over prose that the module system produced rather than a literal string.
    """
    record = _by_name(indexed_options, "services.example-daemon.configFiles")
    text = "".join(fragment[1] for fragment in options_tui.detail(record, 70))

    assert record.name in text
    assert "list of string" in text
    # The paragraphs, the code fence and the colon fence each reach the pane.
    assert "Files that the daemon reads when it starts." in text
    assert 'services.example-daemon.configFiles = [ "/etc/example.conf" ];' in text
    assert "Restart it after a change." in text
    # The renderer draws a left bar beside a code block, and that bar is the
    # sign that `NixMarkdown` handled the fence rather than Rich's default.
    assert "│" in text


def test_the_detail_pane_wraps_to_the_width_it_is_given(
    indexed_options: list[OptionRecord],
) -> None:
    """A narrower pane wraps the description into more lines.

    The test counts lines and not columns. A declaration is an absolute path,
    which the renderer never wraps, so the longest line is that path at every
    width and it hides what the description does.
    """
    record = _by_name(indexed_options, "services.example-daemon.configFiles")
    narrow = _lines(options_tui.detail(record, 40))
    wide = _lines(options_tui.detail(record, 100))
    assert narrow > wide


def _lines(fragments: StyleAndTextTuples) -> int:
    return len("".join(fragment[1] for fragment in fragments).splitlines())


def test_the_detail_pane_marks_a_read_only_option(
    indexed_options: list[OptionRecord],
) -> None:
    record = _by_name(indexed_options, "services.example-daemon.stateVersion")
    assert record.read_only is True
    text = "".join(fragment[1] for fragment in options_tui.detail(record, 70))
    assert "read only" in text

    port = _by_name(indexed_options, "services.example-daemon.port")
    assert port.read_only is False
    assert "read only" not in "".join(fragment[1] for fragment in options_tui.detail(port, 70))


class _StubValues(OptionValues):
    """A resolver that answers at once, so the pane needs no evaluator.

    **A subclass, and not a stand-in object.** `detail` annotates its
    parameter as `OptionValues`, and beartype checks that at run time, so a
    duck-typed double is rejected before the function body runs.

    It records what the pane asked for, because the path is the half that the
    query decides, and a test of the pane is a test of that decision.
    """

    def __init__(self, answer: Rendered) -> None:
        super().__init__(_never_opened)
        self.answer = answer
        self.asked: list[tuple[str, tuple[str, ...]]] = []

    def known(self, name: str, segments: Sequence[str] = ()) -> Rendered:
        self.asked.append((name, tuple(segments)))
        return self.answer


@contextlib.asynccontextmanager
async def _never_opened() -> AsyncGenerator[Trees]:
    """The opener a stub never reaches, because it answers from memory."""
    raise AssertionError("the stub answers without an evaluator")
    yield  # pragma: no cover -- unreachable, and the generator needs it


def test_the_detail_pane_reads_the_value_at_the_path_the_query_names() -> None:
    """One record stands for many instances, and the query says which.

    `services.example-daemon.vhosts.<name>.port` names no value on its own.
    The reader types `...vhosts.web.port`, and that is the path the pane reads
    the configuration at.
    """
    record = OptionRecord(
        name="services.example-daemon.vhosts.<name>.port",
        type="16 bit unsigned integer",
        description=None,
        declarations=[],
        read_only=False,
    )
    answer = Rendered(default=Value(text="80"), value=Value(text="8081"))
    values = _StubValues(answer)
    text = "".join(
        fragment[1]
        for fragment in options_tui.detail(
            record,
            70,
            "services.example-daemon.vhosts.web.port",
            values,
        )
    )
    assert values.asked == [
        ("services.example-daemon.vhosts.<name>.port", ("services", "example-daemon", "vhosts", "web", "port"))
    ]
    assert "value" in text
    assert "8081" in text
    # The default stays on the screen beside it. A reader compares the two.
    assert "80" in text


def test_the_detail_pane_asks_for_no_value_when_the_query_names_no_instance() -> None:
    """A bare option name binds no placeholder, so there is no path to read."""
    record = OptionRecord(
        name="services.example-daemon.vhosts.<name>.port",
        type="16 bit unsigned integer",
        description=None,
        declarations=[],
        read_only=False,
    )
    values = _StubValues(Rendered(default=Value(text="80")))
    options_tui.detail(record, 70, "vhosts", values)
    assert values.asked == [("services.example-daemon.vhosts.<name>.port", ())]


def test_the_detail_pane_names_the_file_that_declares_the_option(
    indexed_options: list[OptionRecord],
) -> None:
    record = _by_name(indexed_options, "services.example-daemon.port")
    text = "".join(fragment[1] for fragment in options_tui.detail(record, 70))
    assert "declared in" in text
    assert "module.nix" in text


async def _typed(tui: SearchTui[OptionRecord], pipe: PipeInput, keys: str) -> None:
    """Start *tui*, type *keys* into it, and then leave it.

    **The write waits for a render, and a fixed wait is not enough.**
    `prompt_toolkit` attaches the read end of the input to the event loop
    inside `run_async`, so a key written before that attach sits in a pipe
    nothing is reading. `after_render` fires from inside `_redraw`, which runs
    after the attach, so a render that happened means the input is being read.

    Measured: a wait of 0.05 s answered this on a fast machine, and this test
    still lost 120 seconds to the deadline in `test-nogc-nix_2_35`.
    `pynix/tests/test_search_tui.py::_Renders` says the rest, and issue #271
    is the CI job it costs.

    **`tui.run_application`, and not `Application.run_async`.** The second one
    installs prompt_toolkit's own exception handler on the event loop, which
    waits for a keypress that a CI runner never sends. `SearchTui.run_application`
    says why in full.
    """
    drawn = anyio.Event()

    def mark(_app: object) -> None:
        drawn.set()

    tui.application.after_render += mark

    async def write() -> None:
        with anyio.move_on_after(_DRAWN):
            await drawn.wait()
        pipe.send_text(keys)
        pipe.send_text(_QUIT)

    async with anyio.create_task_group() as group:
        group.start_soon(tui.run_application)
        group.start_soon(write)


async def test_the_interface_narrows_the_real_index_as_the_caller_types(
    indexed_options: list[OptionRecord],
) -> None:
    """Drive the real application over a pipe, against the real index."""
    source = options_tui.source(indexed_options, subject=str(_SYSTEM_NIX))
    with create_pipe_input() as pipe:
        tui = SearchTui(source, input=pipe, output=DummyOutput())
        with create_app_session(input=pipe, output=DummyOutput()):
            await _typed(tui, pipe, "configFiles")

    assert tui.query == "configFiles"
    assert tui.selection is not None
    assert tui.selection.name == "services.example-daemon.configFiles"
    assert "option" in "".join(fragment[1] for fragment in tui.footer_fragments())


async def test_the_interface_opens_on_the_query_of_the_command_line(
    indexed_options: list[OptionRecord],
) -> None:
    """`search --tui <query>` puts that query in the search bar."""
    source = options_tui.source(indexed_options, subject=str(_SYSTEM_NIX))
    with create_pipe_input() as pipe:
        tui = SearchTui(source, initial_query="stateVersion", input=pipe, output=DummyOutput())
        with create_app_session(input=pipe, output=DummyOutput()):
            await _typed(tui, pipe, "")

    assert tui.query == "stateVersion"
    assert tui.selection is not None
    assert tui.selection.name == "services.example-daemon.stateVersion"


async def test_the_command_opens_the_interface_inside_its_event_loop(
    shared_nix_environment: NixTestEnvironment,
) -> None:
    """`search --tui` runs the interface from inside the loop of the command.

    Regression test. `SearchTui.run` called `Application.run`, which calls
    `asyncio.run`, and every `pynix` command already runs an event loop. The
    command raised "asyncio.run() cannot be called from a running event loop"
    the moment it drew, and no test above catches that: each one awaits the
    application itself rather than going through the command.

    `create_app_session` is what makes this work with no terminal. The
    application takes no input of its own here, exactly as the command builds
    it, so it reads the input of the session.
    """
    with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
        cmd = parse(
            [
                "search",
                "--options",
                "--file",
                str(_SYSTEM_NIX),
                "--tui",
                *shared_nix_environment.pynix_store_args(),
            ],
        )

        # **The quit key repeats, because this test cannot see the
        # application.** The command builds the interface inside itself, so
        # there is no `after_render` to hook, and a key written before
        # `prompt_toolkit` attaches the input is lost. The first key that
        # lands after the attach ends the application, and the rest are never
        # read.
        async def quit_until_it_takes() -> None:
            while True:
                await anyio.sleep(_SETTLE)
                pipe.send_text(_QUIT)

        async with anyio.create_task_group() as group:
            group.start_soon(quit_until_it_takes)
            await cmd.run()
            group.cancel_scope.cancel()


async def test_the_command_writes_nothing_while_the_interface_is_drawn(
    shared_nix_environment: NixTestEnvironment,
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
) -> None:
    """Nothing reaches the terminal between the first draw and the last.

    **The detail pane opens a Nix evaluator while the interface is up**, and
    `pynix._util.forward_nix_logs` prints one structlog line for each log
    event that the evaluator sends. One such line lands in the middle of the
    drawing, and the screen stays wrong until the next full redraw.

    This drives the whole command and reads what the resolver saw. `sys.stderr`
    inside it must not be the object the test holds, because `quiet_terminal`
    swapped it. The line the resolver wrote still reaches the terminal after
    the interface closes, because the guard holds that line and does not drop
    it.
    """
    seen: list[object] = []
    asked = anyio.Event()

    async def watched(*_args: object, **_kwargs: object) -> object:
        seen.append(sys.stderr)
        sys.stderr.write("a line from the resolver\n")
        asked.set()
        raise EvaluatorUnavailableError("this test opens no second evaluator")

    monkeypatch.setattr(search_module, "_option_trees", watched)
    with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
        cmd = parse(
            [
                "search",
                "--options",
                "--file",
                str(_SYSTEM_NIX),
                "--tui",
                *shared_nix_environment.pynix_store_args(),
            ],
        )

        # **The wait is for the request, and not for a number of seconds.**
        # The command builds the index before it draws anything, and the
        # daemon backend takes seconds over that where the local one takes
        # under one. A fixed wait sent the quit key while the index was still
        # building, so the interface never drew and the pump never ran.
        async def write() -> None:
            await anyio.sleep(_SETTLE)
            pipe.send_text("port")
            with anyio.move_on_after(_ASKED):
                await asked.wait()
            pipe.send_text(_QUIT)

        async with anyio.create_task_group() as tasks:
            tasks.start_soon(cmd.run)
            tasks.start_soon(write)
    assert seen, "the detail pane never asked the resolver for a default"
    assert seen[0] is not sys.stderr
    assert "a line from the resolver" in capfd.readouterr().err


# -- options inside a submodule ------------------------------------------------
#
# `systemd.services.<name>.serviceConfig` was not in the index at all, because
# `lib.collect lib.isOption` stops at `systemd.services`: an
# `attrsOf (submodule ...)` is one option, and the options under it are behind
# `opt.type.getSubOptions`. The fixture module carries the same shape.


def test_an_option_inside_an_attrsof_submodule_is_in_the_index(
    indexed_options: list[OptionRecord],
) -> None:
    """Regression test for the shape of `systemd.services.<name>.serviceConfig`.

    Measured before the walk recursed: not one of the 14 752 options of a real
    `nixosConfigurations` held a `<name>` placeholder.
    """
    names = {record.name for record in indexed_options}
    assert "services.example-daemon.vhosts.<name>.port" in names


def test_a_submodule_inside_a_submodule_is_in_the_index(
    indexed_options: list[OptionRecord],
) -> None:
    """The walk enters a second level, and the depth bound still lets it."""
    names = {record.name for record in indexed_options}
    assert "services.example-daemon.vhosts.<name>.nested.<name>.deep" in names


def test_a_list_of_submodules_names_its_element_with_a_star(
    indexed_options: list[OptionRecord],
) -> None:
    """`listOf` names its element `*`, and `attrsOf` names it `<name>`."""
    names = {record.name for record in indexed_options}
    assert "services.example-daemon.upstreams.*.address" in names


def test_a_sub_option_keeps_its_type_and_its_description(
    indexed_options: list[OptionRecord],
) -> None:
    record = _by_name(indexed_options, "services.example-daemon.vhosts.<name>.port")
    assert record.type.startswith("16 bit unsigned integer")
    assert record.description is not None
    assert "Port this host listens on." in record.description


def test_a_sub_option_whose_default_cannot_be_evaluated_does_not_abort_the_walk(
    indexed_options: list[OptionRecord],
) -> None:
    """The walk must force no `default`, at any depth.

    `brokenSubDefault` is the trap that `brokenDefault` is, one level down. One
    option that throws would end the whole bulk fetch, because the walk returns
    one Nix list that is forced in one pass.
    """
    names = {record.name for record in indexed_options}
    assert "services.example-daemon.vhosts.<name>.brokenSubDefault" in names
    assert "services.example-daemon.vhosts.<name>.port" in names
    assert "services.example-daemon.port" in names


def test_the_plumbing_of_a_submodule_stays_out_of_the_index(
    indexed_options: list[OptionRecord],
) -> None:
    """Every submodule declares four `_module.*` options, and all four are noise.

    They are `internal` below the top level, so the filter this index already
    applies removes them. The recursion needs no filter of its own, and this
    test is what says so.
    """
    nested_plumbing = [
        record.name for record in indexed_options if "_module." in record.name and record.name != "_module.args"
    ]
    assert nested_plumbing == []


def test_a_sub_option_is_searchable(
    indexed_options: list[OptionRecord],
) -> None:
    """The whole point: the interface finds the option, by its own name."""
    ranked = options_tui.rank(indexed_options)("brokenSubDefault")
    assert [record.name for record in ranked] == ["services.example-daemon.vhosts.<name>.brokenSubDefault"]

    ranked = options_tui.rank(indexed_options)("vhosts port")
    assert [record.name for record in ranked] == ["services.example-daemon.vhosts.<name>.port"]


async def test_search_finds_lib_where_the_old_default_could_not(
    shared_nix_environment: NixTestEnvironment,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A module system that gives `pkgs` through `_module.args` needs no flag.

    The default of `--lib-attr` was the literal `pkgs.lib`, which required the
    target to re-export `pkgs` at the top. `nixosSystem` does, and a plain
    `lib.evalModules` does not. The chain reaches both.
    """
    cmd = parse(
        [
            "search",
            "--options",
            "--file",
            str(_TARGET_DIR / "module_args.nix"),
            "--json-output",
            "services.example-daemon.port",
            *shared_nix_environment.pynix_store_args(),
        ],
    )
    await cmd.run()
    captured = capsys.readouterr()
    assert "_module.args.pkgs.lib" in captured.err
    assert _results(captured.out)[0]["name"] == "services.example-daemon.port"


async def test_search_names_what_it_tried_when_there_is_no_options_tree(
    shared_nix_environment: NixTestEnvironment,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A bare package set is a real thing to point at, and holds no options."""
    cmd = parse(
        [
            "search",
            "--options",
            "--file",
            str(_TARGET_DIR / "bare_pkgs.nix"),
            "--json-output",
            "anything",
            *shared_nix_environment.pynix_store_args(),
        ],
    )
    with pytest.raises(SystemExit):
        await cmd.run()
    captured = capsys.readouterr()
    assert "no options tree" in captured.err
    assert "--options-attr" in captured.err


_BOTH_NIX = _FIXTURE_DIR / "both.nix"


@pytest.fixture
def offline_program_index(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Stand in for the channel, and for nothing else.

    `program_index_for` reads `${pkgs.path}/programs.sqlite` when there is one,
    and fetches `nixexprs.tar.xz` when there is not. The fixture package set
    points `path` at real nixpkgs, which carries no database, so an unpatched
    run would reach the network. This replaces that one fetch with a real
    SQLite file of the published shape: the rows below are copied from the
    index, and `openssh` really does install `ssh-keygen`.
    """
    database = tmp_path / "programs.sqlite"
    connection = sqlite3.connect(database)
    with connection:
        connection.execute(
            "create table Programs ("
            " name text not null, system text not null, package text not null,"
            " primary key (name, system, package))"
        )
        connection.executemany(
            "insert into Programs values (?, ?, ?)",
            [
                ("rg", "x86_64-linux", "ripgrep"),
                # A binary that no package of the fixture is named after, and
                # that no `meta.mainProgram` names either. Only this index can
                # reach it, which is the whole reason the index exists.
                ("gpg-agent", "x86_64-linux", "hello-no-main"),
            ],
        )
    connection.close()

    async def _index(
        _session: object,
        _pkgs_path: Path,
        system: str,
        _channel: str = "nixos-unstable",
    ) -> ProgramIndex:
        return ProgramIndex(path=database, system=system, release="26.11", revision="0123456789ab")

    monkeypatch.setattr(search_module, "program_index_for", _index)
    return database


def _search_args(environment: NixTestEnvironment, target: Path, *extra: str) -> list[str]:
    return [
        "search",
        "--file",
        str(target),
        "--system",
        "x86_64-linux",
        "--no-tui",
        "--json-output",
        *extra,
        *environment.pynix_store_args(),
    ]


async def test_the_default_searches_both_indexes(
    shared_nix_environment: NixTestEnvironment,
    offline_program_index: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """No flag means whichever of the two the target offers.

    The query names a package and an option at once, so a merged answer holds
    both and a single-index answer cannot.
    """
    del offline_program_index
    cmd = parse(_search_args(shared_nix_environment, _BOTH_NIX, "ripgrep"))
    await cmd.run()
    kinds = {row["kind"] for row in _results(capsys.readouterr().out)}
    assert kinds == {"package"}


async def test_a_query_reaches_an_option_and_a_package_in_one_list(
    shared_nix_environment: NixTestEnvironment,
    offline_program_index: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    del offline_program_index
    cmd = parse(_search_args(shared_nix_environment, _BOTH_NIX, "--limit", "200", "e"))
    await cmd.run()
    kinds = {row["kind"] for row in _results(capsys.readouterr().out)}
    assert kinds == {"option", "package"}


async def test_a_channel_it_cannot_reach_still_leaves_the_packages_searchable(
    shared_nix_environment: NixTestEnvironment,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A binaries index that cannot be had must cost the binaries alone.

    Regression test. `programs.sqlite` ships in the channel expressions and
    in no git checkout, so a target that is a flake input or a checkout has
    to download a channel to get one. The whole search died when that
    download could not happen: measured in a build sandbox, a search of a
    module-system fixture raised `FileNotFoundError: the channel expressions
    hold no programs.sqlite` and lost the packages it had already walked.

    The walk is the answer, and the binaries only add "which package
    installs this program".
    """

    async def _unreachable(
        _session: object,
        _pkgs_path: Path,
        _system: str,
        _channel: str = "nixos-unstable",
    ) -> ProgramIndex:
        raise FileNotFoundError("the channel expressions hold no programs.sqlite at /nix/store/probe")

    monkeypatch.setattr(search_module, "program_index_for", _unreachable)

    cmd = parse(_search_args(shared_nix_environment, _BOTH_NIX, "--packages", "--limit", "50", "hello"))
    await cmd.run()
    captured = capsys.readouterr()

    # A package row is keyed by `attr`, and an option row by `name`.
    names = {str(result["attr"]) for result in _results(captured.out)}
    assert names, "the search answered nothing at all"
    assert any("hello" in name for name in names)
    assert "and no binaries" in captured.err, "the search did not say that the binaries are missing"


async def test_a_missing_binaries_index_does_not_rebuild_on_every_search(
    shared_nix_environment: NixTestEnvironment,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The cache is complete without the binaries, so the second run is warm.

    Without this the cache would count as incomplete for ever, and every
    search would walk the package set and try the download again.
    """
    attempts = 0

    async def _unreachable(
        _session: object,
        _pkgs_path: Path,
        _system: str,
        _channel: str = "nixos-unstable",
    ) -> ProgramIndex:
        nonlocal attempts
        attempts += 1
        raise FileNotFoundError("no channel here")

    monkeypatch.setattr(search_module, "program_index_for", _unreachable)

    args = _search_args(shared_nix_environment, _BOTH_NIX, "--packages", "hello")
    await parse(args).run()
    capsys.readouterr()
    assert attempts == 1

    # A store URI that cannot work, so a second walk would fail outright.
    await parse([*args, "--store", "local://?root=/nonexistent-store-root"]).run()
    captured = capsys.readouterr()
    assert attempts == 1, "the second search tried the download again"
    assert _results(captured.out), "the second search answered nothing"


async def test_the_options_flag_leaves_the_packages_out(
    shared_nix_environment: NixTestEnvironment,
    offline_program_index: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    del offline_program_index
    cmd = parse(_search_args(shared_nix_environment, _BOTH_NIX, "--options", "--limit", "200", "e"))
    await cmd.run()
    assert {row["kind"] for row in _results(capsys.readouterr().out)} == {"option"}


async def test_the_packages_flag_leaves_the_options_out(
    shared_nix_environment: NixTestEnvironment,
    offline_program_index: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    del offline_program_index
    cmd = parse(_search_args(shared_nix_environment, _BOTH_NIX, "--packages", "--limit", "200", "e"))
    await cmd.run()
    assert {row["kind"] for row in _results(capsys.readouterr().out)} == {"package"}


async def test_a_binary_finds_the_package_that_installs_it(
    shared_nix_environment: NixTestEnvironment,
    offline_program_index: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The question `meta.mainProgram` cannot answer, through the command.

    `hello-no-main` names no main program at all, and nothing in the fixture
    is called `gpg-agent`. So the binary index is the only thing that can
    reach it, and a hit proves that the join reached the command.
    """
    del offline_program_index
    cmd = parse(_search_args(shared_nix_environment, _BOTH_NIX, "gpg-agent"))
    await cmd.run()
    rows = _results(capsys.readouterr().out)
    assert rows[0]["attr"] == "hello-no-main"
    binaries = rows[0]["binaries"]
    assert isinstance(binaries, list)
    assert "gpg-agent" in cast("list[object]", binaries)


async def test_the_packages_flag_on_a_target_with_no_package_set_names_what_it_tried(
    shared_nix_environment: NixTestEnvironment,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A flag asks for something, so a target that lacks it is an error."""
    cmd = parse(_search_args(shared_nix_environment, _FIXTURE_DIR / "options_only.nix", "--packages", "anything"))
    with pytest.raises(SystemExit):
        await cmd.run()
    printed = capsys.readouterr().err
    assert "no package set" in printed
    assert "--pkgs-attr" in printed


async def test_the_default_on_a_target_with_no_package_set_still_searches_options(
    shared_nix_environment: NixTestEnvironment,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """No flag asks for whatever is there, so the other half answers alone."""
    cmd = parse(_search_args(shared_nix_environment, _FIXTURE_DIR / "options_only.nix", "port"))
    await cmd.run()
    rows = _results(capsys.readouterr().out)
    assert {row["kind"] for row in rows} == {"option"}


# ── A cache that names a store path which the store no longer has ────


def _stderr(capsys: pytest.CaptureFixture[str]) -> str:
    """Everything on stderr, as one line.

    `error_console` is a rich `Console`, so it wraps to the terminal width and
    a phrase of two words can arrive with a newline inside it. A test that
    reads the text must not depend on how wide the terminal was.
    """
    return " ".join(capsys.readouterr().err.split())


_TARGET = EvaluationTarget.from_command(parse(["search", "--file", str(_SYSTEM_NIX), "--no-tui"]))


def _search_command(*extra: str) -> Search:
    command = parse(["search", "--file", str(_SYSTEM_NIX), "--no-tui", *extra])
    if not isinstance(command, Search):
        raise TypeError("expected the parser to build a Search command")
    return command


def test_a_vanished_binaries_index_does_not_end_the_command(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`programs_db` is a store path, and nothing roots it.

    The walk of nixpkgs lives under `~/.cache/pynix/packages/` and survives
    anything. This one names `/nix/store/...`, so a `nix store gc` between two
    searches deletes it while the cache still says it is there. SQLite then
    raised `unable to open database file` out of `_read`, with no handler
    above it, and a stale cache crashed the whole command.
    """
    gone = tmp_path / "gone" / "programs.sqlite"
    cached = search_module.Cached(programs_db=str(gone))

    binaries = search_module._binaries(_search_command(), cached)

    assert binaries == {}
    printed = _stderr(capsys)
    assert "--update-index" in printed, "the reader has to learn how to get it back"


def test_a_binaries_index_that_is_not_a_database_does_not_end_the_command(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A file that exists and is not SQLite fails at the query and not the open."""
    rubbish = tmp_path / "programs.sqlite"
    rubbish.write_text("this is not a database")
    cached = search_module.Cached(programs_db=str(rubbish))

    assert search_module._binaries(_search_command(), cached) == {}
    assert "--update-index" in _stderr(capsys)


def test_no_binaries_index_is_silent(capsys: pytest.CaptureFixture[str]) -> None:
    """A cache that never held one is complete, so it says nothing."""
    assert search_module._binaries(_search_command(), search_module.Cached()) == {}
    assert _stderr(capsys) == ""


def test_a_readable_index_still_answers(tmp_path: Path) -> None:
    """The degradation must not swallow the working case."""
    database = tmp_path / "programs.sqlite"
    with contextlib.closing(sqlite3.connect(database)) as connection:
        connection.execute("create table Programs (package text, name text, system text)")
        connection.execute(
            "insert into Programs values ('ripgrep', 'rg', ?)", (search_module._system(_search_command()),)
        )
        connection.commit()
    cached = search_module.Cached(programs_db=str(database))

    assert search_module._binaries(_search_command(), cached) == {"ripgrep": ["rg"]}


async def test_a_present_index_opens_no_evaluator(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The miss pays for the fetch, and the hit pays one `exists`."""
    database = tmp_path / "programs.sqlite"
    database.write_text("")
    cached = search_module.Cached(programs_db=str(database))

    def _no_session(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise AssertionError("a path that is there must not open an evaluator")

    monkeypatch.setattr(search_module, "eval_session", _no_session)

    result = await search_module._ensure_binaries_index(_search_command(), tmp_path / "cache.json", _TARGET, cached)

    assert result.programs_db == str(database)


async def test_no_index_opens_no_evaluator(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A cache that never held one has nothing to put back."""

    def _no_session(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise AssertionError("nothing to fetch")

    monkeypatch.setattr(search_module, "eval_session", _no_session)

    result = await search_module._ensure_binaries_index(
        _search_command(), tmp_path / "cache.json", _TARGET, search_module.Cached()
    )

    assert result.programs_db is None


async def test_a_failed_refetch_leaves_the_search_running(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A machine with no network still answers with every package it walked."""
    cached = search_module.Cached(programs_db=str(tmp_path / "gone" / "programs.sqlite"), pkgs_path="/nix/store/x")

    def _fails(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise OSError("no network")

    monkeypatch.setattr(search_module, "eval_session", _fails)

    result = await search_module._ensure_binaries_index(_search_command(), tmp_path / "cache.json", _TARGET, cached)

    assert result is cached, "it must not lose the walk"
    assert "finds nothing" in _stderr(capsys)
