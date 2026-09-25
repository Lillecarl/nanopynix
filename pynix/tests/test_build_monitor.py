"""The nom-style monitor, fed the events a tracked session sends."""

from __future__ import annotations

from typing import Any

from nanopynix import ActivityType, LogEvent, ResultType
from pynix._build_monitor import BuildPlan, MonitorState, format_bytes, format_duration, render, store_path_name

DRV = "/nix/store/zrrcdd2k8m57ciadr6rkm56vww6pbjhw-hello-2.12.drv"
OTHER_DRV = "/nix/store/6lf31wv62nchj9347w6p1nkcrywb4k20-broken-1.0.drv"
GLIBC = "/nix/store/kjrhpqxjackdmrxwc40afcz98rci2n57-glibc-2.40"
CACHE = "https://cache.nixos.org"

BUILDS_ID, COPIES_ID, BUILD_ID, COPY_ID, FAIL_ID = 1, 2, 3, 4, 5


def start(activity_id: int, activity_type: ActivityType, text: str = "", fields: list[Any] | None = None) -> LogEvent:
    return LogEvent(request_id=1, action="start", args=[activity_id, 3, int(activity_type), text, fields or [], 0])


def stop(activity_id: int) -> LogEvent:
    return LogEvent(request_id=1, action="stop", args=[activity_id])


def result(activity_id: int, result_type: ResultType, fields: list[Any]) -> LogEvent:
    return LogEvent(request_id=1, action="result", args=[activity_id, int(result_type), fields])


def error(message: str) -> LogEvent:
    return LogEvent(request_id=1, action="error", args=[0, message, {}])


def feed(state: MonitorState, events: list[tuple[float, LogEvent]]) -> list[str]:
    printed: list[str] = []
    for now, event in events:
        printed += [line.plain for line in state.apply(event, now)]
    return printed


def running_build(state: MonitorState) -> list[str]:
    return feed(
        state,
        [
            (0.0, start(BUILDS_ID, ActivityType.BUILDS)),
            (0.0, start(COPIES_ID, ActivityType.COPY_PATHS)),
            (0.0, result(BUILDS_ID, ResultType.PROGRESS, [0, 3, 0, 0])),
            (0.0, result(COPIES_ID, ResultType.PROGRESS, [0, 2, 0, 0])),
            (0.5, start(COPY_ID, ActivityType.COPY_PATH, f"copying path '{GLIBC}'", [GLIBC, CACHE, "local"])),
            (0.5, result(COPIES_ID, ResultType.PROGRESS, [0, 2, 1, 0])),
            (1.0, result(COPY_ID, ResultType.PROGRESS, [10 * 1024 * 1024, 40 * 1024 * 1024, 0, 0])),
            (2.0, start(BUILD_ID, ActivityType.BUILD, f"building '{DRV}'", [DRV, "", 1, 1])),
            (2.0, result(BUILDS_ID, ResultType.PROGRESS, [0, 3, 1, 0])),
            (2.5, result(BUILD_ID, ResultType.SET_PHASE, ["buildPhase"])),
            (3.0, result(BUILD_ID, ResultType.BUILD_LOG_LINE, ["compiling hello.c"])),
        ],
    )


def test_names_and_units_match_nom() -> None:
    assert store_path_name(DRV) == "hello-2.12"
    assert store_path_name(GLIBC) == "glibc-2.40"
    assert format_duration(5.9) == "5s"
    assert format_duration(65.0) == "1m05s"
    assert format_duration(3725.0) == "1h02m05s"
    assert format_bytes(512) == "512.0B"
    assert format_bytes(40 * 1024 * 1024) == "40.0MiB"


