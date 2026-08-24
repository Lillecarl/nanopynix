from __future__ import annotations

import json
import re
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple, cast

import pytest
from prompt_toolkit.application import create_app_session
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

import pynix._impl.osearch as osearch_module
from pynix import parse
from pynix._impl import osearch_tui
from pynix._impl._search_tui import SearchTui
from pynix._options import OptionRecord
from pynix.osearch import Osearch

if TYPE_CHECKING:
    from prompt_toolkit.formatted_text import StyleAndTextTuples

    from nanopynix_testing.nix_environment import NixTestEnvironment

_FIXTURE_DIR = Path(__file__).parent / "test_osearch"
_SYSTEM_NIX = _FIXTURE_DIR / "system.nix"


def _parse_json_output(out: str) -> object:
    """Extract the JSON portion from captured stdout, skipping structlog lines."""
    _structlog = re.compile(r"^\d{4}-\d{2}-\d{2}\s")
    lines = [line for line in out.splitlines() if not _structlog.match(line)]
    return json.loads("".join(lines))


def _results(out: str) -> list[dict[str, object]]:
    """Parse an ``osearch --json`` result array into typed records."""
    data = _parse_json_output(out)
    if not isinstance(data, list):
        raise TypeError("expected osearch --json to print a JSON array")
    records: list[dict[str, object]] = []
    for entry in cast("list[object]", data):
        if not isinstance(entry, dict):
            raise TypeError("expected each osearch result to be a JSON object")
        records.append(cast("dict[str, object]", entry))
    return records


@pytest.fixture(autouse=True)
def cache_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point osearch cache at a fresh per-test directory so tests don't share a cache."""
    cache_home = tmp_path / "cache"
    osearch_dir = cache_home / "pynix" / "osearch"
    osearch_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(osearch_module, "_cache_dir", lambda: osearch_dir)
    return cache_home


