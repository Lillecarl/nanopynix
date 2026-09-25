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
from enum import Enum
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


@dataclass(frozen=True)
class BuildPlan:
    """What a build will do, and how its derivations depend on each other.

    ``children`` holds, for each node, the inputs that are nodes too: a
    derivation to build, or one whose outputs will be downloaded. ``outputs``
    maps a node to its output paths, which is how a download finds its node.
    """

    roots: tuple[str, ...]
    children: dict[str, tuple[str, ...]]
    outputs: dict[str, frozenset[str]]
    builds: frozenset[str]
    downloads: frozenset[str]


class NodeStatus(Enum):
    """A node's state, in the order nom gives room to it: work in flight first."""

    FAILED = 0
    BUILDING = 1
    DOWNLOADING = 2
    UPLOADING = 3
    PLANNED_BUILD = 4
    PLANNED_DOWNLOAD = 5
    BUILT = 6
    DOWNLOADED = 7
    UPLOADED = 8
    NONE = 9


_IN_FLIGHT: Final = frozenset({NodeStatus.FAILED, NodeStatus.BUILDING, NodeStatus.DOWNLOADING, NodeStatus.UPLOADING})
_PLANNED: Final = frozenset({NodeStatus.PLANNED_BUILD, NodeStatus.PLANNED_DOWNLOAD})


def _lowermost_tree(roots: list[str], edges: dict[str, tuple[str, ...]]) -> dict[str, list[str]]:
    """The DAG under ``roots`` as a tree, each node under its deepest parent.

    Depth is the longest path from a root, so a parent always comes before
    its children in depth order and the owner is known when a child is placed.
    """
    parents: dict[str, list[str]] = {root: [] for root in roots}
    stack = list(roots)
    while stack:
        node = stack.pop()
        for child in edges.get(node, ()):
            if child not in parents:
                parents[child] = []
                stack.append(child)
            parents[child].append(node)
    # Kahn's order over the reachable nodes; a cycle cannot occur in a plan.
    waiting = {node: len(ps) for node, ps in parents.items()}
    ready = [node for node, count in waiting.items() if count == 0]
    depth: dict[str, int] = dict.fromkeys(ready, 0)
    tree: dict[str, list[str]] = {node: [] for node in parents}
    while ready:
        node = ready.pop()
        if parents[node]:
            owner = max(parents[node], key=lambda p: depth[p])
            depth[node] = depth[owner] + 1
            tree[owner].append(node)
        for child in edges.get(node, ()):
            waiting[child] -= 1
            if waiting[child] == 0:
                ready.append(child)
    return tree


