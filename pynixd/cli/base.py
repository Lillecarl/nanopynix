"""Shared CLI helpers for pynixd subcommands."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import structlog

from ..config import PynixdSettings
from ..plugins import import_plugin

if TYPE_CHECKING:
    from collections.abc import Callable


def _load_filter_from_plugins(plugins: list) -> Callable | None:
    """Import each plugin path and return the first callable named ``filter``.

    The callable receives ``(logger, method_name, event_dict)`` and should
    return the event_dict (pass through) or a falsy value (drop).
    Plugins that fail to import are skipped with a warning.
    """
    log = structlog.get_logger(__name__)
    for path in plugins:
        module = import_plugin(path)
        if module is None:
            continue
        if hasattr(module, "filter") and callable(module.filter):
            return module.filter
        if callable(module):
            return module  # type: ignore[return-value]
        log.warning("log_filter_plugin_missing_filter", path=str(path))
    return None


def _build_filter_processor(filter_fn: Callable) -> Callable:
    """Wrap a plugin filter callable as a structlog processor.

    Raises ``DropEvent`` when the filter drops the event, which
    structlog's native ``_proxy_to_logger`` understands as a signal
    to discard.
    """

    def _filter_processor(logger: object, method_name: str, event_dict: dict) -> dict:
        try:
            result = filter_fn(logger, method_name, event_dict)
        except structlog.DropEvent:
            raise
        except Exception:
            return event_dict
        if not result:
            raise structlog.DropEvent
        return result

    return _filter_processor


def setup_logging(settings: PynixdSettings) -> None:
    """Configure structlog and stdlib logging with plugin-defined filtering.

    Uses ``ProcessorFormatter`` to route standard-library log records
    (from asyncssh, libraries, and our own non-structlog code) through
    the same plugin filter that structlog uses.  Both paths produce
    identical JSON output.
    """
    log_level_str = settings.log_level.upper()
    filter_fn = _load_filter_from_plugins(settings.plugins)
    filter_proc = _build_filter_processor(filter_fn) if filter_fn else None

    # ── Structlog processor chain ─────────────────────────────────
    # ``wrap_for_formatter`` defers rendering to the ``ProcessorFormatter``
    # so that stdlib and structlog messages share the same renderer.
    structlog_processors: list = [
        structlog.stdlib.filter_by_level,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.contextvars.merge_contextvars,
    ]
    if filter_proc is not None:
        structlog_processors.append(filter_proc)
    structlog_processors.extend(
        [
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
    )

    structlog.configure(
        processors=structlog_processors,
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    # ── Stdlib bridge via ProcessorFormatter ──────────────────────
    # foreign_pre_chain converts logging.LogRecord → event_dict.
    # Our plugin filter is inserted so the same filtering applies to
    # both structlog and stdlib messages.
    foreign_pre_chain: list = [
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso"),
    ]

    formatter = structlog.stdlib.ProcessorFormatter(
        processor=structlog.processors.JSONRenderer(),
        foreign_pre_chain=foreign_pre_chain,
    )

    logging.basicConfig(
        level=log_level_str,
        handlers=[logging.StreamHandler()],
        force=True,
    )
    for handler in logging.getLogger().handlers:
        handler.setFormatter(formatter)


def load_settings() -> PynixdSettings:
    return PynixdSettings()
