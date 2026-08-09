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

What is here, and what is not
-----------------------------

The rule is that whatever you can *call* on a
:class:`~nanopynix_bindings.store.Store`, you can *implement* here. Two groups
of Nix store operations are deliberately absent:

* Anything needing a stream or a build result -- ``add_to_store``,
  ``nar_from_path``, ``build_paths_with_results``. Those take a C++
  ``Source &``/``Sink &`` or return a ``BuildResult``, and this protocol's
  whole shape is "return a dict, a string, or a list of strings". They are
  excluded permanently, not pending.
* Operations that are not ``nix::Store`` virtuals at all -- ``find_roots``,
  ``get_build_log``, ``collect_garbage``, ``add_perm_root``,
  ``add_indirect_root``. Those live on Nix's ``GcStore``, ``LogStore`` and
  ``LocalFSStore`` interfaces, which a Python store does not inherit, so there
  is nothing to override.
:meth:`~StoreImpl.read_derivation` is dispatched because Nix declares
``Store::readDerivation`` virtual from 2.34 on.
``nanopynix.build_info()["capabilities"]["store_impl_read_derivation"]``
carries the fact, the same mechanism ``dynamic_primop_registration`` uses for
primops.

Nix does *not* read a ``.drv`` through :meth:`~StoreImpl.query_path_info`: it
reads it through a store accessor, which a Python store without an
``underlying_store`` cannot serve, so ``read_derivation`` against one fails
with ``InvalidPath`` unless this hook answers.

This is not the same thing as :mod:`nanopynix.protocols`. Those describe the
async API that nanopynix's own engines *provide* to callers; this describes the
synchronous, per-operation interface a store *implements* for Nix to call.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar

from nanopynix_bindings.store import STORE_DISPATCH_METHODS

if TYPE_CHECKING:
    from collections.abc import Iterable

    from nanopynix_bindings.store import Store

