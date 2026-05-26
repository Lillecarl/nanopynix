"""Shared CLI helpers for pynixd subcommands."""

from __future__ import annotations

import logging

import structlog

from ..config import PynixdSettings

STRUCTLOG_PROCESSORS = [
    structlog.stdlib.filter_by_level,
    structlog.stdlib.add_logger_name,
    structlog.stdlib.add_log_level,
    structlog.contextvars.merge_contextvars,
    structlog.processors.TimeStamper(fmt="iso"),
    structlog.processors.StackInfoRenderer(),
    structlog.processors.format_exc_info,
    structlog.processors.JSONRenderer(),
]


def setup_logging(settings: PynixdSettings) -> None:
    log_level_str = settings.log_level.upper()

    logging.basicConfig(
        level=log_level_str,
        format="%(message)s",
    )

    structlog.configure(
        processors=STRUCTLOG_PROCESSORS,
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )


def load_settings() -> PynixdSettings:
    return PynixdSettings()
