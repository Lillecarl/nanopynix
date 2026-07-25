from __future__ import annotations

import json
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

from pytest_agent._capture import (
    MAX_NAME_BYTES,
    MAX_NODEID_DISPLAY_CHARS,
    TestRecorder,
    abbreviate_nodeid,
    fit_component,
    nodeid_to_relpath,
    safe_component,
)

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


def _report(  # noqa: PLR0913 tracked complexity/arg-count debt, see TODO.md
    nodeid: str,
    when: str,
    *,
    duration: float = 0.01,
    longreprtext: str = "",
    longrepr: object = None,
    capstdout: str = "",
    capstderr: str = "",
) -> pytest.TestReport:
    # A duck-typed stand-in: TestRecorder only reads these specific
    # attributes, and pytest.TestReport has no simple public constructor.
    # `longrepr` is always present on a real report (None when the phase
    # produced no representation at all), so the stand-in always defines it
    # too -- crash extraction reads it for every failing phase.
    fake = SimpleNamespace(
        nodeid=nodeid,
        when=when,
        duration=duration,
        longreprtext=longreprtext,
        longrepr=longrepr,
        capstdout=capstdout,
        capstderr=capstderr,
        caplog="",
    )
    return cast("pytest.TestReport", fake)


def test_records_a_passing_test(tmp_path: Path) -> None:
    recorder = TestRecorder(tmp_path / "agent", rootpath=tmp_path)
    recorder.start()

    assert recorder.add_report(_report("t.py::test_a", "setup"), "") is None
    assert recorder.add_report(_report("t.py::test_a", "call", capstdout="hi"), "passed") is None
    record = recorder.add_report(_report("t.py::test_a", "teardown"), "")

    if record is None:
        raise AssertionError("expected a finalized record on teardown")
    assert record["outcome"] == "passed"

    log_text = (tmp_path / "agent" / record["log_file"]).read_text(encoding="utf-8")
    assert "hi" in log_text

    index_lines = (tmp_path / "agent" / "index.jsonl").read_text(encoding="utf-8").splitlines()
    assert json.loads(index_lines[0])["nodeid"] == "t.py::test_a"


def test_records_a_failing_call_as_failed(tmp_path: Path) -> None:
    recorder = TestRecorder(tmp_path / "agent", rootpath=tmp_path)
    recorder.start()

    recorder.add_report(_report("t.py::test_b", "setup"), "")
    recorder.add_report(_report("t.py::test_b", "call", longreprtext="boom"), "failed")
    record = recorder.add_report(_report("t.py::test_b", "teardown"), "")

    if record is None:
        raise AssertionError("expected a finalized record on teardown")
    assert record["outcome"] == "failed"
    assert record["has_traceback"] is True


def test_a_failing_setup_wins_over_a_passing_teardown(tmp_path: Path) -> None:
    recorder = TestRecorder(tmp_path / "agent", rootpath=tmp_path)
    recorder.start()

    recorder.add_report(_report("t.py::test_c", "setup", longreprtext="setup exploded"), "error")
    record = recorder.add_report(_report("t.py::test_c", "teardown"), "")

    if record is None:
        raise AssertionError("expected a finalized record on teardown")
    assert record["outcome"] == "error"


def test_nodeid_to_relpath_sanitizes_slashes_in_parametrized_names() -> None:
    rel = nodeid_to_relpath("tests/test_x.py::test_param[a/b]")
    assert "/" not in rel.name
    assert str(rel.parent) == "tests/test_x.py"


def test_a_short_name_is_left_exactly_as_it_is() -> None:
    assert fit_component("test_ok[a-b]") == "test_ok[a-b]"


def test_a_name_too_long_for_the_filesystem_is_cut_down_to_fit() -> None:
    # A parametrized id is bounded only by what the test passed to
    # parametrize -- a long string, a serialized payload -- and a component
    # over NAME_MAX makes every write for that test fail with ENAMETOOLONG.
    fitted = fit_component("test_x[" + "y" * 400 + "]")

    assert len(fitted.encode("utf-8")) <= MAX_NAME_BYTES
    # The head survives, so the file is still recognizably that test's.
    assert fitted.startswith("test_x[yyy")


