"""Operation registry — auto-populated by OpRequest.__init_subclass__."""

# Import all operation modules to trigger __init_subclass__ registration
from . import (  # noqa: F401
    builds,
    ca_derivations,
    maintenance,
    queries,
    set_options,
    store_mutations,
)
from .base import OP_REGISTRY as OP_REGISTRY
