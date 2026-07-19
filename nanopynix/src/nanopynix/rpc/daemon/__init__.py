"""Isolated native-Nix daemon supervisor and connection-worker roles.

The supervisor owns a Unix listener but deliberately does not initialise Nix.
Each accepted connection runs in a fresh process, where Nix owns the complete
daemon protocol exchange.
"""

from nanopynix.rpc.daemon._config import DaemonConfig as DaemonConfig
from nanopynix.rpc.daemon._supervisor import DaemonSupervisor as DaemonSupervisor

__all__ = ["DaemonConfig", "DaemonSupervisor"]
