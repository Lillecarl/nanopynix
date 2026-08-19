# pyright: reportUnknownMemberType=false
# pytest.Pytester's makepyfile is typed as (*args, **kwargs) -> Path -- untyped
# varargs, not a stub gap specific to this file.

"""Extra introspection output: `note()`, `attach()`, and where they end up.

Notes exist to answer a question in the run that asked it, so most of these
assert on the *terminal* output as much as on what lands on disk.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import conftest
import pytest
from pytest_agent._capture import MAX_NOTE_CHARS

if TYPE_CHECKING:
    from pathlib import Path


def _run(pytester: pytest.Pytester, *extra: str) -> pytest.RunResult:
    return pytester.runpytest_subprocess(*conftest.agent_plugin_cli_args(), "--agent", "-q", *extra)


def _records(pytester: pytest.Pytester, run: int = 1) -> dict[str, dict[str, Any]]:
    index = pytester.path / ".pytest-agent" / f"runs-{run:04d}" / "index.jsonl"
    records = [json.loads(line) for line in index.read_text(encoding="utf-8").splitlines() if line.strip()]
    return {record["nodeid"]: record for record in records}


def test_a_note_is_echoed_in_the_summary_so_reading_it_costs_no_second_turn(pytester: pytest.Pytester) -> None:
    pytester.makepyfile(
        test_probe="""
        def test_probe(agent_notes):
            agent_notes.note(backend="daemon")
        """,
    )

    result = _run(pytester)

    result.stdout.fnmatch_lines(["*notes:*", "*test_probe.py::test_probe  backend=daemon*"])


def test_notes_reach_the_index_the_record_and_the_log(pytester: pytest.Pytester) -> None:
    pytester.makepyfile(
        test_probe="""
        def test_probe(agent_notes):
            agent_notes.note(backend="daemon", paths=["/nix/store/a", "/nix/store/b"])
        """,
    )

    _run(pytester)

    record = _records(pytester)["test_probe.py::test_probe"]
    # Structured, so a whole run can be sliced by it -- the thing log text
    # could never support.
    assert record["notes"] == {"backend": "daemon", "paths": ["/nix/store/a", "/nix/store/b"]}
    log = (pytester.path / ".pytest-agent" / "runs-0001" / record["log_file"]).read_text(encoding="utf-8")
    assert "=== NOTES ===" in log
    assert 'backend=daemon\npaths=["/nix/store/a", "/nix/store/b"]' in log


def test_a_note_survives_the_process_dying_mid_test(pytester: pytest.Pytester) -> None:
    # The whole reason to add a probe is that something is going wrong, and
    # what goes wrong is not always a tidy exception. Nothing here gets a
    # teardown report, a log file, or an index record -- only notes.jsonl,
    # written as the note was taken, is left.
    pytester.makepyfile(
        test_crash="""
        import os

        from pytest_agent import note


        def test_dies_hard():
            note(got_to="just before the bad call")
            os._exit(1)
        """,
    )

    _run(pytester)

    run_dir = pytester.path / ".pytest-agent" / "runs-0001"
    assert not (run_dir / "test_crash.py").exists(), "expected no per-test log for a test that never finished"
    notes = [json.loads(line) for line in (run_dir / "notes.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [(note["nodeid"], note["key"], note["value"]) for note in notes] == [
        ("test_crash.py::test_dies_hard", "got_to", "just before the bad call"),
    ]


def test_note_works_from_inside_the_code_under_test_with_no_fixture(pytester: pytest.Pytester) -> None:
    # The case the fixture cannot serve: a probe several frames down, where
    # threading a fixture through every caller is a change nobody wants to
    # make and then unmake.
    pytester.makepyfile(
        library="""
        from pytest_agent import note


        def resolve(digest):
            note(resolving=digest)
            return digest
        """,
    )
    pytester.makepyfile(
        test_library="""
        import library


        def test_resolves():
            library.resolve("0lqkw72fqnp7q1kr")
        """,
    )

    result = _run(pytester)

    assert _records(pytester)["test_library.py::test_resolves"]["notes"] == {"resolving": "0lqkw72fqnp7q1kr"}
    result.stdout.fnmatch_lines(["*resolving=0lqkw72fqnp7q1kr*"])


def test_a_repeated_key_collapses_to_where_it_got_to_but_keeps_every_value(pytester: pytest.Pytester) -> None:
    pytester.makepyfile(
        test_loop="""
        def test_loop(agent_notes):
            for i in range(3):
                agent_notes.note(iteration=i)
        """,
    )

    result = _run(pytester)

    # The summary and the record answer "how far did it get".
    result.stdout.fnmatch_lines(["*test_loop.py::test_loop  iteration=2*"])
    assert _records(pytester)["test_loop.py::test_loop"]["notes"] == {"iteration": 2}
    # notes.jsonl keeps the history the collapse dropped.
    notes_path = pytester.path / ".pytest-agent" / "runs-0001" / "notes.jsonl"
    values = [json.loads(line)["value"] for line in notes_path.read_text(encoding="utf-8").splitlines()]
    assert values == [0, 1, 2]


def test_an_unserializable_value_is_recorded_not_raised(pytester: pytest.Pytester) -> None:
    # A probe must never be the thing that breaks the test it was added to.
    pytester.makepyfile(
        test_object="""
        class Opaque:
            def __repr__(self):
                return "<Opaque backend=daemon>"


        def test_object(agent_notes):
            agent_notes.note(thing=Opaque())
        """,
    )

    result = _run(pytester)

    assert result.ret == pytest.ExitCode.OK
    assert _records(pytester)["test_object.py::test_object"]["notes"] == {"thing": "<Opaque backend=daemon>"}


def test_a_huge_value_is_clipped_everywhere_but_kept_whole_in_notes_jsonl(pytester: pytest.Pytester) -> None:
    pytester.makepyfile(
        test_big=f"""
        def test_big(agent_notes):
            agent_notes.note(blob="x" * {MAX_NOTE_CHARS * 2})
        """,
    )

    result = _run(pytester)

    clipped = _records(pytester)["test_big.py::test_big"]["notes"]["blob"]
    assert clipped.startswith("x" * MAX_NOTE_CHARS)
    assert f"truncated {MAX_NOTE_CHARS} chars" in clipped
    assert "use attach()" in clipped
    result.stdout.fnmatch_lines(["*truncated*chars*"])
    notes_path = pytester.path / ".pytest-agent" / "runs-0001" / "notes.jsonl"
    assert json.loads(notes_path.read_text(encoding="utf-8"))["value"] == "x" * (MAX_NOTE_CHARS * 2)


def test_attachments_are_listed_with_a_path_that_can_be_read_back(pytester: pytest.Pytester) -> None:
    pytester.makepyfile(
        test_attach="""
        def test_attach(agent_notes):
            agent_notes.attach("payload.json", '{"a": 1}')
            (agent_notes.dir / "written-directly.txt").write_text("straight into the directory")
        """,
    )

    result = _run(pytester)

    record = _records(pytester)["test_attach.py::test_attach"]
    # Listed by looking at the directory, so a file the test wrote there
    # itself counts too -- a subprocess's output, say.
    assert record["attachments"] == [
        "test_attach.py/test_attach.files/payload.json",
        "test_attach.py/test_attach.files/written-directly.txt",
    ]
    run_dir = pytester.path / ".pytest-agent" / "runs-0001"
    assert (run_dir / record["attachments"][0]).read_text(encoding="utf-8") == '{"a": 1}'
    result.stdout.fnmatch_lines(["*attached: *test_attach.files/payload.json*"])


def test_an_attachment_name_cannot_escape_the_test_s_directory(pytester: pytest.Pytester) -> None:
    pytester.makepyfile(
        test_escape="""
        import pytest


        def test_escape(agent_notes):
            for name in ("../../escaped.txt", "..", "/etc/passwd", ""):
                with pytest.raises(ValueError, match="not usable as a file name"):
                    agent_notes.attach(name, "nope")
        """,
    )

    result = _run(pytester)

    assert result.ret == pytest.ExitCode.OK
    assert not (pytester.path / ".pytest-agent" / "escaped.txt").exists()


def test_attachments_in_a_subdirectory_keep_their_own_names(pytester: pytest.Pytester) -> None:
    # Flattening "logs/worker-1.log" to "worker-1.log" would be fine until
    # the second worker's log silently overwrote the first.
    pytester.makepyfile(
        test_nested="""
        def test_nested(agent_notes):
            agent_notes.attach("logs/worker-1.log", "first")
            agent_notes.attach("logs/worker-2.log", "second")
        """,
    )

    _run(pytester)

    assert _records(pytester)["test_nested.py::test_nested"]["attachments"] == [
        "test_nested.py/test_nested.files/logs/worker-1.log",
        "test_nested.py/test_nested.files/logs/worker-2.log",
    ]


def test_a_test_that_runs_twice_in_one_session_does_not_inherit_its_earlier_notes(
    pytester: pytest.Pytester,
) -> None:
    # A rerun plugin (or --lf in the same process) finalizes the same nodeid
    # more than once. The second record must describe the second run.
    pytester.makeconftest(
        """
        def pytest_collection_modifyitems(items):
            items.extend(list(items))
        """,
    )
    pytester.makepyfile(
        test_twice="""
        from pytest_agent import note

        runs = []


        def test_twice():
            runs.append(len(runs))
            note(run_number=runs[-1])
        """,
    )

    _run(pytester)

    run_dir = pytester.path / ".pytest-agent" / "runs-0001"
    records = [
        json.loads(line) for line in (run_dir / "index.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()
    ]
    assert [record["notes"] for record in records] == [{"run_number": 0}, {"run_number": 1}]
    # The record collapses per key, so it would read the same either way --
    # the log lists every note, and is where inherited ones would show up.
    log = (run_dir / records[-1]["log_file"]).read_text(encoding="utf-8")
    assert "=== NOTES ===\nrun_number=1\n" in log


def test_a_session_fixture_s_teardown_note_lands_on_the_test_it_ran_under(pytester: pytest.Pytester) -> None:
    # pytest finalizes session-scoped fixtures inside the last test's
    # teardown phase, so this is not a note with nowhere to go: it is
    # genuinely recorded while that test was still being torn down, and
    # filing it there is the accurate answer.
    pytester.makeconftest(
        """
        import pytest

        from pytest_agent import note


        @pytest.fixture(scope="session", autouse=True)
        def _probe_session():
            yield
            note(torn_down="the session fixture")
        """,
    )
    pytester.makepyfile(test_trivial="def test_trivial():\n    assert True\n")

    result = _run(pytester)

    result.stdout.fnmatch_lines(["*test_trivial.py::test_trivial  torn_down=the session fixture*"])
    assert _records(pytester)["test_trivial.py::test_trivial"]["notes"] == {"torn_down": "the session fixture"}


def test_a_note_with_genuinely_no_test_running_is_still_kept_and_shown(pytester: pytest.Pytester) -> None:
    pytester.makeconftest(
        """
        from pytest_agent import note


        def pytest_collection_modifyitems(items):
            note(collected=len(items))
        """,
    )
    pytester.makepyfile(test_trivial="def test_trivial():\n    assert True\n")

    result = _run(pytester)

    result.stdout.fnmatch_lines(["*(outside any test)  collected=1*"])
    notes_path = pytester.path / ".pytest-agent" / "runs-0001" / "notes.jsonl"
    assert json.loads(notes_path.read_text(encoding="utf-8"))["nodeid"] is None


def test_the_notes_block_is_capped_so_a_probe_in_a_loop_cannot_bury_the_failures(
    pytester: pytest.Pytester,
) -> None:
    pytester.makepyfile(
        test_many="""
        import pytest

        from pytest_agent import note


        @pytest.mark.parametrize("case", range(80))
        def test_many(case):
            note(case=case)


        def test_the_failure_that_must_stay_visible():
            assert False
        """,
    )

    result = _run(pytester)

    result.stdout.fnmatch_lines(["*test_the_failure_that_must_stay_visible.log*", "*notes:*", "*more lines: *"])
    printed = [line for line in result.outlines if "case=" in line]
    assert len(printed) < 80


def test_with_agent_mode_off_a_note_prints_instead_of_vanishing(pytester: pytest.Pytester, tmp_path: Path) -> None:
    del tmp_path
    pytester.makepyfile(
        test_plain="""
        from pytest_agent import note


        def test_plain():
            note(backend="daemon")
            assert False
        """,
    )

    # No --agent: nothing is recording, so the note has to fall back to
    # pytest's own captured output rather than disappear.
    result = pytester.runpytest_subprocess(*conftest.agent_plugin_cli_args(), "-q")

    assert result.ret == pytest.ExitCode.TESTS_FAILED
    result.stdout.fnmatch_lines(["*[[]pytest-agent[]] note backend=daemon*"])


def test_the_notes_block_honours_a_cap_from_the_command_line(pytester: pytest.Pytester) -> None:
    """`--agent-max-note-lines` reaches the inline cap.

    The block already knew it had a limit and already wrote the whole thing
    to `notes.jsonl`; the constant behind it was reachable from nowhere.
    `--agent-max-summary-lines` governs exactly this trade for the end-of-run
    report of another plugin, and this one made it with a number nobody could
    name. Issue #46.
    """
    pytester.makepyfile(
        test_many_notes="""
        from pytest_agent import note

        def test_one():
            for index in range(12):
                note(**{f"key_{index}": index})
        """
    )
    result = _run(pytester, "--agent-max-note-lines=3")
    # Three lines of the block, and the nodeid that heads it is one of them,
    # so two values follow it. The cap counts what the terminal takes rather
    # than how many notes there were.
    printed = [line for line in result.outlines if "key_" in line]
    assert len(printed) == 2, printed
    assert any("more lines" in line for line in result.outlines)


def test_a_cap_of_zero_leaves_the_notes_to_their_file(pytester: pytest.Pytester) -> None:
    """A probe inside a loop over 300 tests must not bury the failure list."""
    pytester.makepyfile(
        test_many_notes="""
        from pytest_agent import note

        def test_one():
            for index in range(12):
                note(**{f"key_{index}": index})
        """
    )
    result = _run(pytester, "--agent-max-note-lines=0")
    assert not [line for line in result.outlines if "key_" in line]
    assert any("notes.jsonl" in line for line in result.outlines)
