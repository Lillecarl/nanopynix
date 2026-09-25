"""A build monitor in the style of nix-output-monitor (nom).

``MonitorState.apply`` folds the log events of a session with activity
tracking into a model of builds and copies, and ``render`` draws that model
the way nom draws its own: a list of what runs and what finished, and a ``∑``
row of totals. Neither touches a terminal, so both run in a test as plain
functions.

The ``∑`` totals of planned work come from Nix's own summary activities
(``actBuilds`` and ``actCopyPaths``), which is what Nix's progress bar reads.
nom reads the same numbers from ``these N derivations will be built``, which
a library build never prints.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final

from rich.text import Text

from nanopynix import ActivityType, LogLevel, ResultType
from nanopynix._typechecking import BEARTYPING

if TYPE_CHECKING or BEARTYPING:
    from nanopynix import LogEvent

VERTICAL: Final = "┃"
LOWER_LEFT: Final = "┗"
UPPER_LEFT: Final = "┏"
LEFT_T: Final = "┣"
HORIZONTAL: Final = "━"
DOWN: Final = "↓"
UP: Final = "↑"
CLOCK: Final = "⏱"
RUNNING: Final = "⏵"
DONE: Final = "✔"
TODO: Final = "⏸"
WARNING: Final = "⚠"
SUM: Final = "∑"

#: nom gives the list a third of the terminal, and 20 lines without one.
TARGET_RATIO: Final = 3
DEFAULT_LIST_LINES: Final = 20
#: nom shows no timer under a second, to keep the list quiet.
TIMER_THRESHOLD: Final = 1.0

_UNIT_LIMIT: Final = 1000
_LOCAL_STORES: Final = frozenset({"", "local", "daemon", "auto"})
_DRV_PATH = re.compile(r"/[^\s'\"]+\.drv")
_HASH_PREFIX = re.compile(r"^[0-9a-z]{32}-")
_LOG_LINE_RESULTS: Final = frozenset({ResultType.BUILD_LOG_LINE, ResultType.POST_BUILD_LOG_LINE})


def store_path_name(path: str) -> str:
    """``/nix/store/<hash>-hello-2.12.drv`` as ``hello-2.12``."""
    base = path.rsplit("/", 1)[-1].removesuffix(".drv")
    return _HASH_PREFIX.sub("", base)


def is_local_store(uri: str) -> bool:
    """Whether a copy to or from ``uri`` is neither a download nor an upload."""
    scheme = uri.split("?", 1)[0]
    return scheme in _LOCAL_STORES or scheme.startswith(("local", "unix://"))


def format_duration(seconds: float) -> str:
    whole = int(seconds)
    days, rest = divmod(whole, 86400)
    hours, rest = divmod(rest, 3600)
    minutes, secs = divmod(rest, 60)
    if days:
        return f"{days}d{hours:02}h{minutes:02}m{secs:02}s"
    if hours:
        return f"{hours}h{minutes:02}m{secs:02}s"
    if minutes:
        return f"{minutes}m{secs:02}s"
    return f"{secs}s"


def format_bytes(count: int) -> str:
    """nom's units: steps of 1024, and a new unit from 1000 on, so no value needs four digits."""
    value = float(count)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < _UNIT_LIMIT:
            return f"{value:.1f}{unit}"
        value /= 1024
    return f"{value:.1f}PiB"


@dataclass
class Build:
    drv: str
    host: str
    start: float
    end: float | None = None
    phase: str | None = None
    failed: bool = False

    @property
    def name(self) -> str:
        return store_path_name(self.drv)


@dataclass
class Transfer:
    path: str
    host: str
    upload: bool
    start: float
    end: float | None = None
    done_bytes: int = 0
    expected_bytes: int = 0

    @property
    def name(self) -> str:
        return store_path_name(self.path)


@dataclass
class Summary:
    """One ``actBuilds`` or ``actCopyPaths``, as its last ``resProgress`` left it."""

    activity_type: ActivityType
    done: int = 0
    expected: int = 0
    running: int = 0
    failed: int = 0
    stopped: bool = False

    @property
    def planned(self) -> int:
        # A stopped summary plans nothing. Nix counts a finished build before it
        # drops the expectation, so its last update reads [1, 2] for one build.
        if self.stopped:
            return 0
        return max(self.expected - self.done - self.running - self.failed, 0)


