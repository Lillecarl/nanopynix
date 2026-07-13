"""ipaddress primop implementations — worker-side (returns attrsets + callables)."""

from __future__ import annotations

import ipaddress


def _address_to_dict(addr: ipaddress.IPv4Address | ipaddress.IPv6Address) -> dict[str, object]:
    d: dict[str, object] = {
        "version": addr.version,
        "compressed": addr.compressed,
        "exploded": addr.exploded,
        "isPrivate": addr.is_private,
        "isGlobal": addr.is_global,
        "isMulticast": addr.is_multicast,
        "isLoopback": addr.is_loopback,
        "isLinkLocal": addr.is_link_local,
        "isReserved": addr.is_reserved,
        "isUnspecified": addr.is_unspecified,
        "maxPrefixlen": addr.max_prefixlen,
        "reversePointer": addr.reverse_pointer,
    }
    if isinstance(addr, ipaddress.IPv4Address):
        d["isBroadcast"] = addr.is_multicast
    else:
        d["isSiteLocal"] = addr.is_site_local
        d["ipv4Mapped"] = str(addr.ipv4_mapped) if addr.ipv4_mapped else None
    return d


_INT64_MAX = 9223372036854775807


def _network_to_dict(net: ipaddress.IPv4Network | ipaddress.IPv6Network) -> dict[str, object]:
    d: dict[str, object] = {
        "version": net.version,
        "prefixlen": net.prefixlen,
        "numAddresses": min(net.num_addresses, _INT64_MAX),
        "networkAddress": str(net.network_address),
        "netmask": str(net.netmask),
        "hostmask": str(net.hostmask),
        "isPrivate": net.is_private,
        "isGlobal": net.is_global,
        "isMulticast": net.is_multicast,
        "isLoopback": net.is_loopback,
        "isLinkLocal": net.is_link_local,
        "isReserved": net.is_reserved,
        "isUnspecified": net.is_unspecified,
        "address": lambda n: str(net[int(n)]),
        "subnet": lambda n, prefixlen_diff: str(list(net.subnets(prefixlen_diff=prefixlen_diff))[int(n)]),
    }
    if isinstance(net, ipaddress.IPv4Network):
        d["broadcastAddress"] = str(net.broadcast_address)
    else:
        d["isSiteLocal"] = net.is_site_local
    return d


def parse_address(source: str) -> dict[str, object]:
    """Parse a string into an address attrset.

    In Nix: ``builtins.parseAddress "192.168.1.1"``

    Returns an attrset with version, compressed, exploded, and is* properties.
    """
    addr = ipaddress.ip_address(source)
    return _address_to_dict(addr)


def parse_network(source: str) -> dict[str, object]:
    """Parse a string into a network attrset.

    In Nix: ``builtins.parseNetwork "192.168.1.0/24"``

    Returns an attrset with prefixlen, numAddresses, is* properties,
    and callable methods:

      - ``.address n`` — nth address in the network (0-indexed, includes
        network address; negative indices work)
      - ``.subnet n prefixlen_diff`` — nth subnet when splitting by
        prefixlen_diff (0-indexed)
    """
    net = ipaddress.ip_network(source, strict=False)
    return _network_to_dict(net)


def parse_interface(source: str) -> dict[str, object]:
    """Parse a string into an interface attrset.

    In Nix: ``builtins.parseInterface "192.168.1.1/24"``

    Returns an attrset with:

      - ``.ip`` — address attrset (same shape as ``parseAddress``)
      - ``.network`` — network attrset (same shape as ``parseNetwork``)
      - ``.withPrefixlen`` / ``.withNetmask`` / ``.withHostmask`` — string representations
    """
    iface = ipaddress.ip_interface(source)
    return {
        "ip": _address_to_dict(iface.ip),
        "network": _network_to_dict(iface.network),
        "withPrefixlen": iface.with_prefixlen,
        "withNetmask": iface.with_netmask,
        "withHostmask": iface.with_hostmask,
    }