DISPATCHABLE_METHODS: tuple[str, ...] = STORE_DISPATCH_METHODS
"""The store operations the C++ trampoline dispatches into Python.

Read from ``PyStoreImpl`` itself rather than restated here. A method this class
defines but ``py_store_impl.cpp`` never calls would fail in silence -- the
method would simply never run -- so the list is not ours to write.

Override one of these names in your :class:`StoreImpl` subclass and Nix calls
it. Override any other name and nothing happens, which is what
:meth:`~object.__init_subclass__` checks the class body against.
"""

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
            name for name in DISPATCHABLE_METHODS if getattr(cls, name, None) is not getattr(StoreImpl, name)
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

        Returns a store path as a string, in either accepted spelling. ``None``
        is a real answer and is not retried against
        :attr:`underlying_store` -- if you implement this, you own it.
        """
        raise NotImplementedError

    def query_referrers(self, path: str) -> Iterable[str]:
        """Every path that references ``path``.

        The inverse of ``references``, and the query garbage collection is
        built on. Nix's own base class reports this as unsupported rather than
        guessing, so a store that cannot answer should leave it alone rather
        than returning an empty list -- "nothing refers to this" and "I don't
        know" are very different answers to a collector.
        """
        raise NotImplementedError

    def query_valid_derivers(self, path: str) -> Iterable[str]:
        """Every valid ``.drv`` in this store that has ``path`` as an output.

        Not the same as ``deriver`` from :meth:`query_path_info`, which names
        the derivation that actually built the path and may no longer exist.
        """
        raise NotImplementedError

    def query_derivation_outputs(self, path: str) -> Iterable[str]:
        """The output paths of the derivation ``path``.

        Left alone, this is derived from the derivation itself, which is read
        through :meth:`query_path_info` and so routes back into your store.
        Implement it only if you can answer more cheaply or more accurately.
        """
        raise NotImplementedError

    def query_all_valid_paths(self) -> Iterable[str]:
        """Every path this store holds.

        Unimplemented, this raises rather than returning nothing -- a store
        with no way to enumerate itself is not the same as an empty store.
        """
        raise NotImplementedError

    def query_substitutable_paths(self, paths: Iterable[str]) -> Iterable[str]:
        """Which of ``paths`` this store could fetch from a substituter.

        A subset of ``paths``; returning them all means "every one of these is
        available", which is a claim Nix will act on.
        """
        raise NotImplementedError

    def add_temp_root(self, path: str) -> None:
        """Protect ``path`` from garbage collection until the process exits.

        Only meaningful for a store that collects garbage. Nix's base class
        debug-logs that the store does not support GC and carries on, which is
        the right behaviour for a store that does not.
        """
        raise NotImplementedError

    def ensure_path(self, path: str) -> None:
        """Make ``path`` valid, substituting or building it if it is not.

        Should raise if it cannot be made valid.
        """
        raise NotImplementedError

    def optimise_store(self) -> None:
        """Deduplicate identical files in the store, if that means anything here.

        Nix's base class does nothing, which is a correct implementation for
        any store without a notion of on-disk duplication.
        """
        raise NotImplementedError

    def verify_store(self, check_contents: bool, repair: bool) -> bool:
        """Check this store's integrity; return ``True`` if errors *remain*.

        Note the polarity: ``True`` means "still broken", not "verified".
        ``check_contents`` asks for contents to be hashed and not merely for
        metadata to be checked; ``repair`` asks for what can be fixed to be
        fixed. Nix's base class returns ``False`` without looking at anything.
        """
        raise NotImplementedError

    def compute_fs_closure(
        self,
        paths: Iterable[str],
        flip_direction: bool,
        include_outputs: bool,
        include_derivers: bool,
    ) -> Iterable[str]:
        """Every path reachable from ``paths``, including ``paths`` themselves.

        Follows ``references`` by default; ``flip_direction`` follows
        ``referrers`` instead, giving the paths that can reach these.
        ``include_outputs`` also pulls in the outputs of any derivation
        encountered, and ``include_derivers`` its deriver.

        Note this takes a *set* of paths where
        :meth:`nanopynix_bindings.store.Store.compute_fs_closure` takes one:
        Nix has two overloads and only the set-taking one is virtual, so it is
        the only one that can be dispatched. The single-path form is the
        set form with one element.

        Leaving this alone is usually right. Nix's base class walks the graph
        itself using :meth:`query_path_info`, :meth:`query_referrers` and
        :meth:`query_valid_derivers`, all of which route back into your store,
        so it is already correct -- implement this only if you can compute a
        closure in one step rather than one query per node.
        """
        raise NotImplementedError

    def query_missing(self, targets: Iterable[str]) -> dict[str, Any]:
        """What building ``targets`` would require.

        ``targets`` are derived paths in Nix's own ``^`` notation --
        ``<drv>^out``, ``<drv>^*``, or a bare store path. The distinction is
        load-bearing rather than cosmetic: a bare ``.drv`` means "fetch this
        file", while ``<drv>^*`` means "build it".

        Return a dict in the shape
        :meth:`nanopynix_bindings.store.Store.query_missing` produces, so one
        may be echoed back unchanged: ``will_build``, ``will_substitute``,
        ``unknown``, ``download_size`` and ``nar_size``. Missing keys keep
        Nix's defaults (empty sets, zero), so a store that only knows some of
        them may report just those.
        """
        raise NotImplementedError

    def read_derivation(self, path: str) -> str:
        """The derivation stored at ``path``, as ATerm text.

        Alone among these methods, the return value is a *serialization*
        rather than a rendering: the literal contents of the ``.drv``, which
        is what a store holding one already has. Nix parses it with its own
        reader, so a derivation round-trips exactly -- including
        ``__structuredAttrs`` and nested ``inputDrvs``, which a dict shaped
        like :meth:`nanopynix_bindings.store.Store.read_derivation`'s output
        could not reconstruct without this module reimplementing Nix's
        ``DerivationOutput`` variants once per supported version.

        A store that holds ``.drv`` files can return their bytes unread. A
        store that *synthesizes* derivations has to produce the same format,
        and there is no helper here for that: it is Nix's ATerm spelling,
        ``Derive([outputs],[inputDrvs],[inputSrcs],system,builder,[args],
        [env])``, which is what ``Derivation::unparse`` writes and what
        ``nix derivation show --json`` is a view of. Emitting it by hand is
        the cost of this method's fidelity; the alternative, a dict, cannot
        carry ``__structuredAttrs`` or a nested ``inputDrvs`` tree.

        A malformed or empty value raises out of Nix's parser, naming
        ``path``.

        Dispatched because ``Store::readDerivation`` is virtual -- see the
        module docstring, and
        ``build_info()["capabilities"]["store_impl_read_derivation"]``.
        """
        raise NotImplementedError