@dataclass
class Totals:
    builds_running: int
    builds_done: int
    builds_planned: int
    builds_failed: int
    downloads_running: int
    downloads_done: int
    downloads_planned: int
    uploads_running: int
    uploads_done: int


@dataclass
class MonitorState:
    """What the monitor knows. ``apply`` is the only writer."""

    started: float
    print_build_logs: bool = False
    builds: dict[int, Build] = field(default_factory=dict[int, Build])
    transfers: dict[int, Transfer] = field(default_factory=dict[int, Transfer])
    summaries: dict[int, Summary] = field(default_factory=dict[int, Summary])
    errors: int = 0

    def apply(self, event: LogEvent, now: float) -> list[Text]:
        """Fold one event in, and return the lines to print above the monitor."""
        match event.action:
            case "start":
                return self._start(event.args, now)
            case "stop":
                self._stop(event.args[0], now)
                return []
            case "result":
                return self._result(event.args)
            case "error":
                return [self.note_error(event.message or "")]
            case "msg" | "warn":
                message = event.message
                return [] if message is None else [Text.from_ansi(message)]
            case _:
                return []

    def _start(self, args: list[Any], now: float) -> list[Text]:
        activity_id, level, raw_type, text, fields = args[0], args[1], args[2], args[3], args[4]
        match raw_type:
            case ActivityType.BUILD:
                self.builds[activity_id] = Build(drv=fields[0], host=fields[1] if len(fields) > 1 else "", start=now)
            case ActivityType.COPY_PATH:
                path, source, destination = fields[0], fields[1], fields[2]
                if not is_local_store(source):
                    self.transfers[activity_id] = Transfer(path, source, upload=False, start=now)
                elif not is_local_store(destination):
                    self.transfers[activity_id] = Transfer(path, destination, upload=True, start=now)
            case ActivityType.BUILDS | ActivityType.COPY_PATHS:
                self.summaries[activity_id] = Summary(ActivityType(raw_type))
            case _:
                pass
        # nom prints the text of every activity at `info` or below, as Nix does.
        return [Text.from_ansi(text)] if text and level <= LogLevel.INFO else []

    def _stop(self, activity_id: int, now: float) -> None:
        if (build := self.builds.get(activity_id)) is not None and build.end is None:
            build.end = now
            # Nix logs no error for the build a caller asked for; it returns
            # the failure instead. What it does log is the summary's failed
            # count, just before this stop: the worker's single thread counts
            # the failure and then drops the goal's activity, with nothing
            # between. So an unclaimed failure belongs to the build that stops.
            if self._unclaimed_failures() > 0:
                build.failed = True
        elif (transfer := self.transfers.get(activity_id)) is not None and transfer.end is None:
            transfer.end = now
        elif (summary := self.summaries.get(activity_id)) is not None:
            summary.stopped = True

    def _result(self, args: list[Any]) -> list[Text]:
        activity_id, raw_type, fields = args[0], args[1], args[2]
        if raw_type in _LOG_LINE_RESULTS:
            if not self.print_build_logs:
                return []
            build = self.builds.get(activity_id)
            prefix = Text(f"{build.name}> ", style="blue") if build is not None else Text()
            return [prefix + Text.from_ansi(str(fields[0]))]
        if raw_type == ResultType.SET_PHASE and (build := self.builds.get(activity_id)) is not None:
            build.phase = str(fields[0])
        elif raw_type == ResultType.PROGRESS:
            if (summary := self.summaries.get(activity_id)) is not None:
                summary.done, summary.expected, summary.running, summary.failed = (int(f) for f in fields[:4])
            elif (transfer := self.transfers.get(activity_id)) is not None:
                transfer.done_bytes, transfer.expected_bytes = int(fields[0]), int(fields[1])
        return []

    def note_error(self, message: str) -> Text:
        """Count an error from Nix, fail each build it names, and return it to print."""
        self.errors += 1
        text = Text.from_ansi(message)
        named = set(_DRV_PATH.findall(text.plain))
        for build in self.builds.values():
            if build.drv in named:
                build.failed = True
        return text

    def _unclaimed_failures(self) -> int:
        reported = sum(s.failed for s in self.summaries.values() if s.activity_type == ActivityType.BUILDS)
        return reported - sum(b.failed for b in self.builds.values())

    def totals(self) -> Totals:
        builds = self.builds.values()
        transfers = self.transfers.values()
        downloads = [t for t in transfers if not t.upload]
        uploads = [t for t in transfers if t.upload]
        running_downloads = sum(t.end is None for t in downloads)
        done_downloads = sum(t.end is not None for t in downloads)
        return Totals(
            builds_running=sum(b.end is None and not b.failed for b in builds),
            builds_done=sum(b.end is not None and not b.failed for b in builds),
            builds_planned=sum(s.planned for s in self.summaries.values() if s.activity_type == ActivityType.BUILDS),
            builds_failed=sum(b.failed for b in builds),
            downloads_running=running_downloads,
            downloads_done=done_downloads,
            downloads_planned=sum(
                s.planned for s in self.summaries.values() if s.activity_type == ActivityType.COPY_PATHS
            ),
            uploads_running=sum(t.end is None for t in uploads),
            uploads_done=sum(t.end is not None for t in uploads),
        )


