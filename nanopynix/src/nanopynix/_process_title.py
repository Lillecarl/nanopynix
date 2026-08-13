"""Process-title helpers for nanopynix managers and workers."""

from __future__ import annotations

import secrets

from setproctitle import setproctitle

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
