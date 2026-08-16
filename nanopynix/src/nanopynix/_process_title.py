"""Process-title helpers for nanopynix managers and workers."""

from __future__ import annotations

import secrets
import sys

# The mirror of the note on ``_discard_process_title`` below. pyright reads
# ``sys.platform`` statically, so on macOS the branch that names this import is
# the dead one and ``reportUnusedImport`` fires. The import is used, on every
# platform that is not macOS.
from setproctitle import setproctitle as _setproctitle  # pyright: ignore[reportUnusedImport] -- see the note below


# pyright evaluates ``sys.platform`` statically, so it reads the selection
# below as one branch and not as two. Off macOS the branch that names this
# function is dead code, the only access to the name goes with it, and
# ``reportUnusedFunction`` fires. The function is used, on the platform that
# it exists for.
def _discard_process_title(title: str) -> None:  # pyright: ignore[reportUnusedFunction] -- see the note above
    """Ignore the title. See ``setproctitle`` below for why, on macOS."""


#: What actually sets the title, and a no-op on macOS.
#:
#: setproctitle's Darwin backend rewrites the name ``ps`` shows by going
#: through private CoreFoundation bundle APIs, and that path faults on current
#: macOS::
#:
#:     darwin_set_process_title
#:       CFBundleGetFunctionPointerForName
#:         _CFBundleLoadExecutableAndReturnError
#:           os_log_type_enabled
#:             _os_log_preferences_refresh   EXC_BAD_ACCESS / SIGSEGV
#:
#: That killed every rpc worker at startup, so `ekn eval` of a one-line
#: attribute died with SIGSEGV before evaluating anything -- and it looked like
#: an evaluator or GC problem, because the client only ever reports that its
#: worker was signalled.
#:
#: **A no-op is the whole fix, because the title has no reader.** This module's
#: own note above says it is "a name that only a person reading ``ps`` ever
#: sees", and `set_worker_title` returns the slug separately for the one caller
#: that keeps it.
#:
#: Swapped here rather than branched inside `set_process_title`, so that
#: function stays one line and the tests -- which monkeypatch this name --
#: exercise identical code on either platform.
setproctitle = _discard_process_title if sys.platform == "darwin" else _setproctitle

_manager_project_name = "nanopynix"

#: The first word of a worker slug, and the second.
#:
#: **These two lists replace the `coolname` dependency.** That package built the
#: same two-word slug, and it cost 13.7 ms and five modules of every
#: `import nanopynix`, for a name that only a person reading `ps` ever sees. It
#: also ships no PEP 561 stubs, so the import carried a type-checker
#: suppression. Issue #108 removed all three.
#:
#: **The slug needs no uniqueness, and this pair gives none.** Nothing reads
#: the name back: `set_worker_title` returns it, `_state.py` keeps it as
#: `worker_subname`, and `_worker.py` writes it into the title until a store
#: opens and the store URIs replace it. The pid tells two workers apart when
#: two slugs collide.
#:
#: One string that splits, and not a list of strings. `ruff format` puts each
#: element of a list on its own line, which turns these two declarations into
#: 86 lines of one word each. The split runs once, when the module loads.
_ADJECTIVES = (  # noqa: SIM905 -- a list literal here is 45 lines of one word each after `ruff format`
    "amber brisk calm clever cobalt crisp dusky eager fair fleet gentle glad "
    "grave hardy hollow idle keen lucid mellow merry mild noble olive patient "
    "placid prompt quiet rapid russet silent slender solemn spry stark steady "
    "still sunny swift tawny tidy vivid warm wary wise witty"
).split()

_NOUNS = (  # noqa: SIM905 -- a list literal here is 41 lines of one word each after `ruff format`
    "alder badger beacon birch brook cedar cinder comet coral crane delta "
    "ember falcon fern fjord harbor heron kestrel lantern larch linnet marten "
    "meadow mesa otter pebble pika quarry raven ridge rowan sable shale "
    "sparrow spruce summit thistle thrush tundra vireo willow"
).split()


def generate_slug() -> str:
    """Return a two-word slug, as ``adjective-noun``."""
    return f"{secrets.choice(_ADJECTIVES)}-{secrets.choice(_NOUNS)}"


def set_process_title(subname: str, *, project_name: str | None = None) -> None:
    """Set the current process title to ``projectname (subname)``."""
    setproctitle(f"{project_name or _manager_project_name} ({subname})")


def set_manager_title(project_name: str | None = None) -> None:
    """Set the manager title, optionally selecting a project name for this process."""
    global _manager_project_name  # noqa: PLW0603 -- one-shot process-wide title override, set once at manager startup
    if project_name is not None:
        _manager_project_name = project_name
    set_process_title("manager")


def set_worker_title() -> str:
    """Identify a nanopynix worker with a short, distinct name."""
    subname = generate_slug()
    set_process_title(subname, project_name="nanopynix")
    return subname
