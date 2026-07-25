"""Compatibility import for the worker's Nix-thread executor."""

from nanopynix._core._nix_executor import (
    NIX_EVALUATOR_STACK_SIZE as NIX_EVALUATOR_STACK_SIZE,
)
from nanopynix._core._nix_executor import (
    NixThreadExecutor as NixThreadExecutor,
)
