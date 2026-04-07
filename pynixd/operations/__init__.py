"""Operation registry — auto-populated by OpRequest.__init_subclass__."""

# Import all operation modules to trigger __init_subclass__ registration
from . import (  # noqa: F401
    add_perm_root,
    builds,
    ca_derivations,
    collect_garbage,
    find_roots,
    maintenance,
    optimise_store,
    queries,
    set_options,
    store_mutations,
    verify_store,
)
from .base import OP_REGISTRY as OP_REGISTRY
