from __future__ import annotations

import os


class RealTerminal:
    """A copy of fd 1, taken before pytest's capture manager starts
    redirecting it.

    pytest's default `--capture=fd` mode redirects the real fd 1 to a
    per-test buffer for most of the session, including while a hung test is
    mid-execution -- exactly when a "still running, may be stuck" notice is
    most useful. Writing through this duplicated fd instead of `sys.stdout`
    bypasses that redirection: `os.dup` gives a new fd number pointing at the
    same underlying file description, and pytest's later `os.dup2(tmp, 1)`
    only ever touches fd 1 itself, never other fds that already point
    elsewhere. Must be constructed as early as possible (module import time
    of the plugin, i.e. before any capturing has started) for this to hold.
    """

    def __init__(self) -> None:
        self._fd = os.dup(1)

    def write_line(self, text: str) -> None:
        os.write(self._fd, (text.rstrip("\n") + "\n").encode("utf-8", errors="replace"))
