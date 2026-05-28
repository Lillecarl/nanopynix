"""Shared CLI helpers for pynixd subcommands."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import structlog

from ..config import PynixdSettings
from ..plugins import import_plugin

if TYPE_CHECKING:
    from collections.abc import Callable


def _scan_plugins_for_filter(plugins: list) -> Callable | None:
    """Import each plugin path and return the first callable named ``filter``.

    A plugin module may export a ``filter`` function (or a class instance
    with ``__call__``).  The callable receives
    ``(logger, method_name, event_dict)`` and should return the event_dict
    (pass through) or a falsy value (drop).

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


def build_processors(settings: PynixdSettings) -> list:
    """Build the structlog processor chain, injecting a user-defined filter
    processor if any plugin exports a ``filter`` callable.
    """
    processors: list = [
        structlog.stdlib.filter_by_level,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.contextvars.merge_contextvars,
    ]

    filter_fn = _scan_plugins_for_filter(settings.plugins)
    if filter_fn is not None:

        def _filter_processor(logger: object, method_name: str, event_dict: dict) -> dict:
            try:
                result = filter_fn(logger, method_name, event_dict)
            except Exception:
                return event_dict
            if not result:
                raise structlog.DropEvent
            return result

        processors.append(_filter_processor)

    processors.extend(
        [
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
    )
    return processors


def setup_logging(settings: PynixdSettings) -> None:
    log_level_str = settings.log_level.upper()

    logging.basicConfig(
        level=log_level_str,
        format="%(message)s",
    )

    structlog.configure(
        processors=build_processors(settings),
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )


def load_settings() -> PynixdSettings:
    return PynixdSettings()
