# pyright: reportUnknownMemberType=false
# pytest.Pytester's makepyfile is typed as (*args, **kwargs) -> Path -- untyped
# varargs, not a stub gap specific to this file.

from __future__ import annotations

import os
from pathlib import Path

import conftest
import pytest
from pytest_agent._harness_detect import HARNESS_ENV_VARS

_SRC = Path(__file__).resolve().parents[1] / "src"


@pytest.fixture(autouse=True)
def _agent_on_pythonpath(monkeypatch: pytest.MonkeyPatch) -> None:  # type: ignore[reportUnusedFunction] -- pytest autouse fixture, wired by pytest
    existing = os.environ.get("PYTHONPATH", "")
    parts = [str(_SRC), *([existing] if existing else [])]
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join(parts))
    for name in HARNESS_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


def test_profile_fixture_writes_a_report_alongside_agent_mode_output(pytester: pytest.Pytester) -> None:
    pytester.makepyfile(
        test_sample="""
        def _busy_loop():
            total = 0
            for i in range(200_000):
                total += i * i
            return total

        def test_profiled(profile):
            _busy_loop()
        """
    )

    result = pytester.runpytest_subprocess(
        *conftest.agent_plugin_cli_args(), "--agent", "--agent-dir=.pytest-agent", "-q"
    )
    assert result.ret == pytest.ExitCode.OK

    run_dir = pytester.path / ".pytest-agent" / "runs-0001"
    report_path = next(run_dir.rglob("test_profiled.profile.txt"))
    report_text = report_path.read_text(encoding="utf-8")
    assert "_busy_loop" in report_text
    # The .log/.json files from the normal per-test capture must still exist
    # alongside the profile report, in the same directory.
    assert (report_path.parent / "test_profiled.log").exists()


def test_profile_fixture_falls_back_to_a_fixed_directory_without_agent_mode(pytester: pytest.Pytester) -> None:
    pytester.makepyfile(test_sample="def test_profiled(profile):\n    sum(i * i for i in range(100_000))\n")

    result = pytester.runpytest_subprocess(*conftest.agent_plugin_cli_args(), "-q")
    assert result.ret == pytest.ExitCode.OK

    report_path = pytester.path / ".pytest-agent" / "profiles" / "test_sample.py" / "test_profiled.profile.txt"
    assert report_path.exists()
    assert "test_profiled" in report_path.read_text(encoding="utf-8")
