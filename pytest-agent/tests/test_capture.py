from __future__ import annotations

import json
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

from pytest_agent._capture import TestRecorder, nodeid_to_relpath

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