def test_a_running_build_and_download_are_drawn_with_totals() -> None:
    state = MonitorState(started=0.0)
    running_build(state)

    lines = render(state, 12.0, width=100, height=30).plain.splitlines()

    # No plan, so each build and copy is a root; nom draws the first at the bottom.
    assert lines[0] == "┏━ Dependency Graph with 2 roots:"
    assert lines[1].startswith("┃ ↓ ⏵ glibc-2.40 ⏱ 11s 10.0MiB/40.0MiB")
    assert lines[1].endswith(" 25.0%")
    assert lines[2] == "┃ ⏵ hello-2.12 (buildPhase) ⏱ 10s"
    assert lines[3] == "┣━━━ Builds          │ Downloads"
    assert lines[4] == "┗━ ∑ ⏵ 1 │ ✔ 0 │ ⏸ 2 │ ↓ 1 │ ↓ 0 │ ⏸ 1 │ ⏱ 12s"
    assert lines[3].index("│") == lines[4].index("│", 16), "a header must span its three columns exactly"


def test_finished_work_turns_green_and_the_summary_plans_nothing() -> None:
    state = MonitorState(started=0.0)
    running_build(state)
    feed(
        state,
        [
            (4.0, stop(COPY_ID)),
            (6.0, stop(BUILD_ID)),
            # Nix's last update after one build: done 1, and expected still 2.
            (6.0, result(BUILDS_ID, ResultType.PROGRESS, [1, 2, 0, 0])),
            (6.1, stop(BUILDS_ID)),
            (6.1, stop(COPIES_ID)),
        ],
    )

    lines = render(state, 6.2, width=100, height=30, finished_at="12:00:00").plain.splitlines()

    assert lines[1] == "┃ ↓ ✔ glibc-2.40 40.0MiB ⏱ 3s"
    assert lines[2] == "┃ ✔ hello-2.12 ⏱ 4s"
    assert lines[-1].startswith("┗━ ∑ ⏵ 0 │ ✔ 1 │ ⏸ 0 │ ↓ 0 │ ↓ 1 │ ⏸ 0 │ ")
    assert lines[-1].endswith("Finished at 12:00:00 after 6s")


def test_an_error_naming_a_derivation_fails_its_build() -> None:
    state = MonitorState(started=0.0)
    feed(
        state,
        [
            (0.0, start(FAIL_ID, ActivityType.BUILD, "", [OTHER_DRV, "", 1, 1])),
            (0.0, result(FAIL_ID, ResultType.SET_PHASE, ["checkPhase"])),
            (3.0, stop(FAIL_ID)),
        ],
    )
    printed = feed(state, [(3.0, error(f"Cannot build '{OTHER_DRV}'.\nReason: builder failed with exit code 1."))])

    lines = render(state, 4.0, width=100, height=30, finished_at="12:00:00").plain.splitlines()

    assert printed[0].startswith("Cannot build")
    assert lines[1] == "┃ ⚠ broken-1.0 failed after ⏱ 3s in checkPhase"
    assert lines[-1].endswith("⚠ Exited after 1 build failures at 12:00:00 after 4s")


def test_a_failed_build_is_read_from_the_summary_when_nix_logs_no_error() -> None:
    """The order a real failing build sends, measured: no ``error`` event at all."""
    state = MonitorState(started=0.0)
    feed(
        state,
        [
            (0.0, start(BUILDS_ID, ActivityType.BUILDS)),
            (0.0, result(BUILDS_ID, ResultType.PROGRESS, [0, 1, 0, 0])),
            (0.0, start(FAIL_ID, ActivityType.BUILD, "", [OTHER_DRV, "", 1, 1])),
            (0.0, result(BUILDS_ID, ResultType.PROGRESS, [0, 1, 1, 0])),
            (2.0, result(BUILDS_ID, ResultType.PROGRESS, [0, 1, 0, 1])),
            (2.0, stop(FAIL_ID)),
            (2.0, stop(BUILDS_ID)),
        ],
    )

    lines = render(state, 2.5, width=100, height=30, finished_at="12:00:00").plain.splitlines()

    assert lines[1] == "┃ ⚠ broken-1.0 failed after ⏱ 2s"
    assert lines[-1].startswith("┗━ ∑ ⏵ 0 │ ✔ 0 │ ⏸ 0 │ ")
    assert lines[-1].endswith("⚠ Exited after 1 build failures at 12:00:00 after 2s")


