"""The LSP test harness of this suite: a client, drivers, markers, scenarios.

**This is a package, and the suite around it is not.** The suite has no
``__init__.py``, so pytest puts ``pynix/tests/`` on ``sys.path`` and each test
module takes its own bare name. That is what makes ``support`` importable as a
top-level package from every test module here, with no path work in any of
them.

Issue #130 moved these six modules out of ``tests/support/``. They are the only
part of that directory that names a pynix concept, and nothing outside this
suite reads them.
"""

from __future__ import annotations
