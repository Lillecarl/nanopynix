from .wire_string import WireString


class NARHash(WireString):
    """Base16-encoded NAR SHA256 hash — no algorithm prefix on wire."""

    hash: str
