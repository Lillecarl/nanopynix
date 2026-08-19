"""What the installed program is allowed to import.

`pexpect` drives the shells in `test_support.shell_pty`, and it is a test
dependency: the Nix build lists it as a `nativeCheckInput`, so it is **not** in
the runtime closure of the installed application. Re-exporting `ShellSession`
from `completion_spike/__init__.py` therefore made `demo` fail to start outside
the check phase, and no test in this suite could see it -- pytest had `pexpect`
imported already.
"""

from __future__ import annotations

import subprocess
import sys

#: Blocks one module, then imports the program. `sys.modules[name] = None` is
#: the documented way to make an import of *name* raise, and it works whatever
#: the search path holds.
_PROGRAM = """
import sys
sys.modules["pexpect"] = None
import completion_spike
import completion_spike.demo
print("started")
"""


def test_the_program_starts_without_the_test_dependencies() -> None:
    result = subprocess.run(  # noqa: S603 -- this interpreter, and a literal script
        [sys.executable, "-c", _PROGRAM],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "started" in result.stdout
