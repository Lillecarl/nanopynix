"""Conformance checks for transport-neutral public protocols.

Four checks, because a protocol can be wrong in more directions than the
obvious one.

``test_protocol_static_conformance`` is the classic one, run by pyright: does
each engine's concrete class satisfy the protocol it claims to? That catches a
class that drops or changes a declared member.

``test_protocols_declare_the_whole_shared_surface`` is the other direction:
does the protocol declare everything both engines actually share? Nothing
checked that until it was added, and the protocols had drifted badly as a
result -- ``AsyncValue`` declared 6 of 21 shared members, ``AsyncEvalSession``
6 of 8. An under-declared protocol passes conformance while promising almost
nothing, so the three ``Value`` divergences the ledger tracks were free to
persist: no gate was looking at them.

The remaining two are about runtime, and exist because beartype checks these
hints for real. ``test_every_protocol_is_runtime_checkable`` pins the
decorator that lets it do so at all, and
``test_both_engines_pass_the_runtime_conformance_check`` re-asks the
conformance question the way beartype will ask it. The *consequence* of
getting the first one wrong -- beartype silently skipping whole callables --
is asserted a file away, in
``test_beartype_instrumentation.py::test_no_callable_is_silently_undecoratable_beyond_the_known_list``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from nanopynix import inproc
from nanopynix.protocols import (
    AsyncEvalSession,
    AsyncLockedFlake,
    AsyncReplSession,
    AsyncSession,
    AsyncStore,
    AsyncValue,
    AsyncVerbosityController,
)
from nanopynix.rpc.client import _session as rpc_private
from nanopynix.rpc.client.session import Session as RpcSessionImpl
from nanopynix.rpc.client.store import Store as RpcStoreImpl

if TYPE_CHECKING:
    from nanopynix.inproc import (
        EvalSession as InprocEvalSession,
        LockedFlake,
        ReplSession as InprocReplSession,
        Session as InprocSession,
        Store as InprocStore,
        Value,
    )
    from nanopynix.rpc.client._session import (
        EvalSession as RpcEvalSession,
        LockedFlakeHandle,
        ReplSession as RpcReplSession,
        ValueProxy,
    )
    from nanopynix.rpc.client.session import Session as RpcSession
    from nanopynix.rpc.client.store import Store as RpcStore


def _accept_async_value(value: AsyncValue) -> None:
    del value


def _accept_async_store(store: AsyncStore) -> None:
    del store


def _accept_async_locked_flake(locked_flake: AsyncLockedFlake) -> None:
    del locked_flake


def _accept_async_eval_session(eval_session: AsyncEvalSession) -> None:
    del eval_session


def _accept_async_repl_session[ReplValueT: AsyncValue](repl_session: AsyncReplSession[ReplValueT]) -> None:
    del repl_session


def _accept_async_verbosity_controller[VerbosityT](controller: AsyncVerbosityController[VerbosityT]) -> None:
    del controller


def _accept_async_session[StoreT: AsyncStore, EvalT: AsyncEvalSession[Any], ReplT: AsyncReplSession[Any]](
    session: AsyncSession[StoreT, EvalT, ReplT],
) -> None:
    """Generic, because ``StoreT`` is invariant and a bare ``AsyncSession`` is unsatisfiable.

    ``eval`` and ``repl`` take a store *and* ``store`` returns one, so the
    parameter cannot be covariant. A bare ``AsyncSession`` therefore means
    ``AsyncSession[AsyncStore, ...]``, and an engine whose ``eval`` demands its
    own concrete ``Store`` is not that -- correctly, since it would reject the
    other engine's store. Solving the parameters from the argument asks the
    question that is actually useful: is this class *some* ``AsyncSession``?
    The same reason ``_accept_async_repl_session`` is generic in its value.
    """
    del session


def test_protocol_static_conformance() -> None:
    """Keep structural compatibility checked by pyright without constructing Nix."""
    if TYPE_CHECKING:
        value_proxy = cast("ValueProxy", None)
        inproc_value = cast("Value", None)
        rpc_eval_session = cast("RpcEvalSession", None)
        inproc_eval_session = cast("InprocEvalSession", None)
        rpc_repl_session = cast("RpcReplSession", None)
        inproc_repl_session = cast("InprocReplSession", None)
        rpc_session = cast("RpcSession", None)
        inproc_session = cast("InprocSession", None)
        rpc_store = cast("RpcStore", None)
        inproc_store = cast("InprocStore", None)
        inproc_locked_flake = cast("LockedFlake", None)
        locked_flake = cast("LockedFlakeHandle", None)
        _accept_async_value(value_proxy)
        _accept_async_value(inproc_value)
        _accept_async_store(rpc_store)
        _accept_async_store(inproc_store)
        _accept_async_eval_session(rpc_eval_session)
        _accept_async_eval_session(inproc_eval_session)
        _accept_async_repl_session(rpc_repl_session)
        _accept_async_repl_session(inproc_repl_session)
        _accept_async_verbosity_controller(rpc_session)
        _accept_async_verbosity_controller(inproc_session)
        _accept_async_session(rpc_session)
        _accept_async_session(inproc_session)
        _accept_async_locked_flake(inproc_locked_flake)
        _accept_async_locked_flake(locked_flake)


# Members both engines have that the protocol deliberately does not declare,
# with why. Each must name a real reason -- "no signature is true of both
# engines" -- not "we did not get to it". Every one of these is also recorded
# in tests/nanopynix/test_engine_parity.py, which is where the work to remove
# it is tracked; this table only records that the protocol is knowingly silent.
UNDECLARED: dict[str, str] = {}

# (protocol name, protocol, inproc class, rpc class)
PROTOCOL_PAIRS: list[tuple[str, type, type, type]] = [
    ("AsyncValue", AsyncValue, inproc.Value, rpc_private.ValueProxy),
    ("AsyncStore", AsyncStore, inproc.Store, RpcStoreImpl),
    ("AsyncEvalSession", AsyncEvalSession, inproc.EvalSession, rpc_private.EvalSession),
    ("AsyncReplSession", AsyncReplSession, inproc.ReplSession, rpc_private.ReplSession),
    ("AsyncLockedFlake", AsyncLockedFlake, inproc.LockedFlake, rpc_private.LockedFlakeHandle),
    # Last to arrive, and the omission was the reason 13 shared `Session`
    # members went undeclared: the surface gate below only looks at the pairs
    # in this list, so a class absent from it is a class nothing measures.
    ("AsyncSession", AsyncSession, inproc.Session, RpcSessionImpl),
]


def _public(cls: type) -> set[str]:
    return {name for name in dir(cls) if not name.startswith("_")}


def test_every_protocol_is_runtime_checkable() -> None:
    """Because a protocol that is not silently disables beartype around it.

    beartype cannot decorate a function whose parameter is annotated with a
    protocol it cannot pass to ``isinstance``; it skips the whole function and
    warns. That failure is invisible from inside a passing test suite, so the
    property is pinned here rather than left to the warning nobody reads.
    """
    # `_is_runtime_protocol` is what `typing.runtime_checkable` sets and what
    # `_ProtocolMeta.__instancecheck__` reads; there is no public accessor.
    not_checkable = [
        name for name, protocol, _, _ in PROTOCOL_PAIRS if not getattr(protocol, "_is_runtime_protocol", False)
    ]
    assert not not_checkable, f"these protocols are not @runtime_checkable: {not_checkable}"


def _conforms_at_runtime(cls: type, protocol: type) -> bool:
    """Runtime conformance, by whichever check ``typing`` permits for ``protocol``.

    ``issubclass`` is the direct question and is what we ask when we can. But
    ``typing`` forbids it against a protocol with a non-method member --
    ``AsyncReplSession.line_editors`` is one -- and skipping those would leave
    the one protocol that needs a different path checked by nothing.

    ``isinstance`` answers the same question there, and it is also the check
    beartype itself performs, so it is if anything the more relevant of the
    two. It needs an instance rather than a class; ``object.__new__`` gives an
    uninitialized one, which is enough because
    ``_ProtocolMeta.__instancecheck__`` reads members with
    ``inspect.getattr_static`` -- looked up on the type, never invoked. That
    matters for the rpc class in particular: it is ``__slots__``-based, so
    every instance attribute is unset here and a getter-invoking check would
    see nothing. Both engines declare ``line_editors`` as a property on the
    class, so the static lookup finds it. No Nix state is constructed.
    """
    if all(callable(getattr(protocol, member, None)) for member in _public(protocol)):
        return issubclass(cls, protocol)
    return isinstance(object.__new__(cls), protocol)


def test_both_engines_pass_the_runtime_conformance_check() -> None:
    """The static conformance above, asserted again at runtime.

    Not redundant: pyright checks the classes named in ``PROTOCOL_PAIRS``,
    while this checks the objects beartype will actually see -- and beartype's
    check is structural at runtime, so a class can satisfy pyright and still
    fail here (a member supplied by a ``TYPE_CHECKING``-only declaration, say).
    """
    for name, protocol, inproc_cls, rpc_cls in PROTOCOL_PAIRS:
        assert _conforms_at_runtime(inproc_cls, protocol), f"inproc {inproc_cls.__name__} fails {name} at runtime"
        assert _conforms_at_runtime(rpc_cls, protocol), f"rpc {rpc_cls.__name__} fails {name} at runtime"


def test_every_protocol_member_is_abstract() -> None:
    """A non-abstract member turns a forgotten method into a silent no-op.

    This is why ``@abstractmethod`` is required and not decoration. An engine
    class now *inherits* its protocol, so a member it omits is inherited
    instead -- and the inherited body is ``...``, which returns ``None``. A
    forgotten ``close`` would leak rather than raise. Abstract makes it a
    ``TypeError`` at instantiation.
    """
    not_abstract: list[str] = []
    for name, protocol, _, _ in PROTOCOL_PAIRS:
        abstract: frozenset[str] = getattr(protocol, "__abstractmethods__", frozenset())
        not_abstract.extend(f"{name}.{member}" for member in sorted(_public(protocol) - abstract))
    assert not not_abstract, (
        "these protocol members are not @abstractmethod, so an engine that omits one "
        f"inherits a `...` body and returns None instead of failing: {not_abstract}"
    )


def test_every_protocol_declares_slots_in_its_own_body() -> None:
    """``vars``, not ``getattr``: ``Protocol`` supplies an inherited ``()`` that lies.

    A ``__slots__``-based class that subclasses a protocol whose *body* omits
    ``__slots__`` gains a ``__dict__`` per instance. rpc's ``Store``,
    ``ValueProxy`` and ``EvalSession`` are all ``__slots__``-based, and one
    ``ValueProxy`` exists per Nix value, so the cost is per value.

    ``getattr(protocol, "__slots__")`` answers ``()`` from ``typing.Protocol``
    whether or not the body declares it, which makes the obvious check useless.
    """
    missing = [name for name, protocol, _, _ in PROTOCOL_PAIRS if "__slots__" not in vars(protocol)]
    assert not missing, (
        "these protocols do not declare __slots__ = () in their own body, so every "
        f"__slots__-based engine class inheriting them gains a __dict__: {missing}"
    )


def test_no_slotted_engine_class_gained_a_dict() -> None:
    """The consequence of the rule above, asserted on the classes that pay for it.

    ``rpc.ReplSession`` is deliberately absent: it declares no ``__slots__`` of
    its own, so it had a ``__dict__`` before any of this and still does.
    """
    for cls in (RpcStoreImpl, rpc_private.ValueProxy, rpc_private.EvalSession):
        assert not hasattr(object.__new__(cls), "__dict__"), (
            f"{cls.__name__} gained a __dict__; a protocol in its MRO is missing __slots__ = ()"
        )


def test_every_engine_class_inherits_its_protocol() -> None:
    """Structural conformance was already checked. This pins the *nominal* link.

    Inheritance is what makes the docstring inheritable and what arms
    ``@abstractmethod``. Both are lost silently if a class stops naming its
    protocol, and every other check in this file keeps passing when it does.

    The check is ``__mro__`` and deliberately not ``issubclass``: against a
    ``@runtime_checkable`` protocol ``issubclass`` is *structural*, so it
    answers True for a class that inherits nothing and would report a detached
    class as fine. That is the exact failure this test exists to catch.
    """
    detached = [
        f"{name}: {cls.__name__}"
        for name, protocol, inproc_cls, rpc_cls in PROTOCOL_PAIRS
        for cls in (inproc_cls, rpc_cls)
        if protocol not in cls.__mro__
    ]
    assert not detached, f"these engine classes no longer name their protocol as a base: {detached}"


def test_a_class_that_inherits_nothing_still_conforms() -> None:
    """The property an ABC would have cost, kept and pinned.

    ``Protocol``'s metaclass is ``ABCMeta``, so abstract members enforce on an
    explicit subclass -- but structural conformance survives for a class that
    imports nothing from this repository. That is the whole reason these stayed
    protocols instead of becoming ABCs, so it is asserted rather than assumed.
    """

    class Outsider:
        async def eval(self) -> object: ...
        async def write_lock_file(self) -> None: ...
        async def release(self) -> None: ...

    # The MRO is the nominal question. `issubclass` is *not*: against a
    # runtime-checkable protocol it is structural too, so it answers True here
    # and cannot tell inheritance from conformance. It is not asserted because
    # pyright refuses the call statically -- `__slots__ = ()` in each protocol
    # body makes these *data* protocols to its eyes, though `typing` excludes
    # dunders from the members it checks, so the runtime call is fine.
    assert AsyncLockedFlake not in Outsider.__mro__
    assert isinstance(Outsider(), AsyncLockedFlake)


def test_protocols_declare_the_whole_shared_surface() -> None:
    """Every member both engines share must be declared, or excused in UNDECLARED.

    This is the gate that keeps a protocol from quietly becoming decorative.
    Conformance alone cannot: a protocol declaring nothing at all passes it.
    """
    undeclared: dict[str, str] = {}
    for name, protocol, inproc_cls, rpc_cls in PROTOCOL_PAIRS:
        shared = _public(inproc_cls) & _public(rpc_cls)
        for member in sorted(shared - _public(protocol)):
            key = f"{name}.{member}"
            undeclared[key] = UNDECLARED.get(key, "UNJUSTIFIED")

    unjustified = sorted(key for key, reason in undeclared.items() if reason == "UNJUSTIFIED")
    assert not unjustified, (
        "both engines have these members but the protocol does not declare them -- "
        f"declare them, or add them to UNDECLARED with a reason: {unjustified}"
    )

    stale = sorted(set(UNDECLARED) - set(undeclared))
    assert not stale, f"UNDECLARED lists members the protocol now declares (or that no longer exist): {stale}"
