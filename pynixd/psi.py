"""PSI (Pressure Stall Information) and system stats data model and parsing.

Reads /proc/pressure/{cpu,memory,io} and /proc/meminfo to gauge system load
and available resources on Linux backends.

Future directions:
- Event-driven PSI: instead of polling, run a persistent SSH process that
  opens /proc/pressure/* with O_RDWR, writes trigger thresholds (e.g.
  "some 150000 1000000"), and poll()s on the fds. On trigger fire, read
  current PSI state and print to stdout. This would let us gate builder
  admission instantly when a backend starts stalling. A python3 one-liner
  on the remote end is the simplest approach since shell can't do poll().
- Derivation attributes could specify resource requirements (min memory,
  cpu count, etc.) to filter eligible builders before scheduling.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from environs import Env

env = Env()


@dataclass
class PsiMetric:
    """One resource from /proc/pressure/{cpu,memory,io}."""

    some_avg10: float = 0.0
    some_avg60: float = 0.0
    some_avg300: float = 0.0
    full_avg10: float = 0.0
    full_avg60: float = 0.0
    full_avg300: float = 0.0


@dataclass
class PsiWeights:
    """Configurable weights for pressure score calculation."""

    cpu: float = 0.4
    mem: float = 0.3
    io: float = 0.2
    memfull: float = 0.1

    @classmethod
    def from_env(cls) -> PsiWeights:
        """Parse PYNIXD_PSI_WEIGHTS='cpu=0.4,mem=0.3,io=0.2,memfull=0.1'."""
        raw = env.str("PYNIXD_PSI_WEIGHTS", "")
        if not raw:
            return cls()
        kv = dict(part.split("=") for part in raw.split(","))
        return cls(**{k: float(v) for k, v in kv.items()})


PSI_WEIGHTS = PsiWeights.from_env()


@dataclass
class PsiSnapshot:
    """Complete PSI state for a host."""

    cpu: PsiMetric = field(default_factory=PsiMetric)
    memory: PsiMetric = field(default_factory=PsiMetric)
    io: PsiMetric = field(default_factory=PsiMetric)
    timestamp: float = 0.0

    def pressure_score(self, w: PsiWeights = PSI_WEIGHTS) -> float:
        """0.0 = idle, 100.0 = fully stalled.

        Weighted combination of pressure metrics, configurable via
        PYNIXD_PSI_WEIGHTS env var (default: cpu=0.4,mem=0.3,io=0.2,memfull=0.1).
        """
        return (
            w.cpu * self.cpu.some_avg10
            + w.mem * self.memory.some_avg10
            + w.io * self.io.some_avg10
            + w.memfull * self.memory.full_avg10
        )


@dataclass
class MemInfo:
    """Parsed subset of /proc/meminfo (values in kB)."""

    mem_total: int = 0
    mem_available: int = 0
    swap_total: int = 0
    swap_free: int = 0

    @property
    def available_mb(self) -> int:
        return self.mem_available // 1024

    @property
    def total_mb(self) -> int:
        return self.mem_total // 1024


def parse_meminfo(text: str) -> MemInfo:
    """Parse /proc/meminfo output into MemInfo."""
    fields: dict[str, int] = {}
    for line in text.splitlines():
        if ":" not in line:
            continue
        key, rest = line.split(":", 1)
        # Values are in kB, strip the unit
        parts = rest.split()
        if parts:
            try:
                fields[key.strip()] = int(parts[0])
            except ValueError:
                pass
    return MemInfo(
        mem_total=fields.get("MemTotal", 0),
        mem_available=fields.get("MemAvailable", 0),
        swap_total=fields.get("SwapTotal", 0),
        swap_free=fields.get("SwapFree", 0),
    )


def parse_psi_line(line: str) -> tuple[str, dict[str, float]]:
    """Parse 'some avg10=X avg60=Y avg300=Z total=N' -> ('some', {avg10: X, ...})"""
    parts = line.split()
    if not parts:
        raise ValueError(f"Empty PSI line: {line!r}")
    kind = parts[0]  # "some" or "full"
    values: dict[str, float] = {}
    for part in parts[1:]:
        if "=" in part:
            k, v = part.split("=", 1)
            if k != "total":
                values[k] = float(v)
    return kind, values


def parse_psi_output(text: str) -> PsiSnapshot:
    """Parse concatenated output of cat /proc/pressure/{cpu,memory,io}.

    Expected format (6 or 7 lines — cpu has no 'full' line on older kernels):
        some avg10=X avg60=Y avg300=Z total=N   <- cpu some
        [full avg10=X avg60=Y avg300=Z total=N]  <- cpu full (kernel 5.13+)
        some avg10=X avg60=Y avg300=Z total=N   <- memory some
        full avg10=X avg60=Y avg300=Z total=N   <- memory full
        some avg10=X avg60=Y avg300=Z total=N   <- io some
        full avg10=X avg60=Y avg300=Z total=N   <- io full
    """
    lines = [line.strip() for line in text.strip().splitlines() if line.strip()]

    cpu = PsiMetric()
    memory = PsiMetric()
    io = PsiMetric()

    # Parse lines sequentially — cpu first, then memory, then io.
    # CPU may have 1 or 2 lines, memory always 2, io always 2.
    idx = 0

    def apply(metric: PsiMetric, kind: str, values: dict[str, float]) -> None:
        prefix = f"{kind}_"
        for k, v in values.items():
            attr = prefix + k
            if hasattr(metric, attr):
                setattr(metric, attr, v)

    # CPU: always starts with "some", optionally followed by "full"
    if idx < len(lines):
        kind, vals = parse_psi_line(lines[idx])
        apply(cpu, kind, vals)
        idx += 1
    if idx < len(lines) and lines[idx].startswith("full"):
        kind, vals = parse_psi_line(lines[idx])
        apply(cpu, kind, vals)
        idx += 1

    # Memory: some then full
    if idx < len(lines):
        kind, vals = parse_psi_line(lines[idx])
        apply(memory, kind, vals)
        idx += 1
    if idx < len(lines):
        kind, vals = parse_psi_line(lines[idx])
        apply(memory, kind, vals)
        idx += 1

    # IO: some then full
    if idx < len(lines):
        kind, vals = parse_psi_line(lines[idx])
        apply(io, kind, vals)
        idx += 1
    if idx < len(lines):
        kind, vals = parse_psi_line(lines[idx])
        apply(io, kind, vals)
        idx += 1

    return PsiSnapshot(
        cpu=cpu,
        memory=memory,
        io=io,
        timestamp=time.monotonic(),
    )
