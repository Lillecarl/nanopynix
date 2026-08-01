"""Compatibility import for the worker's Nix-thread executor."""

from __future__ import annotations

from nanopynix._core._nix_executor import (
    NIX_EVALUATOR_STACK_SIZE as NIX_EVALUATOR_STACK_SIZE,
    NixThreadExecutor as NixThreadExecutor,
    abandoned_work_is_running as abandoned_work_is_running,
)
