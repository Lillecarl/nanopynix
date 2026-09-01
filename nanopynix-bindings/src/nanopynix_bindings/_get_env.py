"""The path to Nix's ``get-env.sh`` that this package ships.

This file is Nix's own ``src/nix/get-env.sh``. Nix compiles it into the
``nix`` binary as the file-static string ``getEnvSh``
(``src/nix/develop.cc``), so it lives in no library and a consumer of the
libraries has to carry it. The Nix build copies it into this package from the
Nix source of the version it links, so no copy sits in the source tree. This
package is the one that carries it, because it is the one that links libnix.

``nanopynix`` re-exports it so that ``pynix`` and any other consumer can read
it without importing a private implementation module of the bindings.
"""

from __future__ import annotations

from pathlib import Path


def get_env_sh_path() -> Path:
    """Return the installed ``get-env.sh``.

    The file sits beside this module, so ``__file__`` is what finds it. The
    build places it there, so no caller needs ``importlib.resources`` or a
    store path.
    """
    return Path(__file__).with_name("get-env.sh")
