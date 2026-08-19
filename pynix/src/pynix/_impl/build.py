"""The fixed-output rebuild loop, reached when ``pynix build`` runs.

This module re-exports two names and defines nothing. That is its whole job:
it is the boundary that keeps ``nanopynix_helpers.build`` out of a start that
does not build anything.

``nanopynix_helpers`` says in its own docstring that it stays out of the core
of nanopynix "to avoid pulling in dependencies (e.g. tree-sitter) that most
nanopynix consumers don't need", and ``pynix build`` imported it at the top of
the module anyway. Measured on the release build, issue #123: 112.2 ms, nearly
all of it ``tree_sitter_nix``, which imports ``tree_sitter_config``, which
imports ``email_validator`` for one ``EmailStr`` field.

``pynix._impl`` says how the two halves reach each other.
"""

from __future__ import annotations

from nanopynix_helpers.build import (
    FodBuildError as FodBuildError,
    build_with_fod_update as build_with_fod_update,
)
