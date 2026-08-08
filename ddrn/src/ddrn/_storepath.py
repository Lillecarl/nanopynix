"""Store path computation, in pure Python.

A planner runs inside a build sandbox. It has no store, and no daemon, so it
cannot ask Nix where a path goes. This module computes the answer.

The functions here cover the fixed-output case only. That case is the one a
planner needs: it is the only case where the path follows from data that a
lock file already holds, and therefore the only case where a planner can name
a derivation that Nix never instantiated.

For every other case, do not compute the path. Let the Nix expression pass the
path in. See :mod:`ddrn.menu`, which is the supported way.

Each function here is checked against Nix itself by
``ddrn/tests/test_storepath.py``.
"""

from __future__ import annotations

import base64
import hashlib

#: The alphabet of Nix, which is not RFC 4648 base32.
BASE32_ALPHABET = "0123456789abcdfghijklmnpqrsvwxyz"

#: The width of a store path hash, in bytes, after compression.
STORE_HASH_BYTES = 20


def nix_base32(data: bytes) -> str:
    """Encode ``data`` the way Nix encodes a store path hash.

    Nix reads the bit stream from the most significant end, so the output is
    the reverse of a conventional base32 encoding.
    """
    length = (len(data) * 8 - 1) // 5 + 1
    out: list[str] = []
    for n in range(length - 1, -1, -1):
        bit = n * 5
        byte, offset = divmod(bit, 8)
        value = data[byte] >> offset
        if byte + 1 < len(data):
            value |= data[byte + 1] << (8 - offset)
        out.append(BASE32_ALPHABET[value & 0x1F])
    return "".join(out)


def compress_hash(data: bytes, size: int) -> bytes:
    """Fold ``data`` down to ``size`` bytes, as ``nix::compressHash`` does."""
    out = bytearray(size)
    for index, byte in enumerate(data):
        out[index % size] ^= byte
    return bytes(out)


def make_store_path(store_dir: str, path_type: str, inner: bytes, name: str) -> str:
    """The store path that Nix derives from a type, an inner hash and a name.

    ``path_type`` is the fingerprint prefix of Nix, such as ``"source"`` or
    ``"output:out"``. ``inner`` is a raw SHA-256 digest.
    """
    fingerprint = f"{path_type}:sha256:{inner.hex()}:{store_dir}:{name}"
    digest = compress_hash(hashlib.sha256(fingerprint.encode()).digest(), STORE_HASH_BYTES)
    return f"{store_dir}/{nix_base32(digest)}-{name}"


def make_fixed_output_path(
    store_dir: str,
    name: str,
    *,
    sha256: str,
    recursive: bool,
    references: list[str] | None = None,
) -> str:
    """The output path of a fixed-output derivation.

    ``sha256`` is the hash of the content, in hexadecimal. ``recursive``
    selects NAR ingestion, which Nix writes as ``r:sha256`` and which a
    directory needs; a single downloaded file is flat.

    ``recursive`` has no default on purpose. The two modes give different
    paths for the same hash, Nix rejects the derivation when the path and the
    mode disagree, and the message names neither. Prefer
    :meth:`ddrn.Output.fixed`, which sets both from one argument.

    Nix takes two different routes here, and this reproduces both. A recursive
    SHA-256 output uses the ``source`` fingerprint, which is the same one that
    ``addToStore`` uses for a plain NAR. Every other combination uses the
    ``output:out`` fingerprint over a ``fixed:out:`` recipe string.
    """
    refs = references or []
    digest = bytes.fromhex(sha256)
    if len(digest) != hashlib.sha256().digest_size:
        raise ValueError(f"{name}: expected a SHA-256 hash of 64 hexadecimal characters, got {len(sha256)}")

    if recursive:
        return make_store_path(store_dir, ":".join(["source", *sorted(refs)]), digest, name)

    if refs:
        raise ValueError(f"{name}: a flat fixed-output path carries no references")
    recipe = f"fixed:out:sha256:{sha256}:"
    return make_store_path(store_dir, "output:out", hashlib.sha256(recipe.encode()).digest(), name)


def sri_to_hex(value: str) -> str:
    """Convert a hash to hexadecimal, from SRI, from ``sha256:``, or unchanged.

    A lock file writes a hash in whichever of these three spellings its own
    tooling prefers, and a planner should not care which one it reads.
    """
    text = value.strip()
    if text.startswith("sha256-"):
        return base64.b64decode(text.removeprefix("sha256-")).hex()
    text = text.removeprefix("sha256:")
    bytes.fromhex(text)  # Reject anything that is not hexadecimal by now.
    return text
