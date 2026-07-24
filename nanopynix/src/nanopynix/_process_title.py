"""Process-title helpers for nanopynix managers and workers."""

from __future__ import annotations

from coolname import generate_slug  # type: ignore[reportMissingTypeStubs] -- coolname has no PEP 561 stubs
from setproctitle import setproctitle

_manager_project_name = "nanopynix"


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
    subname = generate_slug(2)
    set_process_title(subname, project_name="nanopynix")
    return subname
