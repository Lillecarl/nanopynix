"""The one fixture that a `-p` plugin cannot supply, and why.

Issue #130 moved this suite here from ``tests/nanopynix/``, and
``nanopynix/pytest.ini`` registers every plugin it needs with ``-p``. That file
says why ``pytest_plugins`` cannot be used in this conftest.

``-p`` costs one thing, and this file pays it. A plugin named that way is a
global plugin, at the same level as anyio's own. ``nanopynix_testing`` defines
``anyio_backend`` at session scope, and anyio defines it at module scope; when
both are global plugins, anyio's wins. Every session-scoped fixture that
requests it then fails with ``ScopeMismatch``, and each test behind it collapses
with an internal ``assert not self._finalizers`` that names nothing.

A fixture that a conftest imports is a fixture that conftest defines, and a
conftest beats any plugin. The import below is the whole repair.
"""

from __future__ import annotations

from nanopynix_testing.nix_environment import anyio_backend as anyio_backend
