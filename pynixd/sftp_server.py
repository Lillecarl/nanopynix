"""
SFTP server that only exposes PSI/cgroup monitoring files.

Allows remote pynixd instances to read resource pressure metrics
over SFTP while restricting access to everything else.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

import asyncssh
from anyio import Path as AsyncPath

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

_ALLOWED_PATHS: frozenset[str] = frozenset(
    {
        "/sys/fs/cgroup/cpu.pressure",
        "/sys/fs/cgroup/memory.pressure",
        "/sys/fs/cgroup/io.pressure",
        "/sys/fs/cgroup/cpu.max",
        "/sys/fs/cgroup/cpu.stat",
        "/sys/fs/cgroup/memory.current",
        "/sys/fs/cgroup/memory.max",
        "/proc/pressure/cpu",
        "/proc/pressure/memory",
        "/proc/pressure/io",
        "/proc/stat",
        "/proc/meminfo",
    }
)

_ALLOWED_DIRS: frozenset[str] = frozenset(
    {str(Path(p).parent) for p in _ALLOWED_PATHS}
    | {
        "/sys/fs/cgroup",
        "/sys/fs",
        "/sys",
        "/proc/pressure",
        "/proc",
    }
)


class PSIMonitorSFTPServer(asyncssh.SFTPServer):
    """SFTP server that only serves PSI/cgroup monitoring files.

    All read operations (open, read, stat, scandir) are restricted to
    the specific paths in _ALLOWED_PATHS and their parent directories.
    Write operations always fail with permission denied.
    """

    def _resolve(self, path: bytes) -> str:
        return os.path.realpath(path.decode())

    def _is_allowed_path(self, path: str) -> bool:
        return path in _ALLOWED_PATHS or path in _ALLOWED_DIRS

    def _check_allowed(self, path: bytes) -> None:
        if not self._is_allowed_path(self._resolve(path)):
            raise asyncssh.SFTPError(asyncssh.FX_NO_SUCH_FILE, "Access denied")

    def open(self, path: bytes, pflags: int, attrs: asyncssh.SFTPAttrs) -> object:
        self._check_allowed(path)
        if pflags & (asyncssh.FXF_WRITE | asyncssh.FXF_CREAT | asyncssh.FXF_TRUNC):
            raise asyncssh.SFTPError(asyncssh.FX_PERMISSION_DENIED, "Read only")
        return super().open(path, pflags, attrs)

    def stat(self, path: bytes):
        self._check_allowed(path)
        return super().stat(path)

    def lstat(self, path: bytes):
        self._check_allowed(path)
        return super().lstat(path)

    async def scandir(self, path: bytes) -> AsyncIterator[asyncssh.SFTPName]:
        self._check_allowed(path)
        parent = Path(self._resolve(path))
        async for entry in super().scandir(path):
            entry_name = entry.filename.decode() if isinstance(entry.filename, bytes) else entry.filename  # type: ignore[reportAttributeAccessIssue]
            entry_path = str(await (AsyncPath(parent) / entry_name).resolve())
            if self._is_allowed_path(entry_path):
                yield entry

    def setstat(self, path: bytes, attrs: asyncssh.SFTPAttrs) -> None:
        raise asyncssh.SFTPError(asyncssh.FX_PERMISSION_DENIED, "Read only")

    def fsetstat(self, file_obj: object, attrs: asyncssh.SFTPAttrs) -> None:
        raise asyncssh.SFTPError(asyncssh.FX_PERMISSION_DENIED, "Read only")

    def remove(self, path: bytes) -> None:
        raise asyncssh.SFTPError(asyncssh.FX_PERMISSION_DENIED, "Read only")

    def mkdir(self, path: bytes, attrs: asyncssh.SFTPAttrs) -> None:
        raise asyncssh.SFTPError(asyncssh.FX_PERMISSION_DENIED, "Read only")

    def rmdir(self, path: bytes) -> None:
        raise asyncssh.SFTPError(asyncssh.FX_PERMISSION_DENIED, "Read only")

    def rename(self, oldpath: bytes, newpath: bytes) -> None:
        raise asyncssh.SFTPError(asyncssh.FX_PERMISSION_DENIED, "Read only")

    def symlink(self, oldpath: bytes, newpath: bytes) -> None:
        raise asyncssh.SFTPError(asyncssh.FX_PERMISSION_DENIED, "Read only")

    def write(self, file_obj: object, offset: int, data: bytes) -> int:
        raise asyncssh.SFTPError(asyncssh.FX_PERMISSION_DENIED, "Read only")

    def link(self, oldpath: bytes, newpath: bytes) -> None:
        raise asyncssh.SFTPError(asyncssh.FX_PERMISSION_DENIED, "Read only")

    def lock(self, file_obj: object, offset: int, length: int, flags: int) -> None:
        raise asyncssh.SFTPError(asyncssh.FX_PERMISSION_DENIED, "Read only")

    def unlock(self, file_obj: object, offset: int, length: int) -> None:
        raise asyncssh.SFTPError(asyncssh.FX_PERMISSION_DENIED, "Read only")