# ── rendering ─────────────────────────────────────────────


@dataclass
class _Cell:
    """One cell of nom's table: a label left, a value right, over ``span`` columns."""

    label: str = ""
    value: str = ""
    style: str = ""
    value_style: str = ""
    span: int = 1

    @property
    def width(self) -> int:
        return len(self.label) + len(self.value) + (1 if self.label and self.value else 0)


def _count(label: str, count: int, style: str) -> _Cell:
    return _Cell(label=label, value=str(count), style=style, value_style="bold" if count > 0 else "")


_SEP: Final = " │ "


def _aligned(rows: list[list[_Cell]]) -> list[Text]:
    columns = max(sum(c.span for c in row) for row in rows)
    widths = [0] * columns
    for row in rows:
        index = 0
        for cell in row:
            if cell.span == 1:
                widths[index] = max(widths[index], cell.width)
            index += cell.span
    lines: list[Text] = []
    for row in rows:
        line = Text()
        index = 0
        for position, cell in enumerate(row):
            width = sum(widths[index : index + cell.span]) + len(_SEP) * (cell.span - 1)
            index += cell.span
            if position:
                line.append(_SEP)
            gap = " " * max(width - cell.width, 0)
            line.append(cell.label, style=cell.style)
            if cell.label and cell.value:
                line.append(" ")
            line.append(gap)
            line.append(cell.value, style=f"{cell.style} {cell.value_style}".strip())
        line.rstrip()
        lines.append(line)
    return lines


def _timer(seconds: float) -> str:
    return f" {CLOCK} {format_duration(seconds)}" if seconds > TIMER_THRESHOLD else ""


def _bar(length: int, part: float) -> str:
    filled = part * length
    return "".join("■" if i <= filled else "◧" if i <= filled + 0.5 else "□" for i in range(1, length + 1))


def _build_line(build: Build, now: float) -> Text:
    host = f" on {build.host}" if build.host else ""
    if build.failed:
        end = build.end if build.end is not None else now
        phase = f" in {build.phase}" if build.phase else ""
        timer = f"{CLOCK} {format_duration(end - build.start)}"
        return Text(f"{WARNING} {build.name}{host} failed after {timer}{phase}", style="bold red")
    if build.end is None:
        line = Text(f"{RUNNING} {build.name}", style="bold yellow")
        line.append(host, style="magenta")
        if build.phase:
            line.append(f" ({build.phase})", style="bold")
        line.append(_timer(now - build.start))
        return line
    line = Text(f"{DONE} {build.name}", style="green")
    line.append(f"{host}{_timer(build.end - build.start)}", style="bright_black")
    return line


def _transfer_line(transfer: Transfer, now: float, width: int) -> Text:
    arrow = UP if transfer.upload else DOWN
    if transfer.end is not None:
        line = Text(f"{arrow} {DONE} {transfer.name}", style="green")
        size = f" {format_bytes(transfer.expected_bytes)}" if transfer.expected_bytes else ""
        line.append(f"{size}{_timer(transfer.end - transfer.start)}", style="bright_black")
        return line
    line = Text(f"{arrow} {RUNNING} {transfer.name}", style="bold yellow")
    line.append(_timer(now - transfer.start))
    if transfer.expected_bytes:
        part = min(transfer.done_bytes / transfer.expected_bytes, 1.0)
        line.append(f" {format_bytes(transfer.done_bytes)}/{format_bytes(transfer.expected_bytes)}")
        left = max(60, line.cell_len + 1)
        bar_length = width - left - 6
        if bar_length > 0:
            line.append(" " * (left - line.cell_len))
            line.append(_bar(bar_length, part))
            line.append(f"{part * 100:5.1f}%", style="bold")
    return line


