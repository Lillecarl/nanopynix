"""The one definition of nanopynix's beartype instrumentation.

Two places install it, and neither can import the other:
`nanopynix_testing.beartype_hook` (covering the pytest process) and the
`sitecustomize.py` beside this file (covering freshly exec'd subprocesses).
They must agree on the package list and the config -- an instrumented parent
with an uninstrumented worker child is exactly the asymmetry this module
exists to prevent -- so the definition lives here rather than in either one.

This directory is put on `PYTHONPATH` rather than being a package, because
`sitecustomize` has to be importable by the `site` module at interpreter
startup, long before anything has arranged `sys.path`. That is also why the
import beside it is a bare `import beartype_bootstrap` rather than a dotted
one, and why this directory holds no `__init__.py` although it ships inside
an installed package.
"""

from __future__ import annotations

import os
import sys

# nanopynix_bindings is deliberately absent: its submodules are
# nanobind-compiled .so extensions, not Python source, so there is no AST for
# beartype's import hook to instrument. nanopynix_proto is absent too -- it is
# betterproto2-generated code, not hand-written, so checking it would flag
# generator quirks rather than our own bugs.
#
# `pynix_lsp` is here because issue #107 moved 14 modules out of `pynix`, and
# a package that this tuple does not name is not instrumented at all. The
# split would otherwise have taken the checks off those modules in silence.
PACKAGES = ("nanopynix", "nanopynix_helpers", "libpynix", "pynix", "pynix_lsp")

ENV_VAR = "NANOPYNIX_BEARTYPING"


def install() -> None:
    """Set ``NANOPYNIX_BEARTYPING`` and install beartype's import hook.

    Order matters: the environment variable has to be set before the hook,
    and the hook before any of :data:`PACKAGES` is imported. See
    ``nanopynix/_typechecking.py`` for what the variable does -- modules
    guarding a type-only import with ``if TYPE_CHECKING or BEARTYPING:``
    promote it to a real runtime import under it, so beartype has an actual
    object to check those hints against instead of an unresolvable forward
    reference.

    ``claw_is_pep526=False`` disables beartype's runtime checking of annotated
    *global variable* assignments (PEP 526); function and method signatures
    are unaffected and still fully checked. This codebase relies on
    ``from __future__ import annotations`` (PEP 563) to defer annotation
    evaluation, including forward references to classes defined later in the
    same module for module-level globals (e.g. nanopynix/logging.py's
    ``ACTIVE_LOG_CAPTURES: ContextVar[tuple[LogCapture, ...]]``, declared
    ~200 lines above the ``LogCapture`` class it names). beartype.claw's PEP
    526 instrumentation re-embeds the raw annotation expression as a live
    argument to its own runtime-check call immediately after the assignment --
    outside of any annotation position -- so PEP 563's deferral does not apply
    to it and the class name is looked up immediately, raising NameError for
    any such forward reference regardless of whether the class is defined
    later in the file. Disabling it trades that class of checks away to keep
    the rest of the codebase's forward-reference style working. (Verified with
    ``__pycache__`` cleared -- beartype's own source warns that a stale cached
    ``.pyc`` from a differently-configured run will silently mask config
    changes here.)
    """
    os.environ[ENV_VAR] = "1"

    if sys.version_info >= (3, 15):  # beartype 0.22.9 hook dies on 3.15 source_to_code 4-arg change
        return

    # Deferred deliberately, and this is the one place in the repo where that
    # is not a code smell: importing beartype is what arms the hook, and the
    # environment variable above has to be set before any instrumented module
    # observes it. A top-level import here would also make `sitecustomize`
    # pull beartype into every subprocess the suite spawns, instrumented or
    # not, at interpreter startup.
    from beartype import BeartypeConf  # noqa: PLC0415 -- must follow the env var above
    from beartype.claw import beartype_packages  # noqa: PLC0415 -- must follow the env var above

    beartype_packages(PACKAGES, conf=BeartypeConf(claw_is_pep526=False))
