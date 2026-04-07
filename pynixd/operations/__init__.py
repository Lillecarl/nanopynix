"""Operation registry — auto-populated by OpRequest.__init_subclass__."""

# Import all operation modules to trigger __init_subclass__ registration
from . import (  # noqa: F401
    add_build_log,
    add_indirect_root,
    add_multiple_to_store,
    add_perm_root,
    add_signatures,
    add_temp_root,
    add_to_store,
    add_to_store_nar,
    build_derivation,
    build_paths,
    build_paths_with_results,
    builds,
    ca_derivations,
    collect_garbage,
    ensure_path,
    find_roots,
    maintenance,
    optimise_store,
    queries,
    set_options,
    store_mutations,
    verify_store,
)
from .base import OP_REGISTRY as OP_REGISTRY
