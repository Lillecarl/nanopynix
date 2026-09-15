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
from hyperframe.frame import PingFrame

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


def test_a_connection_without_the_table_says_what_it_is() -> None:
    """The one place the copy departs from h2, and issue #299 is why.

    A bare ``AttributeError`` here reaches the caller as ``WorkerDiedError:
    Protocol error``, which names no object. The replacement names the class,
    its bases and the attributes the instance does carry.
    """
    conn = H2Connection()
    del conn._frame_dispatch_table  # pyright: ignore[reportPrivateUsage] -- reproducing the shape issue #299 reports

    # The patched method directly, because `receive_data` reaches it only once
    # a whole frame has arrived, and the shape under test is the lookup.
    with pytest.raises(RuntimeError, match="has no _frame_dispatch_table") as raised:
        conn._receive_frame(PingFrame(0))  # pyright: ignore[reportPrivateUsage] -- the patched method is the subject

    assert "H2Connection" in str(raised.value)
    assert "incoming_buffer" in str(raised.value)
    # The discriminator of issue #299: this instance really did finish
    # `__init__`, so the report says so and the missing table was removed
    # afterwards. A connection that never finished reports `init_complete=False`.
    assert "init_complete=True" in str(raised.value)


def test_a_finished_connection_carries_the_init_mark() -> None:
    """Without the mark, `init_complete` reads False for every connection and
    the report cannot tell a partial object from a stripped one."""
    assert getattr(H2Connection(), protocol._H2_INIT_COMPLETE, False) is True  # pyright: ignore[reportPrivateUsage] -- the module under test


def test_a_renamed_member_refuses_instead_of_patching(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(protocol, "_h2_fast_receive_patch_installed", False)
    monkeypatch.setattr(protocol, "_H2_PRIVATE_MEMBERS", ("_prepare_for_sending", "_gone_upstream"))

    with pytest.raises(RuntimeError, match="_gone_upstream"):
        protocol.install_h2_fast_receive_patch()
