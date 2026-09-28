"""The names the ``nanopynix_bindings`` engine answers that the bindings do not have.

Each one is a name ``_engine_huggorm`` needs, so ``nanopynix._engine`` offers
it from both engines.
"""

from __future__ import annotations

from typing import Any, cast


def error_detail(exc: BaseException) -> tuple[str, dict[str, Any] | None]:
    """Nix's own rendering of *exc*, and its ``nix::ErrorInfo`` as a dict.

    ``nix_error_info.hh`` attaches both to the exception it raises. A raise
    that could not build them carries neither.
    """
    raw: object = getattr(exc, "raw", "")
    info: object = getattr(exc, "info", None)
    return (
        raw if isinstance(raw, str) else "",
        cast("dict[str, Any]", info) if isinstance(info, dict) else None,
    )


def flush_logs() -> None:
    """Do nothing. The bindings call the log callback when Nix logs, so no record waits."""
