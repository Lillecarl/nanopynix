from __future__ import annotations

import os
import subprocess
import sys
import time
from typing import TYPE_CHECKING

from pytest_agent._pipe_guard import find_banned_pipe_reader

if TYPE_CHECKING:
    from pathlib import Path


def test_no_reader_reported_for_a_plain_python_reader() -> None:
    read_fd, write_fd = os.pipe()
    proc = subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.read()"], stdin=read_fd)
    os.close(read_fd)
    try:
        deadline = time.monotonic() + 2.0
        found = find_banned_pipe_reader(write_fd)
        while found is not None and time.monotonic() < deadline:
            time.sleep(0.02)
            found = find_banned_pipe_reader(write_fd)
        assert found is None
    finally:
        os.close(write_fd)
        proc.wait(timeout=5)


def test_detects_grep_as_the_pipe_reader() -> None:
    read_fd, write_fd = os.pipe()
    proc = subprocess.Popen(["grep", "x"], stdin=read_fd, stdout=subprocess.DEVNULL)
    os.close(read_fd)
    try:
        deadline = time.monotonic() + 2.0
        found = find_banned_pipe_reader(write_fd)
        while found is None and time.monotonic() < deadline:
            time.sleep(0.02)
            found = find_banned_pipe_reader(write_fd)
        assert found == "grep"
    finally:
        os.close(write_fd)
        proc.terminate()
        proc.wait(timeout=5)


def test_no_reader_reported_for_a_regular_file_fd(tmp_path: Path) -> None:
    with (tmp_path / "out.txt").open("w", encoding="utf-8") as handle:
        assert find_banned_pipe_reader(handle.fileno()) is None
