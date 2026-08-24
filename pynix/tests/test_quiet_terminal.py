"""Tests for the guard that keeps stderr off a full-screen screen.

A full-screen interface draws the whole terminal, so one line written by
anything else lands in the middle of the drawing. `quiet_terminal` holds every
such line back and prints it after the interface closes.

**Both halves need a test, and they are different mechanisms.** A Python
writer follows `sys.stderr`, and a writer that holds file descriptor 2 does
not. `structlog` is the first and the Nix evaluator is the second.
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

from pynix._impl._quiet import quiet_terminal

_STDERR = 2


def test_a_python_write_does_not_reach_the_screen(capfd: pytest.CaptureFixture[str]) -> None:
    with quiet_terminal():
        sys.stderr.write("from python\n")
        captured_inside = capfd.readouterr().err
    assert captured_inside == ""
    assert "from python" in capfd.readouterr().err


def test_a_descriptor_write_does_not_reach_the_screen(capfd: pytest.CaptureFixture[str]) -> None:
    """A writer that holds the descriptor never sees `sys.stderr` at all."""
    with quiet_terminal():
        os.write(_STDERR, b"from the descriptor\n")
        captured_inside = capfd.readouterr().err
    assert captured_inside == ""
    assert "from the descriptor" in capfd.readouterr().err


def test_a_subprocess_write_does_not_reach_the_screen(capfd: pytest.CaptureFixture[str]) -> None:
    """A child inherits the descriptor, so the guard has to hold it back too."""
    with quiet_terminal():
        subprocess.run([sys.executable, "-c", "import sys; sys.stderr.write('from the child\\n')"], check=True)
        captured_inside = capfd.readouterr().err
    assert captured_inside == ""
    assert "from the child" in capfd.readouterr().err


def test_the_descriptor_is_restored_after_the_block(capfd: pytest.CaptureFixture[str]) -> None:
    with quiet_terminal():
        pass
    capfd.readouterr()
    os.write(_STDERR, b"after\n")
    assert "after" in capfd.readouterr().err


def test_a_failure_restores_the_descriptor(capfd: pytest.CaptureFixture[str]) -> None:
    """A guard that leaks the descriptor on a failure leaves the terminal dead."""
    with pytest.raises(RuntimeError, match="the interface failed"), quiet_terminal():
        raise RuntimeError("the interface failed")
    capfd.readouterr()
    os.write(_STDERR, b"still here\n")
    assert "still here" in capfd.readouterr().err


def test_nothing_is_printed_when_nothing_was_written(capfd: pytest.CaptureFixture[str]) -> None:
    """A quiet block prints no blank line of its own."""
    capfd.readouterr()
    with quiet_terminal():
        pass
    assert capfd.readouterr().err == ""
