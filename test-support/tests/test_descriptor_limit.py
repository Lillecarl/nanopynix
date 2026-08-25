"""The plugin keeps the process inside an ``fd_set``.

``select.select`` raises for a descriptor at or above ``FD_SETSIZE``, and Nix
2.35 sizes its directory-descriptor cache from ``RLIMIT_NOFILE``. Issue #271
is what that combination looks like from CI: a full-screen application that
cannot read a key, and a runner that dies after six silent minutes.

The subject here is the *arithmetic* of the pin, and not the syscall. Nix
computes ``min(4096, RLIMIT_NOFILE / 8)``, so the limit this plugin sets is
what decides whether the cache alone can fill an ``fd_set``.
"""

from __future__ import annotations

import resource

import pytest

from test_support import plugin

#: ``FD_SETSIZE`` on Linux. `select.select` raises at or above it.
FD_SETSIZE = 1024

#: The divisor in `getGlobalDirFdCacheLimit`, in Nix's
#: `src/libutil/posix-source-accessor.cc`.
NIX_DIR_FD_CACHE_DIVISOR = 8


def test_the_plugin_pins_the_soft_descriptor_limit() -> None:
    """Importing the plugin is what applies the limit, so it is already done."""
    soft, _hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    assert soft <= plugin.DESCRIPTOR_LIMIT, f"the soft limit is {soft}"


def test_the_limit_leaves_nix_a_cache_that_cannot_fill_an_fd_set() -> None:
    """The number is a choice, and this states what the choice has to satisfy.

    Nix would otherwise cache up to 4096 open directories, which is four
    ``fd_set``s. A cache that alone reaches 1024 breaks `select` whatever the
    rest of the process does.
    """
    cache = min(4096, plugin.DESCRIPTOR_LIMIT // NIX_DIR_FD_CACHE_DIVISOR)
    assert cache < FD_SETSIZE // 2, f"Nix would cache {cache} directories"


def test_a_zero_in_the_environment_leaves_the_limit_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    """A suite that needs every descriptor it can get says so, and is obeyed.

    The call is made again here rather than at import, because the import
    already happened. Setting the variable to ``0`` must make the second call
    a no-op, whatever the first one did.
    """
    monkeypatch.setenv(plugin.DESCRIPTOR_LIMIT_ENV_VAR, "0")
    before = resource.getrlimit(resource.RLIMIT_NOFILE)
    plugin.keep_the_process_inside_an_fd_set()
    assert resource.getrlimit(resource.RLIMIT_NOFILE) == before


def test_the_limit_only_ever_goes_down(monkeypatch: pytest.MonkeyPatch) -> None:
    """A number above the current soft limit must not raise it.

    Raising it would undo the pin for every later call, and the process would
    reach an `fd_set` again with nothing to say why.
    """
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    monkeypatch.setenv(plugin.DESCRIPTOR_LIMIT_ENV_VAR, str(soft + 1000))
    plugin.keep_the_process_inside_an_fd_set()
    assert resource.getrlimit(resource.RLIMIT_NOFILE) == (soft, hard)
