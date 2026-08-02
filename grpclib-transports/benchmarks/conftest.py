from __future__ import annotations

import logging
import statistics
from typing import Any

from _bench_utils import bench_results, dump_paths, latency_results
from rich.console import Console
from rich.table import Table

logging.getLogger("h2").setLevel(logging.WARNING)
logging.getLogger("asyncssh").setLevel(logging.WARNING)

_console = Console(highlight=False)


def _bench_types() -> list[str]:
    preferred = ["small", "large"]
    seen = {r["type"] for r in bench_results}
    return [t for t in preferred if t in seen] + sorted(seen - set(preferred))


def _fmt_msgs(v: float) -> str:
    return f"{v:,.0f} msgs/s"


def _fmt_mb(v: float) -> str:
    return f"{v:,.2f} MB/s"


def _fmt_spread(sample_rates: list[float]) -> str:
    high = max(sample_rates)
    low = min(sample_rates)
    median = statistics.median(sample_rates)
    if median == 0:
        return "0.0%"
    return f"{(high - low) / median * 100:.1f}%"


def _fmt_ms(v: float) -> str:
    return f"{v:,.2f} ms"


def _build_throughput_table(test_type: str, rows: list[dict[str, Any]]) -> Table:
    transports = sorted({r["transport"] for r in rows})
    parallelisms = sorted({r["parallelism"] for r in rows})
    cols = ["Transport"] + [f"p={p}" for p in parallelisms]
    heading = f"{test_type.upper()} ({rows[0]['count']} x {rows[0]['payload_size']} B)"
    table = Table(*cols, title=heading)
    table.columns[0].no_wrap = True
    table.columns[0].min_width = max(len(t) for t in transports)
    for col in table.columns[1:]:
        col.no_wrap = True
    for t in transports:
        vals: list[str] = [t]
        for p in parallelisms:
            match = next((r for r in rows if r["transport"] == t and r["parallelism"] == p), None)
            if match:
                thr = _fmt_mb(match["mb_per_sec"]) if match["payload_size"] else _fmt_msgs(match["msgs_per_sec"])
                spread = _fmt_spread(match["sample_rates"])
                vals.append(f"{thr}\n{spread}")
            else:
                vals.append("N/A")
        table.add_row(*vals)
    return table


def pytest_terminal_summary(terminalreporter: Any, exitstatus: Any, config: Any) -> None:  # noqa: ARG001 -- pytest matches this hook by name, so `exitstatus` and `config` must be accepted even though the summary uses neither
    if not bench_results and not latency_results:
        if dump_paths:
            terminalreporter.section("Diagnostic Dumps", bold=True, yellow=True)
            for p in dump_paths:
                terminalreporter.write_line(f"  {p}")
        return

    if bench_results:
        mainstream = [r for r in bench_results if r["type"] in ("small", "large")]
        sweep = [r for r in bench_results if r["type"] not in ("small", "large")]

        for test_type in _bench_types():
            rows = [r for r in mainstream if r["type"] == test_type]
            if not rows:
                continue
            _console.print()
            _console.print(_build_throughput_table(test_type, rows))

        if sweep:
            _console.print()
            table = Table("Transport", "Throughput", "Spread", title="PAYLOAD SWEEP & STREAMING")
            for r in sorted(sweep, key=lambda r: (r["transport"], r["type"])):
                fmt = _fmt_mb if r["payload_size"] else _fmt_msgs
                val = r["mb_per_sec"] if r["payload_size"] else r["msgs_per_sec"]
                table.add_row(
                    f"{r['transport']}  {r['type']}",
                    fmt(val),
                    _fmt_spread(r["sample_rates"]),
                )
            _console.print(table)

    if latency_results:
        _console.print()
        table = Table("Transport", "median", "spread")
        for test_type in sorted({r["type"] for r in latency_results}):
            rows = [r for r in latency_results if r["type"] == test_type]
            heading = f"{test_type.upper()} ({rows[0]['count']} workers/sample)"
            table.add_section()
            table.columns[0].header = heading
            for row in sorted(rows, key=lambda r: r["transport"]):
                table.add_row(
                    row["transport"],
                    _fmt_ms(row["ms_per_op"]),
                    _fmt_spread(row["sample_ms_per_op"]),
                )
        _console.print(table)

    if dump_paths:
        terminalreporter.section("Diagnostic Dumps", bold=True, yellow=True)
        terminalreporter.write_line(f"  Dump directory: {dump_paths[0].parent}")
        terminalreporter.write_line("")
        for p in dump_paths:
            terminalreporter.write_line(f"  {p}")
