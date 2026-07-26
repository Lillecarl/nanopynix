"""``proto_shape`` must flatten nanobind containers without touching scalars.

Inherited from the deleted ``test_service_adapter_unit.py``: the reflective
service adapter it covered is gone, but this helper outlived it because the
eval worker still receives ``nb::dict``/``nb::list`` from C++ where a build
result crosses the boundary as a mapping.
"""

from __future__ import annotations

from nanopynix.rpc.worker._proto_shape import proto_shape


def test_proto_shape_normalizes_nested_containers() -> None:
    assert proto_shape({"a": [1, "two", {"b": 3}]}) == {"a": [1, "two", {"b": 3}]}
    assert proto_shape((1, 2, 3)) == [1, 2, 3]


def test_proto_shape_leaves_scalars_and_byte_strings_alone() -> None:
    """str/bytes are Sequences -- flattening them into char lists would be a bug."""
    assert proto_shape("just a string") == "just a string"
    assert proto_shape(b"raw bytes") == b"raw bytes"
    assert proto_shape(7) == 7