def test_a_success_after_a_failure_stays_a_success() -> None:
    state = MonitorState(started=0.0)
    feed(
        state,
        [
            (0.0, start(BUILDS_ID, ActivityType.BUILDS)),
            (0.0, start(FAIL_ID, ActivityType.BUILD, "", [OTHER_DRV, "", 1, 1])),
            (0.0, start(BUILD_ID, ActivityType.BUILD, "", [DRV, "", 1, 1])),
            (1.0, result(BUILDS_ID, ResultType.PROGRESS, [0, 2, 1, 1])),
            (1.0, stop(FAIL_ID)),
            (2.0, result(BUILDS_ID, ResultType.PROGRESS, [1, 2, 0, 1])),
            (2.0, stop(BUILD_ID)),
        ],
    )

    assert state.builds[FAIL_ID].failed
    assert not state.builds[BUILD_ID].failed


def test_build_log_lines_print_only_when_asked() -> None:
    quiet = MonitorState(started=0.0)
    loud = MonitorState(started=0.0, print_build_logs=True)

    assert "hello-2.12> compiling hello.c" not in running_build(quiet)
    assert "hello-2.12> compiling hello.c" in running_build(loud)


def test_activity_text_prints_above_the_monitor() -> None:
    printed = running_build(MonitorState(started=0.0))
    assert printed == [f"copying path '{GLIBC}'", f"building '{DRV}'"]


def test_finished_work_keeps_to_a_third_of_the_terminal() -> None:
    state = MonitorState(started=0.0)
    feed(
        state,
        [(0.0, start(10 + i, ActivityType.BUILD, "", [f"/nix/store/{'a' * 32}-p{i}.drv", "", 1, 1])) for i in range(20)]
        + [(1.0, stop(10 + i)) for i in range(20)],
    )

    lines = render(state, 1.5, width=100, height=15).plain.splitlines()

    assert sum(line.startswith("┃ ✔") for line in lines) == 5
    assert lines[0] == "┏━ Dependency Graph showing 5 of 20 roots:"


def test_every_running_build_is_drawn_past_the_limit() -> None:
    """nom never hides work in flight to save a line."""
    state = MonitorState(started=0.0)
    feed(
        state,
        [
            (0.0, start(10 + i, ActivityType.BUILD, "", [f"/nix/store/{'a' * 32}-p{i}.drv", "", 1, 1]))
            for i in range(20)
        ],
    )

    lines = render(state, 0.5, width=100, height=15).plain.splitlines()

    assert sum(line.startswith("┃ ⏵") for line in lines) == 20


def plan(*, children: dict[str, tuple[str, ...]], builds: set[str], downloads: dict[str, str]) -> BuildPlan:
    """A plan rooted at ``ROOT``; ``downloads`` maps a node to its one output path."""
    outputs = {node: frozenset({f"{node.removesuffix('.drv')}-out"}) for node in children}
    outputs |= {node: frozenset({path}) for node, path in downloads.items()}
    return BuildPlan(
        roots=(ROOT,),
        children=children,
        outputs=outputs,
        builds=frozenset(builds),
        downloads=frozenset(downloads.values()),
    )


ROOT = "/nix/store/00000000000000000000000000000000-root.drv"
MID = "/nix/store/11111111111111111111111111111111-mid.drv"
LEFT = "/nix/store/22222222222222222222222222222222-left.drv"
RIGHT = "/nix/store/33333333333333333333333333333333-right.drv"
DEP = "/nix/store/44444444444444444444444444444444-dep.drv"
DEP_OUT = "/nix/store/55555555555555555555555555555555-dep"

