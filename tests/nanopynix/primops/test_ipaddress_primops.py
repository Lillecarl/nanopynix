"""Tests for ipaddress primops (parseAddress, parseNetwork, parseInterface)."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from nanopynix.primops import ipaddress_primops
from nanopynix.rpc import Session

if TYPE_CHECKING:
    from nanopynix.models import JsonValue


def _as_dict(v: JsonValue) -> dict[str, JsonValue]:
    assert isinstance(v, dict)
    return v


# parseNetwork deliberately returns Nix *functions* alongside its data --
# `.address n` and `.subnet n d` are part of its documented interface -- and
# parseInterface nests a network attrset under `.network`.
#
# Nix will not convert a function to JSON, so neither will to_python(). That is
# the right answer rather than a regression: the previous deep conversion kept
# them as callable proxies over rpc but produced the useless *string*
# "function" for them in-process, so the two engines disagreed about what this
# very value converted to.
#
# So the whole attrset has no single Python form, and that is not a defect to
# design around -- `nix eval --json` says the same. What a caller reaches for
# instead is `as_dict()`: one level, data leaves and function leaves side by
# side, each read or called on its own. See
# test_the_callables_are_reachable_through_as_dict below, and the same shape
# exercised for both engines in tests/nanopynix/test_engine_parity_semantics.py.
#
# This helper stays because these tests assert the *data* wholesale, which is
# what removeAttrs is for.
_DROP_CALLABLES = """
  let strip = a: builtins.removeAttrs a [ "address" "subnet" ];
  in x: if x ? network then x // { network = strip x.network; } else strip x
