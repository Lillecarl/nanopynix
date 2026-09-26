"""The names the ``nanopynix_bindings`` engine answers that the bindings do not have.

Each one is a name ``_engine_huggorm`` needs, so ``nanopynix._engine`` offers
it from both engines.
"""

from __future__ import annotations


def flush_logs() -> None:
    """Do nothing. The bindings call the log callback when Nix logs, so no record waits."""
