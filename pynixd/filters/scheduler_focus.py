"""Drop noisy debug/trace logs for scheduler-focused benchmarking.

Keeps: build lifecycle, scheduler, decomposition, resolution events,
errors, warnings, and all non-debug level messages.
Drops: SSH channel lifecycle, per-operation trace, SQL queries.
"""

KEEP_EVENTS = frozenset(
    {
        "build_enqueued",
        "build_derivation_enqueued",
        "build_completed",
        "build_failed",
        "build_fasttracked_local",
        "build_assigned_to_store",
        "build_sending_inputs",
        "build_executing",
        "build_executed",
        "pulling_paths",
        "pulled_paths_into_local_store",
        "scheduler_started",
        "scheduler_request_resolved",
        "scheduler_request_resolved_with_failure",
        "store_added_to_scheduler",
        "store_probed",
        "store_reconnected",
        "systems_probed",
        "features_probed",
        "resolved_derivation",
        "registering_dep_realisation_on_builder",
        "register_dep_realisation_failed",
        "trampoline_detected",
        "trampoline_build_enqueued",
        "trampoline_drv_not_found",
        "dynamic_dep_linked",
        "unix_client_connected",
        "client_protocol_negotiated",
        "client_handshake_complete",
    },
)

KEEP_LOGGERS = frozenset(
    {
        "pynixd.scheduler",
        "pynixd.decomposer",
        "pynixd.trampoline",
        "pynixd.derivation_resolver",
        "pynixd.build_queue",
        "pynixd.proxy",
        "pynixd.unix_server",
        "pynixd.store.base",
        "pynixd.operations.probe_systems",
        "pynixd.operations.probe_features",
        "pynixd.operations.build_paths",
    },
)

KEEP_LOGGERS_DEBUG = frozenset(
    {
        "pynixd.scheduler",
        "pynixd.decomposer",
        "pynixd.derivation_resolver",
        "pynixd.build_queue",
    },
)

ALWAYS_KEEP_LEVELS = frozenset({"error", "critical"})


def filter(logger, method_name, event_dict):  # noqa: A001 — plugin convention
    level = event_dict.get("level", "").lower()
    if level in ALWAYS_KEEP_LEVELS or method_name in ALWAYS_KEEP_LEVELS:
        return event_dict

    if level not in ("debug", "info", "trace"):
        return event_dict

    if event_dict.get("event") in KEEP_EVENTS:
        return event_dict

    logger_name = getattr(logger, "name", "")
    if logger_name in KEEP_LOGGERS_DEBUG:
        return event_dict
    if logger_name in KEEP_LOGGERS and method_name != "debug":
        return event_dict

    return None