"""


async def _data_only(value: object) -> JsonValue:
    return await (await value.apply(_DROP_CALLABLES)).to_python()  # type: ignore[reportAttributeAccessIssue] -- ValueProxy, untyped in this test module


@pytest.mark.anyio
async def test_the_callables_are_reachable_through_as_dict():
    """The shape to_python() refuses is still fully usable, one level at a time.

    This is the answer to "our own primop returns something our own flagship
    conversion rejects": nothing about the primop needs changing, because
    as_dict() hands back the data leaves and the function leaves together and
    lets the caller pick. No removeAttrs, no knowing which keys are functions.
    """
    async with (
        Session(primops=ipaddress_primops()) as session,
        session.store() as store,
        session.eval(store) as eval,
    ):
        network = await eval.string('builtins.parseNetwork "192.168.1.0/24"')
        entries = await network.as_dict()

        # A data leaf converts on its own, even though its siblings cannot.
        assert await entries["prefixlen"].to_python() == 24
        assert await entries["numAddresses"].to_python() == 256

        # ...and a function leaf is callable rather than an error. `apply`
        # applies its argument to the receiver, so this is `address 5`.
        index = await eval.string("5")
        assert await (await index.apply(entries["address"])).to_python() == "192.168.1.5"


@pytest.mark.anyio
async def test_parse_address_v4():
    async with (
        Session(primops=ipaddress_primops()) as session,
        session.store() as store,
        session.eval(store) as eval,
    ):
        v = await eval.string('builtins.parseAddress "192.168.1.1"')
        result = await v.to_python()
        assert result == {
            "version": 4,
            "compressed": "192.168.1.1",
            "exploded": "192.168.1.1",
            "isPrivate": True,
            "isGlobal": False,
            "isMulticast": False,
            "isLoopback": False,
            "isLinkLocal": False,
            "isReserved": False,
            "isUnspecified": False,
            "maxPrefixlen": 32,
            "reversePointer": "1.1.168.192.in-addr.arpa",
            "isBroadcast": False,
        }


@pytest.mark.anyio
async def test_parse_address_v6():
    async with (
        Session(primops=ipaddress_primops()) as session,
        session.store() as store,
        session.eval(store) as eval,
    ):
        v = await eval.string('builtins.parseAddress "2a00:1450:4001:830::200e"')
        result = await v.to_python()
        assert result == {
            "version": 6,
            "compressed": "2a00:1450:4001:830::200e",
            "exploded": "2a00:1450:4001:0830:0000:0000:0000:200e",
            "isPrivate": False,
            "isGlobal": True,
            "isMulticast": False,
            "isLoopback": False,
            "isLinkLocal": False,
            "isReserved": False,
            "isUnspecified": False,
            "maxPrefixlen": 128,
            "reversePointer": ("e.0.0.2.0.0.0.0.0.0.0.0.0.0.0.0.0.3.8.0.1.0.0.4.0.5.4.1.0.0.a.2.ip6.arpa"),
            "isSiteLocal": False,
            "ipv4Mapped": None,
        }


@pytest.mark.anyio
async def test_parse_network_v4():
    async with (
        Session(primops=ipaddress_primops()) as session,
        session.store() as store,
        session.eval(store) as eval,
    ):
        v = await eval.string('builtins.parseNetwork "192.168.1.0/24"')
        result = _as_dict(await _data_only(v))
        assert result["version"] == 4
        assert result["prefixlen"] == 24
        assert result["numAddresses"] == 256
        assert result["networkAddress"] == "192.168.1.0"
        assert result["broadcastAddress"] == "192.168.1.255"
        assert result["netmask"] == "255.255.255.0"
        assert result["isPrivate"] is True
        assert result["isGlobal"] is False


@pytest.mark.anyio
async def test_parse_network_v6():
    async with (
        Session(primops=ipaddress_primops()) as session,
        session.store() as store,
        session.eval(store) as eval,
    ):
        v = await eval.string('builtins.parseNetwork "2a00:1450::/32"')
        result = _as_dict(await _data_only(v))
        assert result["version"] == 6
        assert result["prefixlen"] == 32
        assert result["isSiteLocal"] is False
        assert isinstance(result["numAddresses"], int)
        assert result["numAddresses"] > 0


@pytest.mark.anyio
async def test_parse_interface():
    async with (
        Session(primops=ipaddress_primops()) as session,
        session.store() as store,
        session.eval(store) as eval,
    ):
        v = await eval.string('builtins.parseInterface "192.168.1.1/24"')
        result = _as_dict(await _data_only(v))
        assert result["withPrefixlen"] == "192.168.1.1/24"
        assert result["withNetmask"] == "192.168.1.1/255.255.255.0"
        ip = _as_dict(result["ip"])
        assert ip["version"] == 4
        assert ip["compressed"] == "192.168.1.1"
        assert ip["isPrivate"] is True
        net = _as_dict(result["network"])
        assert net["version"] == 4
        assert net["prefixlen"] == 24
        assert net["networkAddress"] == "192.168.1.0"


@pytest.mark.anyio
async def test_network_address():
    """net.address n → nth address (0-indexed, includes network address)."""
    async with (
        Session(primops=ipaddress_primops()) as session,
        session.store() as store,
        session.eval(store) as eval,
    ):
        a0 = await eval.string('(builtins.parseNetwork "192.168.1.0/30").address 0')
        assert await a0.as_string() == "192.168.1.0"  # network
        a1 = await eval.string('(builtins.parseNetwork "192.168.1.0/30").address 1')
        assert await a1.as_string() == "192.168.1.1"


@pytest.mark.anyio
async def test_network_address_negative():
    """net.address (-1) → broadcast address for IPv4."""
    async with (
        Session(primops=ipaddress_primops()) as session,
        session.store() as store,
        session.eval(store) as eval,
    ):
        a = await eval.string('(builtins.parseNetwork "192.168.1.0/24").address (-1)')
        assert await a.as_string() == "192.168.1.255"


@pytest.mark.anyio
async def test_network_subnet():
    """net.subnet n diff → nth subnet when splitting by diff."""
    async with (
        Session(primops=ipaddress_primops()) as session,
        session.store() as store,
        session.eval(store) as eval,
    ):
        s0 = await eval.string('(builtins.parseNetwork "192.168.1.0/24").subnet 0 1')
        assert await s0.as_string() == "192.168.1.0/25"
        s1 = await eval.string('(builtins.parseNetwork "192.168.1.0/24").subnet 1 1')
        assert await s1.as_string() == "192.168.1.128/25"


@pytest.mark.anyio
async def test_network_subnet_ipv6():
    """Indexed subnet access works for IPv6 too."""
    async with (
        Session(primops=ipaddress_primops()) as session,
        session.store() as store,
        session.eval(store) as eval,
    ):
        s = await eval.string('(builtins.parseNetwork "2001:db8::/32").subnet 0 16')
        assert await s.as_string() == "2001:db8::/48"


@pytest.mark.anyio
async def test_loopback_address():
    async with (
        Session(primops=ipaddress_primops()) as session,
        session.store() as store,
        session.eval(store) as eval,
    ):
        v = await eval.string('builtins.parseAddress "127.0.0.1"')
        result = _as_dict(await v.to_python())
        assert result["isLoopback"] is True
        assert result["isGlobal"] is False
        assert result["version"] == 4


@pytest.mark.anyio
async def test_multicast_address():
    async with (
        Session(primops=ipaddress_primops()) as session,
        session.store() as store,
        session.eval(store) as eval,
    ):
        v = await eval.string('builtins.parseAddress "224.0.0.1"')
        result = _as_dict(await v.to_python())
        assert result["isMulticast"] is True