@dataclass
class MonitorState:
    """What the monitor knows. ``apply`` and ``plan`` are the only writers."""

    started: float
    print_build_logs: bool = False
    plan: BuildPlan | None = None
    builds: dict[int, Build] = field(default_factory=dict[int, Build])
    transfers: dict[int, Transfer] = field(default_factory=dict[int, Transfer])
    summaries: dict[int, Summary] = field(default_factory=dict[int, Summary])
    errors: int = 0
    _build_of_drv: dict[str, Build] = field(default_factory=dict[str, Build], init=False, repr=False)
    _transfer_of_path: dict[str, Transfer] = field(default_factory=dict[str, Transfer], init=False, repr=False)

    def node_status(self, node: str) -> NodeStatus:
        """What ``node``, a derivation or an unplanned path, is doing now."""
        if (build := self._build_of_drv.get(node)) is not None:
            if build.failed:
                return NodeStatus.FAILED
            return NodeStatus.BUILDING if build.end is None else NodeStatus.BUILT
        transfers = self.transfers_of(node)
        running = [t for t in transfers if t.end is None]
        plan = self.plan
        status = NodeStatus.NONE
        if running:
            status = NodeStatus.UPLOADING if running[0].upload else NodeStatus.DOWNLOADING
        elif plan is not None and node in plan.builds:
            status = NodeStatus.PLANNED_BUILD
        elif transfers:
            status = NodeStatus.UPLOADED if transfers[0].upload else NodeStatus.DOWNLOADED
        elif plan is not None and plan.outputs.get(node, frozenset[str]()) & plan.downloads:
            status = NodeStatus.PLANNED_DOWNLOAD
        return status

    def build_of(self, node: str) -> Build | None:
        """The last build of ``node``, if one started."""
        return self._build_of_drv.get(node)

    def transfers_of(self, node: str) -> list[Transfer]:
        """The copies of ``node``'s outputs that started."""
        paths: frozenset[str] | None = self.plan.outputs.get(node) if self.plan is not None else None
        if paths is None:
            paths = frozenset[str]() if node.endswith(".drv") else frozenset({node})
        return [t for p in sorted(paths) if (t := self._transfer_of_path.get(p)) is not None]

    def forest(self) -> tuple[list[str], dict[str, list[str]]]:
        """The roots, and each node's children, with every node under one parent only.

        A node that several nodes need goes under the deepest of them, as nom
        puts it "only for the lowermost dependency", so it draws once and the
        chain above it stays whole.

        The plan's roots come first. A build or a copy the plan does not name
        is a root of its own, which is the whole display when there is no
        plan. A download the plan names but no node owns is only counted, as
        nom does with a path whose derivation it never saw.
        """
        plan = self.plan
        roots = list(plan.roots) if plan is not None else []
        tree = _lowermost_tree(roots, plan.children if plan is not None else {})
        for kids in tree.values():
            kids.sort(key=lambda c: (self.node_status(c).value, store_path_name(c)))
        seen = set(tree)
        owned = {p for node in seen for p in (plan.outputs.get(node, ()) if plan is not None else ())}
        planned_downloads = plan.downloads if plan is not None else frozenset[str]()
        for build in self.builds.values():
            if build.drv not in seen:
                seen.add(build.drv)
                roots.append(build.drv)
                tree[build.drv] = []
        for transfer in self.transfers.values():
            if transfer.path not in owned and transfer.path not in planned_downloads and transfer.path not in seen:
                seen.add(transfer.path)
                roots.append(transfer.path)
                tree[transfer.path] = []
        return roots, tree

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
                build = Build(drv=fields[0], host=fields[1] if len(fields) > 1 else "", start=now)
                self.builds[activity_id] = build
                self._build_of_drv[build.drv] = build
            case ActivityType.COPY_PATH:
                path, source, destination = fields[0], fields[1], fields[2]
                transfer = None
                if not is_local_store(source):
                    transfer = Transfer(path, source, upload=False, start=now)
                elif not is_local_store(destination):
                    transfer = Transfer(path, destination, upload=True, start=now)
                if transfer is not None:
                    self.transfers[activity_id] = transfer
                    self._transfer_of_path[path] = transfer
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
        # The plan when there is one: Nix's summary overstates what it plans
        # for as long as a finished goal is still counted as expected.
        plan = self.plan
        if plan is not None:
            builds_planned = sum(d not in self._build_of_drv for d in plan.builds)
            downloads_planned = sum(p not in self._transfer_of_path for p in plan.downloads)
        else:
            summaries = self.summaries.values()
            builds_planned = sum(s.planned for s in summaries if s.activity_type == ActivityType.BUILDS)
            downloads_planned = sum(s.planned for s in summaries if s.activity_type == ActivityType.COPY_PATHS)
        return Totals(
            builds_running=sum(b.end is None and not b.failed for b in builds),
            builds_done=sum(b.end is not None and not b.failed for b in builds),
            builds_planned=builds_planned,
            builds_failed=sum(b.failed for b in builds),
            downloads_running=running_downloads,
            downloads_done=done_downloads,
            downloads_planned=downloads_planned,
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


def _transfers_line(name: str, transfers: list[Transfer], now: float) -> tuple[Text, float | None]:
    """A node's copies as one line, and the fraction done while they run.

    The bar goes on the right, where ``_with_bars`` puts it after every line is
    known, because nom aligns the bars of all rows.
    """
    arrow = UP if transfers[0].upload else DOWN
    start = min(t.start for t in transfers)
    expected = sum(t.expected_bytes for t in transfers)
    if all(t.end is not None for t in transfers):
        line = Text(f"{arrow} {DONE} {name}", style="green")
        size = f" {format_bytes(expected)}" if expected else ""
        end = max(t.end or start for t in transfers)
        line.append(f"{size}{_timer(end - start)}", style="bright_black")
        return line, None
    running = [t for t in transfers if t.end is None]
    line = Text(f"{arrow} {RUNNING} {name}", style="bold yellow")
    line.append(_timer(now - start))
    expected = sum(t.expected_bytes for t in running)
    if not expected:
        return line, None
    done = sum(t.done_bytes for t in running)
    line.append(f" {format_bytes(done)}/{format_bytes(expected)}")
    return line, min(done / expected, 1.0)


def _node_line(state: MonitorState, node: str, now: float) -> tuple[Text, float | None]:
    status = state.node_status(node)
    name = store_path_name(node)
    build = state.build_of(node)
    if build is not None:
        return _build_line(build, now), None
    match status:
        case NodeStatus.PLANNED_BUILD:
            return Text(f"{TODO} {name}", style="blue"), None
        case NodeStatus.PLANNED_DOWNLOAD:
            return Text(f"{DOWN} {TODO} {name}", style="blue"), None
        case _:
            transfers = state.transfers_of(node)
            if transfers:
                return _transfers_line(name, transfers, now)
            return Text(name), None


def _hidden_summary(state: MonitorState, hidden: list[str]) -> Text:
    """nom's ``waiting for`` counts, over the inputs the tree has no room for."""
    counts = dict.fromkeys(NodeStatus, 0)
    for node in hidden:
        counts[state.node_status(node)] += 1
    parts = [
        (counts[NodeStatus.FAILED], WARNING, "red"),
        (counts[NodeStatus.BUILDING], RUNNING, "yellow"),
        (counts[NodeStatus.PLANNED_BUILD], TODO, "blue"),
        (counts[NodeStatus.UPLOADING], UP, "yellow"),
        (counts[NodeStatus.DOWNLOADING], DOWN, "yellow"),
        (counts[NodeStatus.PLANNED_DOWNLOAD], f"{DOWN} {TODO}", "blue"),
    ]
    text = Text()
    for count, glyph, style in parts:
        if count:
            text.append(f" {count} {glyph}", style=style)
    return Text(" waiting for", style="bright_black") + text if text else text


def _descendants(tree: dict[str, list[str]], node: str) -> list[str]:
    out: list[str] = []
    stack = list(tree.get(node, ()))
    while stack:
        child = stack.pop()
        out.append(child)
        stack.extend(tree.get(child, ()))
    return out


def _walk(roots: list[str], tree: dict[str, list[str]]) -> tuple[list[str], dict[str, str]]:
    """Every node in depth-first order, and each node's parent."""
    parent: dict[str, str] = {}
    order: list[str] = []
    stack = list(reversed(roots))
    while stack:
        node = stack.pop()
        order.append(node)
        for child in reversed(tree.get(node, [])):
            parent[child] = node
            stack.append(child)
    return order, parent


def _keep(state: MonitorState, roots: list[str], tree: dict[str, list[str]], limit: int) -> set[str]:
    """The nodes to draw: all work in flight, then planned, then finished, while room lasts.

    A node brings its ancestors, so every drawn node hangs from a root. Work
    in flight is drawn even past ``limit``, as nom draws every running build.
    """
    order, parent = _walk(roots, tree)
    status = {node: state.node_status(node) for node in order}
    keep: set[str] = set()

    def add(node: str | None) -> None:
        while node is not None and node not in keep:
            keep.add(node)
            node = parent.get(node)

    for node in order:
        if status[node] in _IN_FLIGHT:
            add(node)
    for wanted in (_PLANNED, frozenset(NodeStatus) - _IN_FLIGHT - _PLANNED - {NodeStatus.NONE}):
        for node in order:
            if len(keep) >= limit:
                return keep
            if status[node] in wanted:
                add(node)
    return keep


def _tree_lines(
    state: MonitorState, roots: list[str], tree: dict[str, list[str]], keep: set[str], now: float
) -> list[tuple[Text, float | None]]:
    """nom's ``showForest``: the root at the bottom and its inputs above it.

    Built top-down, as nom builds it, then reversed. The last child of a node
    therefore prints first, under ``┌─``.
    """

    def show(node: str) -> list[tuple[Text, float | None]]:
        line, part = _node_line(state, node, now)
        kept = [c for c in tree.get(node, []) if c in keep]
        hidden = [d for d in _descendants(tree, node) if d not in keep]
        if not kept and hidden and state.node_status(node) in _PLANNED:
            line += _hidden_summary(state, hidden)
        return [(line, part), *nested([show(c) for c in kept])]

    def nested(children: list[list[tuple[Text, float | None]]]) -> list[tuple[Text, float | None]]:
        out: list[tuple[Text, float | None]] = []
        for index, rows in enumerate(children):
            last = index == len(children) - 1
            first_prefix, rest_prefix = ("┌─ ", "   ") if last else ("├─ ", "│  ")
            for row, (line, part) in enumerate(rows):
                prefix = Text(first_prefix if row == 0 else rest_prefix, style="blue")
                out.append((prefix + line, part))
        return out

    lines: list[tuple[Text, float | None]] = []
    for root in roots:
        if root in keep:
            lines += show(root)
    return lines[::-1]


def _with_bars(rows: list[tuple[Text, float | None]], width: int) -> list[Text]:
    """Put each running copy's bar at one column, as nom's ``with_progress`` does."""
    left = max([60, *(line.cell_len + 1 for line, part in rows if part is not None)])
    out: list[Text] = []
    for line, part in rows:
        bar_length = width - left - 6
        if part is None or bar_length <= 0:
            out.append(line)
            continue
        with_bar = line + Text(" " * (left - line.cell_len)) + Text(_bar(bar_length, part))
        with_bar.append(f"{part * 100:5.1f}%", style="bold")
        out.append(with_bar)
    return out


def _graph_lines(state: MonitorState, now: float, limit: int, width: int) -> tuple[list[Text], int, int]:
    """The drawn tree, the roots it draws, and the roots there are."""
    roots, tree = state.forest()
    roots = [r for r in roots if state.node_status(r) != NodeStatus.NONE or tree.get(r)]
    keep = _keep(state, roots, tree, limit)
    lines = _with_bars(_tree_lines(state, roots, tree, keep, now), width)
    return lines, sum(r in keep for r in roots), len(roots)


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
    graph, shown_roots, all_roots = _graph_lines(state, now, limit, width - 2)
    table = _table(state, _time_text(state, now, finished_at))
    lines: list[Text] = []
    if graph:
        title = Text("Dependency Graph", style="bold")
        if all_roots > 1 and shown_roots == all_roots:
            title.append(f" with {all_roots} roots")
        elif all_roots > 1:
            title.append(f" showing {shown_roots} of {all_roots} roots")
        lines.append(Text(f"{UPPER_LEFT}{HORIZONTAL} ") + title + Text(":"))
        lines += [Text(f"{VERTICAL} ") + line for line in graph]
        lines.append(Text(LEFT_T) + table[0])
    else:
        lines.append(Text(UPPER_LEFT) + table[0])
    lines.append(table[1])
    out = Text("\n").join(lines)
    out.no_wrap = True
    out.overflow = "ellipsis"
    return out
