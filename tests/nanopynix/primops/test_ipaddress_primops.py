"""Tests for ipaddress primops (parseAddress, parseNetwork, parseInterface)."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from nanopynix import NixType, Session
from nanopynix.primops import ipaddress_primops

if TYPE_CHECKING:
    from nanopynix.rpc.client._session import NixDeepValue


pytestmark = pytest.mark.nix_version(minimum="2.32")


def _as_dict(v: NixDeepValue) -> dict[str, NixDeepValue]:
    assert isinstance(v, dict)
    return v


@pytest.mark.anyio
async def test_parse_address_v4():
    async with (
        Session(primops=ipaddress_primops()) as session,
        session.store() as store,
        session.eval(store) as eval,
    ):
        v = await eval.string('builtins.parseAddress "192.168.1.1"')
        result = await v.force_deep()
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
        result = await v.force_deep()
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
        result = _as_dict(await v.force_deep())
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
        result = _as_dict(await v.force_deep())
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
        result = _as_dict(await v.force_deep())
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
        assert await a0.force_as(NixType.STRING) == "192.168.1.0"  # network
        a1 = await eval.string('(builtins.parseNetwork "192.168.1.0/30").address 1')
        assert await a1.force_as(NixType.STRING) == "192.168.1.1"


@pytest.mark.anyio
async def test_network_address_negative():
    """net.address (-1) → broadcast address for IPv4."""
    async with (
        Session(primops=ipaddress_primops()) as session,
        session.store() as store,
        session.eval(store) as eval,
    ):
        a = await eval.string('(builtins.parseNetwork "192.168.1.0/24").address (-1)')
        assert await a.force_as(NixType.STRING) == "192.168.1.255"


@pytest.mark.anyio
async def test_network_subnet():
    """net.subnet n diff → nth subnet when splitting by diff."""
    async with (
        Session(primops=ipaddress_primops()) as session,
        session.store() as store,
        session.eval(store) as eval,
    ):
        s0 = await eval.string('(builtins.parseNetwork "192.168.1.0/24").subnet 0 1')
        assert await s0.force_as(NixType.STRING) == "192.168.1.0/25"
        s1 = await eval.string('(builtins.parseNetwork "192.168.1.0/24").subnet 1 1')
        assert await s1.force_as(NixType.STRING) == "192.168.1.128/25"


@pytest.mark.anyio
async def test_network_subnet_ipv6():
    """Indexed subnet access works for IPv6 too."""
    async with (
        Session(primops=ipaddress_primops()) as session,
        session.store() as store,
        session.eval(store) as eval,
    ):
        s = await eval.string('(builtins.parseNetwork "2001:db8::/32").subnet 0 16')
        assert await s.force_as(NixType.STRING) == "2001:db8::/48"


@pytest.mark.anyio
async def test_loopback_address():
    async with (
        Session(primops=ipaddress_primops()) as session,
        session.store() as store,
        session.eval(store) as eval,
    ):
        v = await eval.string('builtins.parseAddress "127.0.0.1"')
        result = _as_dict(await v.force_deep())
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
        result = _as_dict(await v.force_deep())
        assert result["isMulticast"] is True
