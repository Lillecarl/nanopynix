"""Installs beartype's import hook before any of our own packages are imported.

pytest-beartype normally installs this hook from its own `pytest_configure`
hookimpl (driven by pytest.ini's `beartype_packages` setting), but pytest
loads `tests/conftest.py` -- and therefore this module's own `import
nanopynix` et al. -- during initial-conftest collection, which happens
*before* any plugin's `pytest_configure` runs. By the time pytest-beartype's
hook would install, nanopynix/nanopynix_helpers/pynix/ekn are already
imported and unhookable, so it silently no-ops (emitting a
"not checkable by beartype" warning). Importing this module first -- an
ordinary import-time side effect, same pattern nanopynix_bindings itself uses
to install its C++ exception translator -- installs the hook early enough to
actually cover them.

`claw_is_pep526=False` disables beartype's runtime checking of annotated
*global variable* assignments (PEP 526), as opposed to function/method
signatures, which are unaffected and still fully checked. This codebase
relies on `from __future__ import annotations` (PEP 563) to defer annotation
evaluation, including forward references to classes defined later in the same
module for module-level globals (e.g. nanopynix/logging.py's
`ACTIVE_LOG_CAPTURES: ContextVar[tuple[LogCapture, ...]]`, declared ~200
lines above the `LogCapture` class it names). beartype.claw's PEP 526
instrumentation re-embeds the raw annotation expression as a live argument to
its own runtime-check call immediately after the assignment -- outside of any
annotation position -- so PEP 563's deferral doesn't apply to it and the
class name is looked up immediately, raising NameError for any such forward
reference regardless of whether the class is defined later in the file.
Disabling it trades that class of checks away to keep the rest of the
codebase's forward-reference style working. (Verified with `__pycache__`
cleared -- beartype's own source warns that a stale cached `.pyc` from a
differently-configured run will silently mask config changes here.)

nanopynix_bindings is deliberately excluded: its submodules are
nanobind-compiled .so extensions, not Python source, so there is no AST for
beartype's import hook to instrument. nanopynix_proto is excluded too: it's
betterproto2-generated code, not hand-written, so checking it would flag
generator quirks rather than our own bugs.

Sets `NANOPYNIX_BEARTYPING=1` before installing the hook -- see
nanopynix/_typechecking.py's module docstring for why this is an environment
variable rather than a Python attribute we set here directly, and for what it
does: modules across nanopynix/nanopynix_helpers/pynix/ekn that guard a
type-only import with `if TYPE_CHECKING or BEARTYPING:` promote it to a real
runtime import under this flag, so beartype has an actual object to check
those hints against instead of an unresolvable forward reference.
"""

from __future__ import annotations

import os

from beartype import BeartypeConf
from beartype.claw import beartype_packages

os.environ["NANOPYNIX_BEARTYPING"] = "1"

beartype_packages(
    ("nanopynix", "nanopynix_helpers", "pynix", "ekn"),
    conf=BeartypeConf(claw_is_pep526=False),
)
