# pyright: reportUnknownMemberType=false
# pytest.Pytester's makepyfile is typed as (*args, **kwargs) -> Path -- untyped
# varargs, not a stub gap specific to this file.

"""The whole loop an agent actually runs: fail a suite, then query it.

Everything here goes through real processes -- an inner ``pytest --agent``
run, then real ``pytest-agent`` CLI invocations against what it wrote. The
scenario is modelled on the debugging session that motivated these commands:
several parametrized tests failing on one shared root cause (with the varying
part of the message embedded in it), plus one unrelated failure that must not
be swept into the same group.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from typing import TYPE_CHECKING

import conftest
import pytest

if TYPE_CHECKING:
    from pathlib import Path

_HELPER = """
import json
from pathlib import Path


def load_schema(digest):
    return json.loads(Path(f"/nix/store/{digest}-swagger.json").read_text())
"""

_TESTS = """
import pytest

import schema_helper


@pytest.mark.parametrize("backend", ["in_process", "daemon"])
@pytest.mark.parametrize("store", ["local", "remote"])
def test_hover_on_a_kind_name(backend, store):
    # A different (nonexistent) store path per parametrization, so the
    # failures share a cause but not a literal message.
    schema_helper.load_schema("0lqkw72fqnp7q1krbmnc48v4h1bqbx" + backend[0] + store[0])


def test_something_unrelated():
    raise ValueError("nothing to do with schemas")


def test_fine():
    assert True