def _activity_lines(state: MonitorState, now: float, limit: int, width: int) -> list[Text]:
    """Failed first, then running, then the most recent finished, up to ``limit``."""
    builds = list(state.builds.values())
    transfers = list(state.transfers.values())
    failed = [b for b in builds if b.failed]
    running_builds = [b for b in builds if b.end is None and not b.failed]
    running_transfers = [t for t in transfers if t.end is None]
    finished_builds = sorted((b for b in builds if b.end is not None and not b.failed), key=lambda b: -(b.end or 0))
    finished_transfers = sorted((t for t in transfers if t.end is not None), key=lambda t: -(t.end or 0))
    lines = [_build_line(b, now) for b in failed + running_builds]
    lines += [_transfer_line(t, now, width) for t in running_transfers]
    lines += [_build_line(b, now) for b in finished_builds]
    lines += [_transfer_line(t, now, width) for t in finished_transfers]
    return lines[:limit]


def _time_text(state: MonitorState, now: float, finished_at: str | None) -> Text:
    elapsed = format_duration(now - state.started)
    if finished_at is None:
        return Text(f"{CLOCK} {elapsed}", style="bold")
    failures = state.totals().builds_failed
    if failures:
        return Text(f"{WARNING} Exited after {failures} build failures at {finished_at} after {elapsed}", style="red")
    if state.errors:
        return Text(
            f"{WARNING} Exited with {state.errors} errors reported by nix at {finished_at} after {elapsed}", style="red"
        )
    return Text(f"Finished at {finished_at} after {elapsed}", style="green")


def _table(state: MonitorState, time_text: Text) -> list[Text]:
    totals = state.totals()
    show_builds = totals.builds_running + totals.builds_done + totals.builds_planned + totals.builds_failed > 0
    show_downloads = totals.downloads_running + totals.downloads_done + totals.downloads_planned > 0
    show_uploads = totals.uploads_running + totals.uploads_done > 0
    headers: list[_Cell] = []
    last: list[_Cell] = []
    if show_builds:
        headers.append(_Cell(label="Builds", style="bold", span=3))
        last += [
            _count(RUNNING, totals.builds_running, "yellow"),
            _count(DONE, totals.builds_done, "green"),
            _count(TODO, totals.builds_planned, "blue"),
        ]
    if show_downloads:
        headers.append(_Cell(label="Downloads", style="bold", span=3))
        last += [
            _count(DOWN, totals.downloads_running, "yellow"),
            _count(DOWN, totals.downloads_done, "green"),
            _count(TODO, totals.downloads_planned, "blue"),
        ]
    if show_uploads:
        headers.append(_Cell(label="Uploads", style="bold", span=2))
        last += [_count(UP, totals.uploads_running, "yellow"), _count(UP, totals.uploads_done, "green")]
    last.append(_Cell(label=time_text.plain, style=str(time_text.style)))
    rows = [headers or [_Cell()], last]
    header_line, last_line = _aligned(rows)
    top = Text(HORIZONTAL * 3 + " ") + header_line
    bottom = Text(f"{LOWER_LEFT}{HORIZONTAL} {SUM} ") + last_line
    return [top, bottom]


def render(state: MonitorState, now: float, *, width: int, height: int | None, finished_at: str | None = None) -> Text:
    """Draw ``state`` as nom draws its status, ``width`` columns wide.

    ``finished_at`` is the wall-clock time the build ended, and turns the timer
    into nom's closing line.
    """
    limit = height // TARGET_RATIO if height is not None else DEFAULT_LIST_LINES
    activity = _activity_lines(state, now, limit, width - 2)
    table = _table(state, _time_text(state, now, finished_at))
    lines: list[Text] = []
    if activity:
        lines.append(Text(f"{UPPER_LEFT}{HORIZONTAL} ") + Text("Activity:", style="bold"))
        lines += [Text(f"{VERTICAL} ") + line for line in activity]
        lines.append(Text(LEFT_T) + table[0])
    else:
        lines.append(Text(UPPER_LEFT) + table[0])
    lines.append(table[1])
    out = Text("\n").join(lines)
    out.no_wrap = True
    out.overflow = "ellipsis"
    return out
