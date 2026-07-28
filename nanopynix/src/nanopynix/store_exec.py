"""Running programs that live in a *relocated* Nix store.

A store opened with a root (``local://?root=/x``, ``unix:///sock?root=/x``)
reports ordinary logical ``/nix/store/...`` paths while the bytes live under
``/x/nix/store/...``. Resolving the physical path and exec'ing that does not
work: the ELF interpreter, DT_RUNPATH, shebangs, and every ``/nix/store/...``
string baked into the closure still name the *logical* directory. Making such
a closure runnable means putting the real store at the logical path, which is
what ``nix run`` does.

Nix's own implementation of that (``chrootHelper``, ``src/nix/run.cc``) cannot
be bound: it is in the ``nix`` CLI rather than any ``libnix*``, it is reached
by re-exec'ing the nix binary under a magic ``argv[0]``, it needs
``unshare(CLONE_NEWUSER)`` which fails in a multithreaded process like CPython,
and it ends in ``execvp``. So the capability ships as a small exec-final helper
binary (``tools/store-exec/store-exec.c``) and this module is the door onto it.
"""

from __future__ import annotations

import shutil
from typing import TYPE_CHECKING

from nanopynix._typechecking import BEARTYPING

# Not a plain `if TYPE_CHECKING:` block: beartype resolves this annotation at
# call time, and under a TYPE_CHECKING-only import it cannot -- it raises
# BeartypeCallHintForwardRefException instead of running the function.
if TYPE_CHECKING or BEARTYPING:
    from nanopynix.protocols import AsyncStore

#: The helper binary, resolved off ``PATH`` like the other tools this project
#: ships. Nix packaging puts it there (``storeExecTool`` in default.nix).
STORE_EXEC_TOOL = "nanopynix-store-exec"


async def store_exec_prefix(store: AsyncStore) -> list[str]:
    """Return an argv prefix that makes *store*'s paths runnable.

    Prepend it to any command that execs a binary out of *store*::

        prefix = await store_exec_prefix(store)
        await anyio.run_process([*prefix, f"{out}/bin/tofu", "version"])

    Empty for an ordinary store whose paths are already at their logical
    location, which is the common case -- so callers prepend it
    unconditionally rather than branching on the store's layout. It is worth
    caching per store (it costs a ``store_dirs()`` round trip), but it is
    stable for a store's lifetime.

    Also empty when the store exposes no local filesystem path at all (a
    plain remote or binary-cache store): there is nothing local to run from,
    and saying so by returning nothing keeps the caller's own error the one
    the user sees.

    Raises ``RuntimeError`` if the store *is* relocated but the helper is not
    installed -- deliberately loud. Silently returning an empty prefix there
    would reproduce the exact bug this exists to fix: an exec that fails with
    ``FileNotFoundError`` on a perfectly valid store path, which reads as a
    bug in whatever was being run rather than a missing tool.
    """
    dirs = await store.store_dirs()
    store_dir = dirs.store_dir.rstrip("/")
    real_store_dir = dirs.real_store_dir

    if real_store_dir is None:
        return []
    real_store_dir = real_store_dir.rstrip("/")
    if real_store_dir == store_dir:
        return []

    tool = shutil.which(STORE_EXEC_TOOL)
    if tool is None:
        raise RuntimeError(
            f"{STORE_EXEC_TOOL} is not on PATH, so programs cannot be run from the relocated store "
            f"at {real_store_dir} (its paths are reported as {store_dir}/...). "
            f"Install nanopynix's storeExecTool alongside the library."
        )
    return [tool, store_dir, real_store_dir]