"""


@pytest.fixture
def failed_run(pytester: pytest.Pytester) -> Path:
    """A project directory whose suite has just been run and mostly failed."""
    pytester.makepyfile(schema_helper=_HELPER)
    pytester.makepyfile(test_lsp_scenarios=_TESTS)
    result = pytester.runpytest_subprocess(*conftest.agent_plugin_cli_args(), "--agent", "-q")
    assert result.ret == pytest.ExitCode.TESTS_FAILED
    return pytester.path


def test_digest_collapses_the_shared_cause_and_keeps_the_unrelated_one_apart(failed_run: Path) -> None:
    result = conftest.run_cli(["digest"], cwd=failed_run)

    assert result.returncode == 0, result.stderr
    assert "5 failed/errored of 6 recorded, 2 distinct root causes" in result.stdout
    assert "4x  FileNotFoundError" in result.stdout
    assert "1x  ValueError: nothing to do with schemas" in result.stdout
    # The four parametrizations are named, so the next step is obvious.
    assert result.stdout.count("test_hover_on_a_kind_name[") == 4


def test_digest_points_at_first_party_code_not_the_stdlib_frame_that_raised(failed_run: Path) -> None:
    result = conftest.run_cli(["digest"], cwd=failed_run)

    assert result.returncode == 0, result.stderr
    # Group [1] is the shared FileNotFoundError (groups are ordered by count),
    # and only its frames are of interest here -- group [2] has its own.
    group_one, _, _rest = result.stdout.partition("\n[2] ")
    frames = re.findall(r"^\s+(\S+\.py):\d+$", group_one, flags=re.MULTILINE)
    # The call chain in the project's own code, outermost first, and nothing
    # else: no pytest internals, no stdlib, no site-packages.
    assert frames == ["test_lsp_scenarios.py", "schema_helper.py"]
    # The actual raise site is inside pathlib, kept as a single trailing
    # line rather than passed off as the interesting location.
    raised_in = re.search(r"-> raised in (\S+):\d+$", group_one, flags=re.MULTILINE)
    if raised_in is None:
        raise AssertionError(f"expected a 'raised in' line, got:\n{result.stdout}")
    assert "pathlib" in raised_in.group(1)


def test_show_reads_one_parametrized_test_by_substring(failed_run: Path) -> None:
    result = conftest.run_cli(["show", "remote-daemon"], cwd=failed_run)

    assert result.returncode == 0, result.stderr
    assert "test_hover_on_a_kind_name[remote-daemon]" in result.stdout
    assert "FileNotFoundError" in result.stdout
    # The full traceback, not just the crash line.
    assert "def test_hover_on_a_kind_name" in result.stdout


def test_show_refuses_an_ambiguous_substring_and_names_the_candidates(failed_run: Path) -> None:
    result = conftest.run_cli(["show", "hover"], cwd=failed_run)

    assert result.returncode == 1
    assert "4 tests" in result.stdout
    assert "--all" in result.stdout


def test_last_failures_paths_can_be_read_back_verbatim(failed_run: Path) -> None:
    result = conftest.run_cli(["last-failures"], cwd=failed_run)

    assert result.returncode == 0, result.stderr
    quoted = [line.strip() for line in result.stdout.splitlines() if line.startswith((".pytest-agent", "'"))]
    assert len(quoted) == 5

    # The real test: feed each printed path straight back to a shell. An
    # unquoted `[` would make fish refuse the command outright, and other
    # shells glob it.
    for path in quoted:
        readback = subprocess.run(
            ["sh", "-c", f"cat {path}"],
            cwd=failed_run,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,  # asserted below
        )
        assert readback.returncode == 0, readback.stderr
        assert "outcome: " in readback.stdout


def _recorded_nodeids(project: Path, run_number: int) -> set[str]:
    index_path = project / ".pytest-agent" / f"runs-{run_number:04d}" / "index.jsonl"
    lines = index_path.read_text(encoding="utf-8").splitlines()
    return {json.loads(line)["nodeid"] for line in lines if line.strip()}


def test_rerun_runs_exactly_the_recorded_failures_and_nothing_else(failed_run: Path) -> None:
    result = conftest.run_cli(["rerun"], cwd=failed_run)

    assert result.returncode != 0, "the failures were re-run, so the run must still fail"
    assert "re-running 5 failed from .pytest-agent/runs-0001" in result.stdout

    reran = _recorded_nodeids(failed_run, 2)
    assert len(reran) == 5
    # The passing test is not re-run: that is the whole difference between
    # this and running the suite again.
    assert "test_lsp_scenarios.py::test_fine" not in reran
    # Parametrized ids go straight into pytest's argv, so the brackets that a
    # shell would treat as a glob never need quoting by whoever calls this.
    assert "test_lsp_scenarios.py::test_hover_on_a_kind_name[remote-daemon]" in reran


def test_rerun_forwards_the_rest_of_its_arguments_to_pytest(failed_run: Path) -> None:
    result = conftest.run_cli(["rerun", "-x"], cwd=failed_run)

    assert result.returncode != 0
    # -x reached pytest: it stopped at the first of the five.
    assert len(_recorded_nodeids(failed_run, 2)) == 1


def test_rerun_can_target_an_older_run_than_the_last_one(failed_run: Path) -> None:
    # The thing pytest's own --lf cannot do. Its cache holds only the last run
    # in a rootdir, so the -x re-run below overwrites the list of five -- while
    # runs-0001 still has it, and can be re-run again from there.
    first = conftest.run_cli(["rerun", "-x"], cwd=failed_run)
    assert first.returncode != 0

    result = conftest.run_cli(["rerun", "--run", "1"], cwd=failed_run)

    assert "re-running 5 failed from .pytest-agent/runs-0001" in result.stdout
    assert len(_recorded_nodeids(failed_run, 3)) == 5


def test_rerun_of_a_module_that_failed_to_import_gives_pytest_a_path_it_accepts(
    pytester: pytest.Pytester,
) -> None:
    # A collect error's nodeid is a file path, not a `::` id, and it goes back
    # to pytest as a positional argument like any other. Worth pinning: a bad
    # import is the most ordinary broken state there is, and a rerun that
    # choked on it would fail exactly when it was most wanted.
    pytester.makepyfile(
        test_broken="""
        import nonexistent_module_xyz


        def test_a():
            assert True
        """,
    )
    run = pytester.runpytest_subprocess(*conftest.agent_plugin_cli_args(), "--agent", "-q")
    assert run.ret != pytest.ExitCode.OK

    result = conftest.run_cli(["rerun"], cwd=pytester.path)

    assert "re-running 1 failed" in result.stdout
    assert result.returncode != 0, "the module still doesn't import, so the re-run must fail too"
    assert _recorded_nodeids(pytester.path, 2) == {"test_broken.py"}


def test_rerun_with_nothing_to_do_does_not_quietly_run_the_whole_suite(pytester: pytest.Pytester) -> None:
    # With no nodeids to pass, falling through to pytest would run everything
    # -- turning "re-run my failures" into a full suite run of a suite that
    # had none, which is exactly the surprise an agent cannot afford.
    pytester.makepyfile(
        test_all_fine="""
        def test_one():
            assert True


        def test_two():
            assert True
        """,
    )
    run = pytester.runpytest_subprocess(*conftest.agent_plugin_cli_args(), "--agent", "-q")
    assert run.ret == pytest.ExitCode.OK

    result = conftest.run_cli(["rerun"], cwd=pytester.path)

    assert result.returncode == 0, result.stderr
    assert "nothing to re-run" in result.stdout
    assert not (pytester.path / ".pytest-agent" / "runs-0002").exists()


def test_queries_run_from_a_subdirectory_and_print_paths_that_work_there(failed_run: Path) -> None:
    subdir = failed_run / "src" / "deep"
    subdir.mkdir(parents=True)

    result = conftest.run_cli(["last-failures"], cwd=subdir)

    assert result.returncode == 0, result.stderr
    assert "../../.pytest-agent/runs-0001" in result.stdout
    assert str(failed_run) not in result.stdout


def test_query_output_may_be_piped_because_no_pytest_session_is_involved(failed_run: Path) -> None:
    # The guard exists for pytest's output; a query's output is exactly the
    # thing an agent has good reason to filter. Routing queries through
    # pytest.main() for convenience would silently break this.
    head = subprocess.Popen(["head", "-n", "2"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    if head.stdin is None:
        raise AssertionError("expected head's stdin to be a pipe")

    digest = subprocess.Popen(
        [sys.executable, "-m", "pytest_agent", "digest"],
        cwd=failed_run,
        stdout=head.stdin,
        stderr=subprocess.PIPE,
        text=True,
    )
    head.stdin.close()
    _out, stderr = digest.communicate(timeout=30)
    piped, _ = head.communicate(timeout=30)

    assert digest.returncode == 0
    assert "refusing to run" not in stderr
    assert "2 distinct root causes" in piped


def test_a_second_run_is_what_the_queries_then_describe(failed_run: Path, pytester: pytest.Pytester) -> None:
    # Queries follow the newest run, and --run reaches back to an older one.
    pytester.makepyfile(test_lsp_scenarios="def test_now_fine():\n    assert True\n")
    pytester.runpytest_subprocess(*conftest.agent_plugin_cli_args(), "--agent", "-q")

    latest = conftest.run_cli(["digest"], cwd=failed_run)
    assert latest.returncode == 0, latest.stderr
    assert "runs-0002" in latest.stdout
    assert "no failures out of 1 tests recorded" in latest.stdout

    older = conftest.run_cli(["digest", "--run", "1"], cwd=failed_run)
    assert older.returncode == 0, older.stderr
    assert "2 distinct root causes" in older.stdout


def test_the_cli_still_forwards_everything_else_to_pytest(pytester: pytest.Pytester) -> None:
    pytester.makepyfile(test_sample="def test_ok():\n    assert True\n\ndef test_bad():\n    assert False\n")

    result = conftest.run_cli(["-k", "test_ok"], cwd=pytester.path)

    assert result.returncode == 0
    records = (pytester.path / ".pytest-agent" / "runs-0001" / "index.jsonl").read_text(encoding="utf-8")
    assert "test_ok" in records
    assert "test_bad" not in records


def test_a_mistyped_subcommand_is_named_as_such_instead_of_reaching_pytest(pytester: pytest.Pytester) -> None:
    result = conftest.run_cli(["lastfailures"], cwd=pytester.path)

    assert result.returncode == 2
    assert "did you mean 'last-failures'?" in result.stderr
    # It must not have started a pytest session on the way to saying so.
    assert not (pytester.path / ".pytest-agent").exists()


def test_rerun_can_target_a_run_by_its_label(pytester: pytest.Pytester) -> None:
    # The workflow labels are for: the long suite is started once, under a
    # name, and every later command refers to that name -- so re-running its
    # failures doesn't depend on counting the runs done while it went.
    pytester.makepyfile(schema_helper=_HELPER)
    pytester.makepyfile(test_lsp_scenarios=_TESTS)
    result = pytester.runpytest_subprocess(
        *conftest.agent_plugin_cli_args(),
        "--agent",
        "--agent-label=full-suite",
        "-q",
    )
    assert result.ret == pytest.ExitCode.TESTS_FAILED

    reran = conftest.run_cli(["rerun", "--run", "full-suite"], cwd=pytester.path)

    assert reran.returncode != 0, "the failures were re-run, so the run must still fail"
    assert "re-running 5 failed from .pytest-agent/runs-0001" in reran.stdout
    assert len(_recorded_nodeids(pytester.path, 2)) == 5


def test_a_label_that_could_be_read_as_a_run_number_is_refused_up_front(pytester: pytest.Pytester) -> None:
    # Refused at the writing end so the reading end never has to guess: with a
    # run labeled "2" on disk, `--run 2` would mean different runs depending
    # on what else was there.
    pytester.makepyfile(test_sample="def test_ok():\n    assert True\n")

    result = pytester.runpytest_subprocess(*conftest.agent_plugin_cli_args(), "--agent", "--agent-label=2", "-q")

    assert result.ret == pytest.ExitCode.USAGE_ERROR
    result.stderr.fnmatch_lines(["*--agent-label*all digits*"])
    # And nothing was claimed on disk for a run that never started.
    assert not (pytester.path / ".pytest-agent").exists()
