"""The path to Nix's ``get-env.sh`` that ``nanopynix-bindings`` ships.

``nanopynix-bindings`` carries ``get-env.sh`` because Nix compiles it into the
``nix`` binary and no library carries it. ``nanopynix`` re-exports the
accessor so that ``pynix`` can read it without importing a private module of
the bindings, which ``tests/meta/test_consumer_surface.py`` forbids.
"""

from __future__ import annotations

from nanopynix_bindings._get_env import get_env_sh_path as get_env_sh_path

__all__ = ["get_env_sh_path"]
