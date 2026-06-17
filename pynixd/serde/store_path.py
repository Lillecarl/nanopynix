from pathlib import Path

from .wire_string import WireString


class StorePath(WireString):
    """A store path — single string field."""

    path: str

    def endswith(self, suffix: str) -> bool:
        return self.path.endswith(suffix)

    def hash_part(self) -> str:
        return Path(self.path).name.split("-", 1)[0]