GRAPH = plan(
    children={ROOT: (MID,), MID: (LEFT, RIGHT, DEP), LEFT: (), RIGHT: ()},
    builds={ROOT, MID, LEFT, RIGHT},
    downloads={DEP: DEP_OUT},
)


def test_the_graph_is_drawn_upside_down_as_nom_draws_it() -> None:
    """The root at the bottom, its inputs above it, the last input under ``┌─``.

    Inputs sort by state, work in flight first, so reversed it is the nearest
    to its parent and finished work drifts to the top.
    """
    state = MonitorState(started=0.0, plan=GRAPH)
    feed(
        state,
        [
            (0.0, start(COPY_ID, ActivityType.COPY_PATH, "", [DEP_OUT, CACHE, "local"])),
            (1.0, stop(COPY_ID)),
            (1.0, start(BUILD_ID, ActivityType.BUILD, "", [LEFT, "", 1, 1])),
        ],
    )

    lines = render(state, 3.0, width=100, height=30).plain.splitlines()

    assert lines[:6] == [
        "┏━ Dependency Graph:",
        "┃    ┌─ ↓ ✔ dep",
        "┃    ├─ ⏸ right",
        "┃    ├─ ⏵ left ⏱ 2s",
        "┃ ┌─ ⏸ mid",
        "┃ ⏸ root",
    ]
    # The plan, not Nix's summary: three builds and no download still to do.
    assert lines[-1] == "┗━ ∑ ⏵ 1 │ ✔ 0 │ ⏸ 3 │ ↓ 0 │ ↓ 1 │ ⏸ 0 │ ⏱ 3s"


def test_a_pruned_input_is_summarised_on_its_planned_parent() -> None:
    """With room for two lines, the running build and its ancestors win."""
    state = MonitorState(started=0.0, plan=GRAPH)
    feed(state, [(0.0, start(BUILD_ID, ActivityType.BUILD, "", [MID, "", 1, 1]))])

    lines = render(state, 0.5, width=100, height=6).plain.splitlines()

    assert lines[:4] == [
        "┏━ Dependency Graph:",
        "┃ ┌─ ⏵ mid",
        "┃ ⏸ root",
        "┣━━━ Builds          │ Downloads",
    ]


def test_a_planned_leaf_says_what_it_waits_for() -> None:
    state = MonitorState(started=0.0, plan=GRAPH)

    lines = render(state, 0.5, width=100, height=6).plain.splitlines()

    assert lines[1] == "┃ ┌─ ⏸ mid waiting for 2 ⏸ 1 ↓ ⏸"


def test_a_download_the_plan_names_but_no_node_owns_is_only_counted() -> None:
    orphan = "/nix/store/66666666666666666666666666666666-runtime-dep"
    state = MonitorState(started=0.0, plan=BuildPlan((ROOT,), {ROOT: ()}, {}, frozenset({ROOT}), frozenset({orphan})))
    feed(state, [(0.0, start(COPY_ID, ActivityType.COPY_PATH, "", [orphan, CACHE, "local"]))])

    lines = render(state, 0.5, width=100, height=30).plain.splitlines()

    assert "runtime-dep" not in "\n".join(lines)
    assert lines[-1].startswith("┗━ ∑ ⏵ 0 │ ✔ 0 │ ⏸ 1 │ ↓ 1 │")


def test_a_local_copy_is_neither_a_download_nor_an_upload() -> None:
    state = MonitorState(started=0.0)
    feed(state, [(0.0, start(COPY_ID, ActivityType.COPY_PATH, "", [GLIBC, "local", "daemon"]))])
    assert state.transfers == {}


def test_nothing_to_show_is_a_bare_timer() -> None:
    lines = render(MonitorState(started=0.0), 2.0, width=80, height=None).plain.splitlines()
    assert lines == ["┏━━━ ", "┗━ ∑ ⏱ 2s"]