def test_two_long_names_sharing_a_prefix_do_not_collide() -> None:
    # Truncation alone would map every parametrization of one test with a
    # long payload onto a single file, so each would overwrite the last.
    first = fit_component("test_x[" + "y" * 400 + "-one]")
    second = fit_component("test_x[" + "y" * 400 + "-two]")

    assert first != second


def test_cutting_a_long_name_never_leaves_half_a_character() -> None:
    # NAME_MAX is a byte limit and ids are text: a byte-wise cut can land
    # inside a multi-byte character, and half a character is not a filename.
    fitted = fit_component("test_x[" + "é" * 400 + "]")

    assert len(fitted.encode("utf-8")) <= MAX_NAME_BYTES
    fitted.encode("utf-8").decode("utf-8")  # would raise if it were cut mid-character


def test_a_test_whose_detail_cannot_be_written_is_still_recorded(tmp_path: Path) -> None:
    # Recording detail happens inside pytest_runtest_logreport, where raising
    # is an INTERNALERROR that abandons every test after this one. Whatever
    # the filesystem objects to -- a full disk, a read-only mount -- the
    # outcome must still reach index.jsonl.
    root = tmp_path / "agent"
    root.mkdir()
    # A regular file where the test's directory needs to be, so creating it
    # fails the way a hostile filesystem would.
    (root / "t.py").write_text("in the way", encoding="utf-8")
    recorder = TestRecorder(root, rootpath=tmp_path)
    recorder.start()

    recorder.add_report(_report("t.py::test_a", "setup"), "")
    recorder.add_report(_report("t.py::test_a", "call", longreprtext="boom"), "failed")
    record = recorder.add_report(_report("t.py::test_a", "teardown"), "")

    if record is None:
        raise AssertionError("expected a finalized record on teardown")
    assert record["outcome"] == "failed"
    assert "Error" in record["capture_error"]
    # And it is in the index, where the queries will find it.
    indexed = json.loads((root / "index.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert indexed["nodeid"] == "t.py::test_a"
    assert indexed["capture_error"] == record["capture_error"]
    assert recorder.records_with_capture_error() == [record]


def test_a_name_needing_no_change_is_used_as_it_is() -> None:
    # The common case, and the one that makes a log path readable back as a
    # nodeid -- which is why last-failures can print one path instead of a
    # path and an id.
    assert safe_component("test_ok[in_process-local]") == "test_ok[in_process-local]"


def test_names_that_sanitize_alike_stay_distinct() -> None:
    # Sanitizing is not injective: both of these become "test_p[a_b]", so
    # without disambiguation one test's log overwrites the other's and a
    # query answers about the wrong test.
    assert safe_component("test_p[a/b]") != safe_component("test_p[a_b]")
    assert safe_component("test_p[a/b]") != safe_component("test_p[a\\b]")


def test_a_name_at_the_limit_leaves_room_for_the_stuck_suffix() -> None:
    # MAX_NAME_BYTES reserves slack under NAME_MAX (255) for the longest
    # suffix appended to a test's name. If that reserve is ever shaved, the
    # failure is silent and lands on exactly the run that needs it most: a
    # hang, whose stack dump is written best-effort and would be dropped.
    fitted = fit_component("z" * 400)
    assert len(fitted.encode("utf-8")) == MAX_NAME_BYTES
    assert len(f"{fitted}.stuck.txt".encode()) <= 255


def test_a_long_nodeid_is_shortened_for_the_terminal() -> None:
    # The progress line reprints the running nodeid on every heartbeat, so a
    # hung test with a monster parametrized id would repeat that line until
    # the run is killed.
    nodeid = f"tests/test_mod.py::test_thing[{'z' * 400}-local]"
    shortened = abbreviate_nodeid(nodeid)
    assert len(shortened) <= MAX_NODEID_DISPLAY_CHARS
    # Both ends survive: the file path tells you where, the trailing
    # parameters tell you which member of the family.
    assert shortened.startswith("tests/test_mod.py::test_thing[")
    assert shortened.endswith("-local]")


def test_an_ordinary_nodeid_is_left_exactly_as_it_is() -> None:
    # The cap bounds the pathological case; it must not quietly rewrite the
    # ids a normal suite prints, which the queries are matched against by eye.
    nodeid = "tests/pynix/test_lsp_scenarios.py::test_hover_resolves[in_process-local]"
    assert abbreviate_nodeid(nodeid) == nodeid
