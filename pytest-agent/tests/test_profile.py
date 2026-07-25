# pyright: reportUnknownMemberType=false
# pytest.Pytester's makepyfile is typed as (*args, **kwargs) -> Path -- untyped
# varargs, not a stub gap specific to this file.

from __future__ import annotations

import conftest
import pytest

# The PYTHONPATH wiring and harness-env-var cleanup these subprocess runs
# depend on live in conftest._clean_agent_env, which is autouse for every
# test in this package.


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
        """,
    )

    result = pytester.runpytest_subprocess(
        *conftest.agent_plugin_cli_args(),
        "--agent",
        "--agent-dir=.pytest-agent",
        "-q",
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
