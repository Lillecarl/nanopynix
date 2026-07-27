"""Base class for Nix stores implemented in Python.

:func:`~nanopynix.register_store_implementation` lets a Python object stand in
as a Nix store: register a URI scheme, and ``open_store("<scheme>://...")``
routes into your object instead of into one of Nix's own store backends.

Subclass :class:`StoreImpl`, override the operations you want to serve, and
leave the rest alone. An operation you do not override falls through to
:attr:`StoreImpl.underlying_store` if you set one, and otherwise to whatever
Nix's own ``Store`` base class does for that operation -- which for most
queries means deriving an answer from the operations you *did* override.

Overriding is how the C++ side decides what you implement. It used to probe
with ``hasattr``, which cannot tell a real implementation from a typo: misspell
``query_path_info`` and you silently got the fallback instead of an error.
:meth:`~object.__init_subclass__` records which of :data:`DISPATCHABLE_METHODS`
your class actually replaced, once, when the class is defined. Two consequences
worth knowing:

* Subclassing :class:`StoreImpl` is required. Registering a duck-typed object
  raises :class:`TypeError` rather than quietly producing a store that answers
  nothing.
* The set is fixed when the class body executes, so attaching a method to the
  class afterwards with ``setattr`` will not register it. Define your methods
  in the class body.

This is not the same thing as :mod:`nanopynix.protocols`. Those describe the
async API that nanopynix's own engines *provide* to callers; this describes the
synchronous, per-operation interface a store *implements* for Nix to call.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar

if TYPE_CHECKING:
    from nanopynix_bindings.store import Store

# The store operations the C++ trampoline (`PyStoreImpl`) can dispatch into
# Python. Deliberately short: `nix::Store` has far more virtuals, and adding one
# here means nothing unless `py_store_impl.cpp` grows a call site for it.
DISPATCHABLE_METHODS = (
    "is_valid_path_uncached",
    "query_path_info",
    "query_path_from_hash_part",
)

# Where `__init_subclass__` records the answer, and the name `py_store_impl.cpp`
# reads to find it. Changing it means changing the C++ side in lockstep.
OVERRIDES_ATTRIBUTE = "_nanopynix_store_overrides"


class StoreImpl:
    """A Nix store backed by Python.

    Override any subset of the methods below. None is required -- a subclass
    that overrides nothing is a valid store that defers everything, which is
    useful with :attr:`underlying_store`.

    Deliberately not an :class:`abc.ABC`. Since no operation is mandatory there
    is nothing to mark :func:`~abc.abstractmethod`, and an ABC with no abstract
    members only implies a restriction it does not enforce. What this class is
    for is recording which methods a subclass replaced, and giving the type
    checker signatures to check them against.
    """

    underlying_store: Store | None = None
    """A real store to serve every operation this class does not override.

    Unlike the methods, this is read from the *instance* at open time, so
    setting it in ``__init__`` works and is the usual way to use it -- which is
    why it is deliberately not a :data:`~typing.ClassVar`, even though the
    default lives on the class. ``ClassVar`` would make the common case
    (``self.underlying_store = ...``) a type error.
    """

    _nanopynix_store_overrides: ClassVar[frozenset[str]] = frozenset()

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        cls._nanopynix_store_overrides = frozenset(
            name
            for name in DISPATCHABLE_METHODS
            if getattr(cls, name, None) is not getattr(StoreImpl, name)
        )

    def is_valid_path_uncached(self, path: str) -> bool:
        """Does ``path`` exist in this store?

        ``path`` is a base name (``<hash>-<name>``), not a full store path.
        Nix's own path-info cache is disabled for Python stores, so this is
        called every time rather than being memoised.
        """
        raise NotImplementedError

    def query_path_info(self, path: str) -> dict[str, Any] | None:
        """Metadata for ``path``, or ``None`` if it is not valid.

        ``path`` is a base name. The return value is a dict in the same shape
        :meth:`nanopynix_bindings.store.Store.query_path_info` produces, so a
        store may echo one back unchanged: ``path``, ``references``,
        ``nar_hash``, ``nar_size``, ``registration_time``, ``deriver``, ``ca``,
        ``ultimate`` and ``sigs``. Absent optionals may be omitted or given as
        ``None``.

        Store paths in ``path``, ``references`` and ``deriver`` are accepted
        both as full ``/nix/store/...`` paths and as bare base names -- you are
        handed base names, so echoing your input is valid, and so is mirroring
        what ``query_path_info`` renders.

        Raising propagates to the caller. It does not fall through to
        :attr:`underlying_store`; a store that fails should say so rather than
        hand back someone else's data.
        """
        raise NotImplementedError

    def query_path_from_hash_part(self, hash_part: str) -> str | None:
        """The path whose hash is ``hash_part``, or ``None`` if there is none.

        Returns a store path as a string, in either accepted spelling.
        """
        raise NotImplementedError
