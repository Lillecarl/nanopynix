"""Shared exceptions for pynixd."""


class PynixdError(Exception):
    """Base for all pynixd errors."""


class InfrastructureError(PynixdError):
    """Transport/connection failure (SSH down, EOF, timeout)."""


class BackendUnavailableError(InfrastructureError):
    """Backend is in circuit breaker cooldown."""


class BuildFailureError(PynixdError):
    """Build-level failure with status code."""

    def __init__(self, status: int, drv_path: str, msg: str = ""):
        self.status = status
        self.drv_path = drv_path
        super().__init__(msg or f"Build failed (status={status}): {drv_path}")


class BackendError(PynixdError):
    """Raised when the backend sends STDERR_ERROR.

    The error has already been forwarded to the client.
    """


class ResourceExhaustedError(PynixdError):
    """Raised when system resources are too stressed to proceed (PSI/Load)."""


class OpNotImplementedError(PynixdError):
    """Raised when an operation is not implemented for a specific executor (e.g. DB)."""
