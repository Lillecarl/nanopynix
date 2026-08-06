"""Pytest markers for Nix bugs that make a test's subject unusable.

These are not "this test is flaky" escapes -- each one names an upstream
defect, the Nix versions that carry it, and the issue to check before
widening or dropping the exclusion.
"""

from __future__ import annotations

import pytest

NIX_GC_ROOTS_BUG = pytest.mark.nix_version(
    exclude=("2.31", "2.34"),
    reason="findRoots/collectGarbage crash on nonnumeric temproots filenames; https://github.com/NixOS/nix/issues/16138",
)
"""``findRoots``/``collectGarbage`` abort instead of returning.

Nix creates temp-root files whose names are not PIDs, then parses every
temp-root filename with ``std::stoi``. Any test that reaches either call on
an affected version dies on the parse, not on anything the test did.
"""

NIX_CONF_FILE_IGNORED = pytest.mark.nix_version(
    minimum="2.34",
    reason="Nix 2.31 reads NIX_USER_CONF_FILES when libstore loads, which is before `Session(nix_conf=...)` can set it",
)
"""``Session(nix_conf=...)`` sets ``NIX_USER_CONF_FILES`` too late for Nix 2.31.

**The two versions read the variable at different moments**, which the source
of each states. In 2.31, ``globals.cc`` reads it while it constructs the
global ``Settings`` object::

    Settings settings;                                    // globals.cc:49
        , nixUserConfFiles(getUserConfigFiles())           // globals.cc:66

``settings`` is at namespace scope, so that constructor runs at static
initialisation -- when ``libnixstore`` loads, which is when the bindings are
imported. ``loadConfFile`` later reads the stored ``settings.nixUserConfFiles``
(globals.cc:125) and never looks at the environment again.

2.34 moved the read behind a function with a local static, and
``loadConfFile`` calls it::

    auto files = nixUserConfFiles();                       // globals.cc:143
    static const std::vector<...> files = [] {             // globals.cc:165
        auto nixConfFiles = getEnvOs(OS_STR("NIX_USER_CONF_FILES"));

So 2.34 reads it on the first ``loadConfFile``, which ``init_libstore`` calls
after the worker sets the variable, and 2.31 has already read it by then. The
worker cannot set it earlier: the forkserver preloads
``nanopynix.rpc.worker._worker``, so the bindings load before a child exists.

Measured, with one unambiguous key, the same file and only the linked Nix
different::

    file says `max-jobs = 42`   ->   2.31 reports 4 (the host), 2.34 reports 42

A test that gives a session the configuration of a pretend host therefore
tests nothing on 2.31: the session reads the real ``nix.conf`` of whatever
machine runs the suite. This marks the test, and not the library, because a
fix would have to set the variable process-wide before the first import, which
is the opposite of a per-session setting.
"""
