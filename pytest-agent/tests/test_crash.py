from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

from pytest_agent._crash import (
    MAX_FRAMES,
    crash_from_report,
    frames_from_report,
    normalize_message,
    split_exception_type,
)

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


def _entry(path: str, lineno: int) -> SimpleNamespace:
    return SimpleNamespace(reprfileloc=SimpleNamespace(path=path, lineno=lineno, message=""))


def _report(
    *,
    message: str = "",
    path: str = "",
    lineno: int | None = None,
    entries: list[SimpleNamespace] | None = None,
    longreprtext: str = "",
) -> pytest.TestReport:
    # Mirrors the shape of pytest's ExceptionChainRepr: a reprcrash plus a
    # reprtraceback of entries, each with a reprfileloc. Duck-typed because
    # the real classes have no usable public constructor.
    reprcrash = SimpleNamespace(message=message, path=path, lineno=lineno) if message or path else None
    longrepr = SimpleNamespace(
        reprcrash=reprcrash,
        reprtraceback=SimpleNamespace(reprentries=entries or []),
    )
    return cast("pytest.TestReport", SimpleNamespace(longrepr=longrepr, longreprtext=longreprtext))


def test_crash_reads_type_message_and_location_from_the_report(tmp_path: Path) -> None:
    report = _report(message="FileNotFoundError: /nix/store/x-swagger.json", path="tests/test_a.py", lineno=12)

    crash = crash_from_report(report, tmp_path)

    if crash is None:
        raise AssertionError("expected a crash record")
    assert crash["exc_type"] == "FileNotFoundError"
    assert crash["message"] == "FileNotFoundError: /nix/store/x-swagger.json"
    assert crash["path"] == "tests/test_a.py"
    assert crash["lineno"] == 12


def test_crash_falls_back_to_the_rendered_exception_line_without_a_reprcrash(tmp_path: Path) -> None:
    # pytest uses reprs with no reprcrash at all for some collection
    # failures (CollectErrorRepr), where the rendered text is all there is.
    report = cast(
        "pytest.TestReport",
        SimpleNamespace(
            longrepr="ImportError: no module named nope",
            longreprtext="tests/test_a.py:3: in <module>\n    import nope\nE   ImportError: no module named nope\n",
        ),
    )

    crash = crash_from_report(report, tmp_path)

    if crash is None:
        raise AssertionError("expected a crash record from the rendered text")
    assert crash["exc_type"] == "ImportError"
    assert crash["message"] == "ImportError: no module named nope"


def test_crash_is_none_when_the_report_has_no_representation_at_all(tmp_path: Path) -> None:
    report = cast("pytest.TestReport", SimpleNamespace(longrepr=None, longreprtext=""))

    assert crash_from_report(report, tmp_path) is None


def test_crash_survives_a_skip_style_tuple_longrepr(tmp_path: Path) -> None:
    # A skipped test's longrepr is a plain (path, lineno, reason) tuple --
    # no reprcrash, no reprtraceback, and nothing that should blow up here.
    report = cast(
        "pytest.TestReport", SimpleNamespace(longrepr=("tests/test_a.py", 4, "Skipped: nope"), longreprtext="")
    )

    assert crash_from_report(report, tmp_path) is None
    assert frames_from_report(report, tmp_path) == []


def test_a_message_without_a_type_prefix_has_no_exception_type() -> None:
    assert split_exception_type("assert 1 == 2") is None


def test_frames_are_tagged_first_party_by_being_under_rootdir(tmp_path: Path) -> None:
    report = _report(
        message="ValueError: bad",
        path="tests/test_a.py",
        lineno=9,
        entries=[
            _entry(str(tmp_path / "tests" / "test_a.py"), 9),
            _entry("/usr/lib/python3.13/json/decoder.py", 355),
            _entry(str(tmp_path / ".venv" / "lib" / "site-packages" / "requests" / "api.py"), 61),
        ],
    )

    frames = frames_from_report(report, tmp_path)

    assert [frame["path"] for frame in frames] == [
        "tests/test_a.py",
        "/usr/lib/python3.13/json/decoder.py",
        ".venv/lib/site-packages/requests/api.py",
    ]
    # A vendored site-packages tree lives *under* rootdir, so "is it under
    # rootdir" alone is not enough to call a frame first-party.
    assert [frame["first_party"] for frame in frames] == [True, False, False]


def test_only_the_innermost_frames_are_kept(tmp_path: Path) -> None:
    entries = [_entry(str(tmp_path / "deep.py"), lineno) for lineno in range(MAX_FRAMES + 5)]

    frames = frames_from_report(_report(message="RecursionError: too deep", entries=entries), tmp_path)

    assert len(frames) == MAX_FRAMES
    assert frames[-1]["lineno"] == MAX_FRAMES + 4


def test_normalize_collapses_store_hashes_temp_dirs_and_numbers() -> None:
    first = normalize_message("FileNotFoundError: /nix/store/0lqkw72fqnp7q1krbmnc48v4h1bqbxam-swagger.json")
    second = normalize_message("FileNotFoundError: /nix/store/zzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzz-swagger.json")
    assert first == second

    third = normalize_message("OSError: /tmp/pytest-of-alice/pytest-3/test_a0/db.sqlite missing")
    fourth = normalize_message("OSError: /tmp/pytest-of-bob/pytest-91/test_a0/db.sqlite missing")
    assert third == fourth


def test_normalize_keeps_genuinely_different_causes_apart() -> None:
    assert normalize_message("ValueError: bad kind") != normalize_message("KeyError: bad kind")