async def test_osearch_builds_index_and_finds_a_match(
    shared_nix_environment: NixTestEnvironment,
    cache_home: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cmd = parse(
        [
            "osearch",
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

    cache_files = list((cache_home / "pynix" / "osearch").glob("*.json"))
    assert len(cache_files) == 1


async def test_osearch_survives_an_option_whose_default_cannot_be_evaluated(
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
            "osearch",
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


async def test_osearch_filters_out_internal_options(
    shared_nix_environment: NixTestEnvironment,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cmd = parse(
        [
            "osearch",
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


async def test_osearch_second_run_hits_the_cache_without_a_working_store(
    shared_nix_environment: NixTestEnvironment,
    capsys: pytest.CaptureFixture[str],
) -> None:
    build_cmd = parse(["osearch", "--file", str(_SYSTEM_NIX), *shared_nix_environment.pynix_store_args()])
    await build_cmd.run()
    capsys.readouterr()

    # A bogus store URI would make evaluation fail if the cache were bypassed.
    cached_cmd = parse(
        [
            "osearch",
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


async def test_osearch_update_index_rebuilds_the_cache(
    shared_nix_environment: NixTestEnvironment,
    capsys: pytest.CaptureFixture[str],
) -> None:
    build_cmd = parse(["osearch", "--file", str(_SYSTEM_NIX), *shared_nix_environment.pynix_store_args()])
    await build_cmd.run()
    capsys.readouterr()

    rebuild_cmd = parse(
        ["osearch", "--file", str(_SYSTEM_NIX), "--update-index", *shared_nix_environment.pynix_store_args()],
    )
    await rebuild_cmd.run()
    captured = capsys.readouterr()
    assert "indexed" in captured.err


async def test_osearch_limit_truncates_results(
    shared_nix_environment: NixTestEnvironment,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cmd = parse(
        [
            "osearch",
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
# `osearch --tui` is a second reader of the same cached index. These tests build
# that index from the real module fixture, so the ranking and the detail pane
# run against options that `lib.evalModules` produced, and not against a double.


class _ModeCase(NamedTuple):
    """One row of the table that decides which mode `osearch` runs."""

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
    monkeypatch.setattr(osearch_module, "human_at_terminal", lambda: case.human)
    argv = ["osearch", "--file", str(_SYSTEM_NIX)]
    if case.tui is True:
        argv.append("--tui")
    elif case.tui is False:
        argv.append("--no-tui")
    if case.json_output:
        argv.append("--json-output")
    if case.query is not None:
        argv.append(case.query)
    command = parse(argv)
    if not isinstance(command, Osearch):
        raise TypeError("expected the parser to build an Osearch command")
    assert osearch_module._use_tui(command) is case.expected


@pytest.fixture
async def indexed_options(
    shared_nix_environment: NixTestEnvironment,
    cache_home: Path,
) -> list[OptionRecord]:
    """Every option of the fixture module, indexed by a real evaluation."""
    cmd = parse(
        ["osearch", "--file", str(_SYSTEM_NIX), "--no-tui", *shared_nix_environment.pynix_store_args()],
    )
    await cmd.run()
    (cache_file,) = (cache_home / "pynix" / "osearch").glob("*.json")
    return osearch_module._load_cache(cache_file)


def _by_name(records: list[OptionRecord], name: str) -> OptionRecord:
    return next(record for record in records if record.name == name)


def test_the_ranking_puts_the_options_in_order_for_an_empty_query(
    indexed_options: list[OptionRecord],
) -> None:
    """An empty query has nothing to rank against, so the names are sorted."""
    ranked = osearch_tui.rank(indexed_options)("")
    names = [record.name for record in ranked]
    assert names == sorted(names)
    assert len(names) == len(indexed_options)


def test_the_ranking_finds_an_option_by_a_part_of_its_name(
    indexed_options: list[OptionRecord],
) -> None:
    ranked = osearch_tui.rank(indexed_options)("configFiles")
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
    ranked = osearch_tui.rank(indexed_options)("configFiles")
    assert len(ranked) < len(indexed_options)
    assert all("onfig" in record.name for record in ranked)


def _named(*names: str) -> list[OptionRecord]:
    return [OptionRecord(name=name, type="bool", description=None, declarations=[], read_only=False) for name in names]


def test_the_ranking_finds_a_short_query_inside_a_long_option_name() -> None:
    """A caller who is still typing gives a query far shorter than the name."""
    long_name = "services.nginx.virtualHosts.<name>.locations.<name>.proxyWebsockets"
    ranked = osearch_tui.rank(_named(long_name, "boot.loader.grub.enable"))("websock")
    assert [record.name for record in ranked] == [long_name]


def test_the_ranking_ignores_the_case_of_the_query() -> None:
    assert len(osearch_tui.rank(_named("services.a.proxyWebsockets"))("WEBSOCKETS")) == 1


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
    rank_query = osearch_tui.rank(records)
    counts = [len(rank_query(query)) for query in ("v", "vs", "vsc", "vsco", "vscod", "vscode")]
    assert counts == sorted(counts, reverse=True), counts


def test_every_word_of_the_query_must_appear() -> None:
    """Two words narrow, which one long word cannot do."""
    records = _named(
        "programs.vscode.enable",
        "programs.vscode.package",
        "services.openssh.enable",
    )
    rank_query = osearch_tui.rank(records)
    assert len(rank_query("vscode")) == 2
    assert [record.name for record in rank_query("vscode enable")] == ["programs.vscode.enable"]


def test_the_shorter_and_more_specific_option_ranks_first() -> None:
    records = _named(
        "programs.vscode.profiles.<name>.userSettings.editorAssociations",
        "programs.vscode.enable",
    )
    ranked = osearch_tui.rank(records)("vscode")
    assert ranked[0].name == "programs.vscode.enable"


def test_a_typo_falls_back_to_a_near_match() -> None:
    """An empty screen answers nothing, so a query no name holds still ranks.

    `vscodee` appears in no option name. The fallback is what keeps the
    interface useful while the caller corrects the spelling.
    """
    records = _named("programs.vscode.enable", "networking.firewall.allowPing")
    ranked = osearch_tui.rank(records)("vscodee")
    assert [record.name for record in ranked] == ["programs.vscode.enable"]


def test_the_ranking_finds_nothing_for_a_query_that_matches_nothing(
    indexed_options: list[OptionRecord],
) -> None:
    assert osearch_tui.rank(indexed_options)("zzzzzzzz") == []


def test_the_detail_pane_renders_a_real_myst_description(
    indexed_options: list[OptionRecord],
) -> None:
    """The fixture option carries paragraphs, a code fence and a colon fence.

    `pynix._markdown` is the renderer, and this is the only test that drives it
    over prose that the module system produced rather than a literal string.
    """
    record = _by_name(indexed_options, "services.example-daemon.configFiles")
    text = "".join(fragment[1] for fragment in osearch_tui.detail(record, 70))

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
    narrow = _lines(osearch_tui.detail(record, 40))
    wide = _lines(osearch_tui.detail(record, 100))
    assert narrow > wide


def _lines(fragments: StyleAndTextTuples) -> int:
    return len("".join(fragment[1] for fragment in fragments).splitlines())


def test_the_detail_pane_marks_a_read_only_option(
    indexed_options: list[OptionRecord],
) -> None:
    record = _by_name(indexed_options, "services.example-daemon.stateVersion")
    assert record.read_only is True
    text = "".join(fragment[1] for fragment in osearch_tui.detail(record, 70))
    assert "read only" in text

    port = _by_name(indexed_options, "services.example-daemon.port")
    assert port.read_only is False
    assert "read only" not in "".join(fragment[1] for fragment in osearch_tui.detail(port, 70))


def test_the_detail_pane_names_the_file_that_declares_the_option(
    indexed_options: list[OptionRecord],
) -> None:
    record = _by_name(indexed_options, "services.example-daemon.port")
    text = "".join(fragment[1] for fragment in osearch_tui.detail(record, 70))
    assert "declared in" in text
    assert "module.nix" in text


async def test_the_interface_narrows_the_real_index_as_the_caller_types(
    indexed_options: list[OptionRecord],
) -> None:
    """Drive the real application over a pipe, against the real index."""
    source = osearch_tui.source(indexed_options, subject=str(_SYSTEM_NIX))
    with create_pipe_input() as pipe:
        tui = SearchTui(source, input=pipe, output=DummyOutput())
        pipe.send_text("configFiles\x03")
        with create_app_session(input=pipe, output=DummyOutput()):
            await tui.application.run_async()

    assert tui.query == "configFiles"
    assert tui.selection is not None
    assert tui.selection.name == "services.example-daemon.configFiles"
    assert "option" in "".join(fragment[1] for fragment in tui.footer_fragments())


async def test_the_interface_opens_on_the_query_of_the_command_line(
    indexed_options: list[OptionRecord],
) -> None:
    """`osearch --tui <query>` puts that query in the search bar."""
    source = osearch_tui.source(indexed_options, subject=str(_SYSTEM_NIX))
    with create_pipe_input() as pipe:
        tui = SearchTui(source, initial_query="stateVersion", input=pipe, output=DummyOutput())
        pipe.send_text("\x03")
        with create_app_session(input=pipe, output=DummyOutput()):
            await tui.application.run_async()

    assert tui.query == "stateVersion"
    assert tui.selection is not None
    assert tui.selection.name == "services.example-daemon.stateVersion"


async def test_the_command_opens_the_interface_inside_its_event_loop(
    shared_nix_environment: NixTestEnvironment,
) -> None:
    """`osearch --tui` runs the interface from inside the loop of the command.

    Regression test. `SearchTui.run` called `Application.run`, which calls
    `asyncio.run`, and every `pynix` command already runs an event loop. The
    command raised "asyncio.run() cannot be called from a running event loop"
    the moment it drew, and no test above catches that: each one awaits the
    application itself rather than going through the command.

    `create_app_session` is what makes this work with no terminal. The
    application takes no input of its own here, exactly as the command builds
    it, so it reads the input of the session.
    """
    with create_pipe_input() as pipe:
        pipe.send_text("port\x03")
        with create_app_session(input=pipe, output=DummyOutput()):
            cmd = parse(
                [
                    "osearch",
                    "--file",
                    str(_SYSTEM_NIX),
                    "--tui",
                    *shared_nix_environment.pynix_store_args(),
                ],
            )
            await cmd.run()


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
    ranked = osearch_tui.rank(indexed_options)("brokenSubDefault")
    assert [record.name for record in ranked] == ["services.example-daemon.vhosts.<name>.brokenSubDefault"]

    ranked = osearch_tui.rank(indexed_options)("vhosts port")
    assert [record.name for record in ranked] == ["services.example-daemon.vhosts.<name>.port"]
