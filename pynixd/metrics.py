"""Prometheus metrics for pynixd build orchestration.

Provides Gauges, Counters, and Histograms to monitor queue depth,
store load, and build throughput. These are exposed via the
PynixdHttpServer's /metrics endpoint.
"""

from __future__ import annotations

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    REGISTRY,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)

# --- Queue Metrics ---

QUEUE_SIZE = Gauge(
    "pynixd_build_queue_size",
    "Number of builds currently in the queue",
    ["status"],  # pending, building, done
)

BUILD_DURATION = Histogram(
    "pynixd_build_duration_seconds",
    "Time spent actively building (excluding queue wait time)",
    buckets=(1, 5, 15, 30, 60, 120, 300, 600, 1200, 3600),
)

QUEUE_WAIT_DURATION = Histogram(
    "pynixd_build_queue_wait_duration_seconds",
    "Time spent in the queue before a build task starts",
    buckets=(1, 5, 10, 30, 60, 120, 300),
)

BUILDS_COMPLETED = Counter(
    "pynixd_builds_completed_total",
    "Total number of builds completed",
    ["status"],  # success, failure
)

# --- Store Metrics ---

STORE_CPU_UTILIZATION = Gauge(
    "pynixd_store_cpu_utilization_percent",
    "Reported CPU utilization of a backend store",
    ["store_id"],
)

STORE_AVAILABLE_SLOTS = Gauge(
    "pynixd_store_available_slots",
    "Current number of available build slots on a backend store",
    ["store_id"],
)

STORE_HEALTHY = Gauge(
    "pynixd_store_healthy",
    "Health status of a backend store (1 = healthy, 0 = unhealthy)",
    ["store_id"],
)

# --- Transfer Metrics ---

PATHS_TRANSFERRED = Counter(
    "pynixd_paths_transferred_total",
    "Number of paths moved between stores",
    ["source", "destination"],
)


def get_metrics_response() -> tuple[bytes, str]:
    """Generate a Prometheus-formatted metrics response."""
    return generate_latest(REGISTRY), CONTENT_TYPE_LATEST
