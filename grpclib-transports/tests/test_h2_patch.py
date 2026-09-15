"""The fast-receive patch states what it reads out of ``h2``.

``_receive_frame_without_trace_repr`` is a copy of an h2 private method, so
every line of it touches a private member. The signature check alone covered
one name of five. A release that renames one of the others gives an
``AttributeError`` from inside the frame pump of a live connection, which
reaches the caller as ``WorkerDiedError: Protocol error`` and names neither h2
nor this patch.
"""

from __future__ import annotations

import inspect

import pytest
from grpclib_transports import protocol
from h2.connection import H2Connection

_MEMBERS: tuple[str, ...] = protocol._H2_PRIVATE_MEMBERS  # pyright: ignore[reportPrivateUsage] -- the module under test
_COPY_SOURCE = inspect.getsource(protocol._receive_frame_without_trace_repr)  # pyright: ignore[reportPrivateUsage] -- the function under test


def test_every_named_member_is_really_there() -> None:
    """The guard is only worth its line while the names are right.

    A typo in the tuple makes the patch refuse on a good h2, and the whole
    library then fails to import. That is worse than the hole it closes.
    """
    for name in _MEMBERS:
        assert hasattr(H2Connection, name), name


def test_the_patch_reads_no_class_member_the_tuple_leaves_out() -> None:
    """The tuple has to follow the copy, and nothing else makes it.

    ``_frame_dispatch_table`` is the one deliberate omission:
    ``H2Connection`` sets it in ``__init__``, so the class cannot answer for
    it. See nanopynix issue #299 for the CI job where an instance did not have
    it either.
    """
    read = {
        name
        for name in dir(H2Connection)
        if name.startswith("_") and not name.startswith("__") and f"self.{name}" in _COPY_SOURCE
    }

    assert read - {"_frame_dispatch_table"} == set(_MEMBERS)


def test_a_renamed_member_refuses_instead_of_patching(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(protocol, "_h2_fast_receive_patch_installed", False)
    monkeypatch.setattr(protocol, "_H2_PRIVATE_MEMBERS", ("_prepare_for_sending", "_gone_upstream"))

    with pytest.raises(RuntimeError, match="_gone_upstream"):
        protocol.install_h2_fast_receive_patch()
