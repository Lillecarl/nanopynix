from .wire_string import WireString


class StorePath(WireString):
    """A store path — single string field."""

    path: str
