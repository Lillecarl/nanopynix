"""Primop specs for the ipaddress module."""

from __future__ import annotations

from nanopynix.models import PrimOpSpec as PrimOpSpec


def ipaddress_primops() -> list[PrimOpSpec]:
    return [
        PrimOpSpec(
            name="parseAddress",
            arity=1,
            args=["source"],
            doc=(
                'Parse an IP address string (e.g. ``"192.168.1.1"``) into an attrset\n'
                "with properties like ``version``, ``isPrivate``, ``isGlobal``, etc."
            ),
            import_path="nanopynix.primops._ipaddress_impl:parse_address",
        ),
        PrimOpSpec(
            name="parseNetwork",
            arity=1,
            args=["source"],
            doc=(
                'Parse a network string (e.g. ``"192.168.1.0/24"``) into an attrset\n'
                "with ``prefixlen``, ``numAddresses``, callable ``hosts`` and ``subnets``\n"
                "methods, and is* property checks."
            ),
            import_path="nanopynix.primops._ipaddress_impl:parse_network",
        ),
        PrimOpSpec(
            name="parseInterface",
            arity=1,
            args=["source"],
            doc=(
                'Parse an interface string (e.g. ``"192.168.1.1/24"``) into an attrset\n'
                "with ``ip`` and ``network`` sub-attrsets."
            ),
            import_path="nanopynix.primops._ipaddress_impl:parse_interface",
        ),
    ]
