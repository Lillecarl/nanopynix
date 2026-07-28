"""Sum CPU time across a process and its descendants.

``time.process_time()`` only sees this process's threads, which is exactly the
wrong instrument for RPC: the evaluation happens in worker processes. And
``RUSAGE_CHILDREN`` only counts children that have been *reaped*, so live
workers contribute nothing to it -- an undercount that has already produced one
wrong conclusion in this investigation.

So read ``/proc`` directly. This is Linux-only and fine for that: it is a
measurement aid in a prototype, not shipped behaviour.
"""

from __future__ import annotations

import os
from pathlib import Path

CLOCK_TICKS = os.sysconf("SC_CLK_TCK")


def _stat_fields(pid: int) -> list[str] | None:
    """Fields of ``/proc/<pid>/stat`` from ``state`` onwards, 0-indexed at field 3.

    Split after the last ``)`` because ``comm`` is parenthesised and may itself
    contain spaces and parentheses -- splitting the whole line breaks on any
    process whose name has a space in it.
    """
    try:
        line = Path(f"/proc/{pid}/stat").read_text()
    except (OSError, ValueError):
        return None
    close = line.rfind(")")
    if close == -1:
        return None
    return line[close + 2 :].split()


def _ppid(pid: int) -> int | None:
    fields = _stat_fields(pid)
    if fields is None or len(fields) < 2:
        return None
    try:
        return int(fields[1])
    except ValueError:
        return None


def _cpu_seconds(pid: int) -> float:
    fields = _stat_fields(pid)
    # utime and stime are fields 14 and 15 one-indexed, i.e. offsets 11 and 12
    # from `state`.
    if fields is None or len(fields) < 13:
        return 0.0
    try:
        return (int(fields[11]) + int(fields[12])) / CLOCK_TICKS
    except ValueError:
        return 0.0


def descendants(pid: int) -> list[int]:
    """Every live descendant of *pid*, found by walking ``/proc`` parent links.

    Whole-tree rather than direct children: a worker may be launched through an
    intermediate process, and this measurement must not depend on knowing which.
    """
    children: dict[int, list[int]] = {}
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        child = int(entry.name)
        parent = _ppid(child)
        if parent is not None:
            children.setdefault(parent, []).append(child)

    found: list[int] = []
    queue = list(children.get(pid, ()))
    while queue:
        current = queue.pop()
        found.append(current)
        queue.extend(children.get(current, ()))
    return found


def tree_cpu_seconds(pid: int | None = None) -> float:
    """CPU seconds burned by *pid* and every live descendant."""
    root = os.getpid() if pid is None else pid
    return _cpu_seconds(root) + sum(_cpu_seconds(child) for child in descendants(root))
