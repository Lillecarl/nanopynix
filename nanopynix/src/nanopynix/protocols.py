"""Transport-neutral public protocols for nanopynix's asynchronous APIs.

The RPC and in-process implementations have different construction and
lifetime mechanics, but callers can depend on these shared operation shapes.
Protocols deliberately describe only behaviour common to both transports.
See :mod:`nanopynix.store` and :mod:`nanopynix.inproc` for the two
implementations, checked against these protocols in
``nanopynix/tests/test_protocols.py``.

Every protocol here is ``@runtime_checkable``. That is not for callers to reach
for ``isinstance`` in place of static typing -- it stays a structural check on
member *names*, so it says nothing about signatures and is no substitute for
what pyright already verifies. It is here because a protocol beartype cannot
pass to ``isinstance`` is one beartype refuses to decorate *at all*: annotate a
parameter with a non-runtime-checkable protocol and the entire surrounding
function goes unchecked, silently, with only a warning on stderr to say so.
Measured before this was added: 20 of the methods below, plus every function
elsewhere annotated with one of them, were skipped outright.

The runtime cost is bounded by where these names are actually annotated. Inside
this repository that is only the stub methods below, whose bodies are ``...``
and never run; the check therefore fires for consumers who annotate with these
types, and for nobody else. Note the asymmetry ``@runtime_checkable`` inherits
from :mod:`typing`: ``AsyncReplSession`` has a non-method member
(``line_editors``), so ``isinstance`` works against it but ``issubclass`` does
not.

Every member is ``@abstractmethod``, and each engine class names its protocol
as a base. That is not a move to ABCs, and it gives up nothing: ``Protocol``'s
metaclass **is** ``ABCMeta``, so an abstract member enforces itself on an
explicit subclass while a class that inherits nothing still conforms
structurally. A ``Protocol`` with abstract members is a superset of an ABC.

``@abstractmethod`` is required rather than decorative. An explicit subclass
that omits a **non**-abstract member inherits the ``...`` body, so the call
returns ``None`` and the omission is silent -- a missing ``close`` would leak
rather than raise. Abstract turns that into a ``TypeError`` at instantiation.

Each protocol declares ``__slots__ = ()`` **in its own body**, and that line is
load-bearing. A ``__slots__``-based class that subclasses a protocol whose body
omits it gains a ``__dict__`` per instance -- measured at 40 bytes to 56 on
Python 3.14, plus the dict. rpc's ``Store``, ``ValueProxy``, ``EvalSession``
and ``ReplSession`` are all ``__slots__``-based, and one ``ValueProxy`` exists
per Nix value. The check is easy to get wrong:
``getattr(protocol, "__slots__")`` answers ``()`` from ``Protocol`` itself even
when the body omits it, so ``nanopynix/tests/test_protocols.py`` asks
``vars(protocol)`` instead.
"""

from __future__ import annotations

from abc import abstractmethod
from typing import TYPE_CHECKING, Any, Protocol, Self, TypeVar, runtime_checkable

from nanopynix_bindings.store import BuildMode

# LogLevel is a runtime import, unlike the type-only names below:
# AsyncEvalSession parameterises AsyncVerbosityController with it, and a base
# class expression is evaluated when the module loads.
from nanopynix_proto.nix.common import LogLevel
from nanopynix_proto.nix.store import GcAction, StoreDirs

from nanopynix._typechecking import BEARTYPING
from nanopynix._wire import DEFAULT_CA_METHOD, DEFAULT_HASH_ALGO, NO_GC_LIMIT

if TYPE_CHECKING or BEARTYPING:
    from collections.abc import AsyncIterator, Mapping, Sequence

    from nanopynix.logging import BusSubscription, LogCallback, LogCapture
    from nanopynix.models import (
        BuildResult,
        Derivation,
        FlakeRef,
        GcResult,
        GcRoot,
        JsonValue,
        LockedNode,
        LogEvent,
        MissingInfo,
        NixType,
        PathInfo,
        RegistryEntry,
        StorePath,
    )
    from nanopynix.settings import NixEvalSettings, NixFetchSettings, NixGlobalSettings, SettingsProvenance
    from nanopynix.stores import StoreConfig
    from nanopynix.verbosity import LogLevelInput

VerbosityT_co = TypeVar("VerbosityT_co", covariant=True)


@runtime_checkable
class AsyncValue[StoreT: AsyncStore = Any](Protocol):
    """The common asynchronous value lifecycle and forcing interface.

    ``StoreT`` is the store type of the engine that made this value, and it is
    what :meth:`build` accepts. Each engine names its own, so neither has to
    accept the other's.

    **The default is ``Any`` and not :class:`AsyncStore`.** A bare
    ``AsyncValue`` is the annotation almost every consumer writes, and it has
    to keep meaning "a value of any engine". ``StoreT`` is invariant, so a
    default of ``AsyncStore`` would make the bare name mean
    ``AsyncValue[AsyncStore]``, which no engine's value satisfies. Measured:
    that default gave 54 pyright errors across the bound sites and the test
    doubles, where ``Any`` gives none.
    """

    __slots__ = ()  # in the body, and load-bearing -- see the module docstring

    @abstractmethod
    async def __aenter__(self) -> Self: ...

    @abstractmethod
    async def __aexit__(self, *args: object) -> None: ...

    @abstractmethod
    async def get_type(self) -> NixType:
        """Evaluate to weak head normal form and return the resulting Nix type.

        This is Nix's ``forceValue`` followed by ``value->type()``, which is
        how Nix itself uses forcing: the verb is never the goal, knowing what
        you have is. There is no separate ``force()`` because on both engines
        answering this question already forces.
        """
        ...

    @abstractmethod
    async def to_python(self, *, copy_to_store: bool = False) -> JsonValue:
        """Convert the whole value tree to plain Python data, using Nix's toJSON rules."""
        ...

    @abstractmethod
    async def realise_string(self) -> str:
        """Coerce to a string and realise its Nix string context."""
        ...

    @abstractmethod
    async def realise_argv(self) -> list[str]:
        """Coerce a Nix list to argv and realise all string contexts."""
        ...

    @abstractmethod
    async def edit_location(self) -> tuple[str, int]:
        """Return the physical file path and line Nix would open for this value."""
        ...

    @abstractmethod
    async def release(self) -> None:
        """Release the underlying worker-side handle. Idempotent."""
        ...

    # ── Typed extraction ───────────────────────────────────────────
    # Each forces to weak head normal form and raises if the value is not of
    # the requested type. `Self` rather than `AsyncValue` in the container
    # returns is what keeps an engine's values homogeneous: indexing an
    # inproc list yields an inproc Value, not "some AsyncValue".

    @abstractmethod
    async def as_int(self) -> int:
        """Return this value as a Nix integer, or raise if it is not one."""
        ...

    @abstractmethod
    async def as_float(self) -> float:
        """Return this value as a Nix float, or raise if it is not one."""
        ...

    @abstractmethod
    async def as_bool(self) -> bool:
        """Return this value as a Nix boolean, or raise if it is not one."""
        ...

    @abstractmethod
    async def as_string(self) -> str:
        """Return this value as a Nix string, or raise if it is not one."""
        ...

    @abstractmethod
    async def as_list(self) -> list[Self]:
        """Return this value's elements, or raise if it is not a list."""
        ...

    @abstractmethod
    async def as_dict(self) -> dict[str, Self]:
        """Return this value's attributes, or raise if it is not an attrset."""
        ...

    # ── Structure ──────────────────────────────────────────────────

    @abstractmethod
    async def has_attr(self, name: str, /) -> bool:
        """Return whether this attrset has the attribute ``name``."""
        ...

    @abstractmethod
    async def attr_names(self) -> list[str]:
        """Return this attrset's attribute names."""
        ...

    @abstractmethod
    async def list_length(self) -> int:
        """Return the number of elements in this list."""
        ...

    # ── Selection ──────────────────────────────────────────────────
    #
    # `def`, not `async def`, on a protocol whose every other member is a
    # coroutine: selecting a child does no Nix work on either engine now, it
    # only records which child was asked for. The hop is deferred to whatever
    # forces the result, not absent. That is what makes `v.attr("a").attr("b")`
    # a legal chain rather than a stack of nested awaits.
    #
    # These three were the last members of the shared surface this protocol
    # could not describe, because inproc awaited `attr`/`list_get` and took a
    # single `call` argument. nanopynix/tests/test_engine_parity.py's ledger
    # carried them as Value.attr:async, Value.list_get:async and
    # Value.call:params.

    @abstractmethod
    def attr(self, name: str, /) -> Self:
        """Select attribute ``name``, without forcing anything yet."""
        ...

    @abstractmethod
    def list_get(self, index: int, /) -> Self:
        """Select element ``index``, without forcing anything yet."""
        ...

    # ── Application and realisation ────────────────────────────────

    @abstractmethod
    async def call(self, *args: Any) -> Self:
        """Call this value as a curried Nix function with ``args``."""
        ...

    @abstractmethod
    async def apply(self, function: str | Self, /) -> Self:
        """Apply ``function`` to this value, returning the unforced result."""
        ...

    @abstractmethod
    async def auto_call(self) -> Self:
        """Auto-call a function using its default arguments, as ``nix-build`` does."""
        ...

    @abstractmethod
    async def build(self, *, build_mode: BuildMode | int | None = None, store: StoreT | None = None) -> dict[str, str]:
        """Realise this derivation, returning output name to store path.

        ``store`` was omitted here once, because each engine accepts only its
        own ``Store`` and naming :class:`AsyncStore` would have demanded that
        both accept the other's. ``StoreT`` answers that: it is this value's
        own store type, so each engine names its own.

        Issue #232 measured what the omission cost.
        ``nanopynix_helpers.build`` had to import ``nanopynix.rpc`` to reach
        this parameter, and beartype checks an annotation at call time, so the
        helper then refused every value that ``nanopynix.inproc`` makes.
        """
        ...


@runtime_checkable
class AsyncStore(Protocol):
    """The common asynchronous Store lifecycle and path-query interface."""

    __slots__ = ()  # in the body, and load-bearing -- see the module docstring

    @abstractmethod
    async def __aenter__(self) -> Self: ...

    @abstractmethod
    async def __aexit__(self, *args: object) -> None: ...

    @property
    @abstractmethod
    def is_open(self) -> bool:
        """Whether this store holds an open Nix store.

        A plain attribute rather than a coroutine, because the session reads it
        to decide whether it may write the global settings, and that decision
        must not need the worker.
        """
        ...

    @abstractmethod
    async def open(self) -> None:
        """Open the underlying store."""
        ...

    @abstractmethod
    async def close(self, *, force: bool = False) -> None:
        """Close the underlying store, optionally closing its evaluator first."""
        ...

    @abstractmethod
    async def uri(self) -> str:
        """Return the canonical URI of this store."""
        ...

    @abstractmethod
    async def store_dir(self) -> str:
        """Return this store's logical store directory."""
        ...

    @abstractmethod
    async def parse_store_path(self, path: str, /) -> StorePath:
        """Validate and normalise ``path`` as a Nix store path."""
        ...

    @abstractmethod
    async def is_valid_path(self, path: str | StorePath, /) -> bool:
        """Return whether ``path`` is valid in this store."""
        ...

    @abstractmethod
    async def query_path_info(self, path: str | StorePath, /) -> PathInfo:
        """Return metadata for a valid store path."""
        ...

    @abstractmethod
    async def dump_db(
        self,
        paths: Sequence[str | StorePath],
        /,
        *,
        show_derivers: bool = True,
        show_hash: bool = True,
    ) -> str:
        """Return the registration text for *paths*, as ``nix-store --dump-db`` does.

        ``nix-store --load-db`` reads this text back. It builds the database
        from nothing, and it adds to a database that exists already, so it can
        register a closure on a machine that Nix never ran on.

        **Give the whole closure.** This function registers the paths that it
        gets, and nothing more. Call :meth:`compute_fs_closure` first, or the
        database names a path that the machine does not have.

        The records come out in the order of *paths*. The order does not change
        the database, because ``--load-db`` adds every path before it resolves
        any reference.

        The text is not the text that ``pkgs.closureInfo`` writes. Both texts
        are valid input for ``--load-db``, and the fields differ:

        =============  ==========================  =========================
        field          this function               ``closureInfo``
        =============  ==========================  =========================
        NAR hash       base16, with no prefix      ``sha256:`` and base32
        deriver        the deriver of the path     always empty
        =============  ==========================  =========================

        Set *show_hash* to false to leave out the NAR hash and the NAR size,
        and *show_derivers* to false to leave the deriver empty.
        """
        ...

    @abstractmethod
    async def query_all_valid_paths(self) -> list[StorePath]:
        """Return every valid path registered in this store."""
        ...

    @abstractmethod
    async def compute_fs_closure(
        self,
        path: str | StorePath,
        /,
        *,
        flip_direction: bool = False,
        include_outputs: bool = False,
        include_derivers: bool = False,
    ) -> list[StorePath]:
        """Return the filesystem closure of ``path``."""
        ...

    @abstractmethod
    async def query_derivation_outputs(self, path: str | StorePath, /) -> list[StorePath]:
        """Return output paths declared by a derivation."""
        ...

    @abstractmethod
    async def query_valid_derivers(self, path: str | StorePath, /) -> list[StorePath]:
        """Return valid derivations that produced ``path``."""
        ...

    @abstractmethod
    async def query_referrers(self, path: str | StorePath, /) -> list[StorePath]:
        """Return valid store paths that reference ``path``."""
        ...

    @abstractmethod
    async def follow_links_to_store_path(self, path: str, /) -> StorePath:
        """Resolve a path that may traverse symlinks to its containing store path."""
        ...

    @abstractmethod
    async def query_path_from_hash_part(self, hash_part: str, /) -> StorePath | None:
        """Return the valid store path whose hash component is ``hash_part``, if any."""
        ...

    @abstractmethod
    async def query_substitutable_paths(self, paths: list[str | StorePath], /) -> list[StorePath]:
        """Return the subset of ``paths`` that can be substituted from a binary cache."""
        ...

    @abstractmethod
    async def get_build_log(self, path: str | StorePath, /) -> str | None:
        """Return the build log for ``path``, or ``None`` if no log is available."""
        ...

    @abstractmethod
    async def query_missing(
        self,
        derived_paths: list[str | StorePath],
        /,
    ) -> MissingInfo:
        """Return which of ``derived_paths`` still need to be built or substituted.

        A plain derivation path means all outputs here, for the reason
        :meth:`build_paths_with_results` gives. Both methods have to agree:
        a caller asks this one whether the other would do any work.
        """
        ...

    @abstractmethod
    async def build_paths_with_results(
        self,
        derived_paths: list[str | StorePath],
        /,
        *,
        build_mode: int = BuildMode.Normal.value,
        eval_store: Self | None = None,
    ) -> list[BuildResult]:
        """Build derived paths, treating a plain derivation path as all outputs.

        That reading belongs to this API and not to Nix, which takes a bare
        ``.drv`` as an opaque fetch and builds none of its outputs.
        :meth:`~nanopynix.models.DerivedPath.for_build` applies it, and each
        engine's ``Store`` calls that before the request reaches a binding --
        so ``nanopynix_bindings`` keeps Nix's meaning and this keeps the
        useful one.

        Each result reports the request back decomposed:
        :attr:`BuildResult.drv_path` is a store path and
        :attr:`BuildResult.outputs` is what was asked of it -- empty for an
        opaque fetch, ``["*"]`` for every output, else the named outputs Nix
        canonicalised. :class:`~nanopynix.models.DerivedPath` is the matching
        type for the ``^`` spelling accepted on the way in.

        ``eval_store`` is ``Self``, not ``AsyncStore``: both engines reject an
        ``eval_store`` from a different session at runtime, so no
        implementation can honestly accept an arbitrary store here. Typing it
        as ``AsyncStore`` made every concrete class fail conformance, and the
        blanket suppression that hid it disabled the whole ``AsyncStore``
        check -- including, until this change, whether either engine had any
        GC-root methods at all.

        The cost, since ``Self`` is in an argument position: code written
        against ``AsyncStore`` rather than a concrete store can pass ``None``
        here but cannot thread a store through. That is a real restriction on
        engine-agnostic callers, and it is the honest one -- such a caller has
        no way to prove the store it holds came from the same session.
        """
        ...

    @abstractmethod
    async def read_derivation(self, drv_path: str | StorePath, /) -> Derivation:
        """Parse and return the ``.drv`` file at ``drv_path``."""
        ...

    @abstractmethod
    async def write_dev_shell_derivation(self, drv_path: str | StorePath, get_env_script: str, /) -> str:
        """Store a rewrite of ``drv_path`` whose builder dumps its environment.

        This is the first half of what ``nix print-dev-env`` does. Build the
        returned derivation, and its output holds the environment as JSON.

        ``get_env_script`` is the text of the dumping script, and the caller
        owns it: Nix keeps its own copy inside the ``nix`` binary, where no
        library can reach it. The script has to enter the store before the
        derivation is hashed, which is why the text is the argument.

        Raises :class:`NixError` when the builder of ``drv_path`` is not
        ``bash``, which is the same refusal ``nix develop`` makes.
        """
        ...

    @abstractmethod
    async def collect_garbage(
        self,
        action: GcAction,
        /,
        *,
        ignore_liveness: bool = False,
        paths_to_delete: list[str | StorePath] | tuple[()] = (),
        max_freed: int = NO_GC_LIMIT,
    ) -> GcResult:
        """Run a garbage-collection pass; see :meth:`nanopynix.store.Store.collect_garbage`."""
        ...

    @abstractmethod
    async def add_temp_root(self, path: str | StorePath, /) -> None:
        """Keep ``path`` alive against the collector for as long as this process holds the store."""
        ...

    @abstractmethod
    async def add_perm_root(self, path: str | StorePath, gc_root: str, /) -> str:
        """Create the symlink ``gc_root`` -> ``path`` and return its resolved path."""
        ...

    @abstractmethod
    async def add_indirect_root(self, path: str, /) -> None:
        """Register an existing user-facing symlink as an indirect root."""
        ...

    @abstractmethod
    async def find_roots(self, *, censor: bool = False) -> list[GcRoot]:
        """Return the garbage collector's roots."""
        ...

    @abstractmethod
    async def registry_entries(self, *, fetch_settings: Mapping[str, str] | None = None) -> list[RegistryEntry]:
        """Every flake registry entry Nix would consult, in Nix's own order.

        A store is the receiver because the global layer downloads its file
        into one. Pass ``{"flake-registry": ""}`` to drop that layer, and the
        call reads local files alone.
        """
        ...

    @abstractmethod
    async def compute_store_path(
        self,
        path: str,
        *,
        name: str | None = None,
        method: str = DEFAULT_CA_METHOD,
        hash_algo: str = DEFAULT_HASH_ALGO,
    ) -> StorePath:
        """Compute the store path content-addressing ``path`` without adding it."""
        ...

    @abstractmethod
    async def add_to_store(
        self,
        path: str,
        *,
        name: str | None = None,
        method: str = DEFAULT_CA_METHOD,
        hash_algo: str = DEFAULT_HASH_ALGO,
    ) -> StorePath:
        """Add a file or directory to this store, returning its store path."""
        ...

    @abstractmethod
    async def store_dirs(self) -> StoreDirs:
        """Return this store's full set of configured directories."""
        ...

    @abstractmethod
    async def ensure_path(self, path: str | StorePath, /) -> None:
        """Make ``path`` valid in this store, substituting it if necessary."""
        ...

    @abstractmethod
    async def copy_closure(
        self,
        paths: list[str | StorePath],
        /,
        dest_store: Self,
        *,
        repair: bool = False,
        check_sigs: bool = True,
        substitute: bool = False,
    ) -> None:
        """Copy the closure of ``paths`` from this store to ``dest_store``.

        ``dest_store`` is ``Self`` for the same reason as
        :meth:`build_paths_with_results`'s ``eval_store`` -- both engines
        reject a store from another session, so no implementation can honestly
        accept an arbitrary ``AsyncStore`` here.
        """
        ...

    @abstractmethod
    async def optimise_store(self) -> None:
        """Reclaim disk space by hard-linking identical files in this store."""
        ...

    @abstractmethod
    async def verify_store(self, *, check_contents: bool = False, repair: bool = False) -> bool:
        """Check this store's consistency, returning whether errors were found."""
        ...


@runtime_checkable
class AsyncLockedFlake(Protocol):
    """The common lifecycle for an in-memory flake lock."""

    __slots__ = ()  # in the body, and load-bearing -- see the module docstring

    # `description` is deliberately not declared here, though both engines
    # carry it. A non-method member has to be a `@property` to stay abstract
    # and to keep `issubclass` usable, as `AsyncReplSession.line_editors` is,
    # and both engines hold `description` as a plain attribute -- rpc's as a
    # dataclass field. Converting them buys no caller anything.

    @abstractmethod
    async def eval(self) -> AsyncValue:
        """Evaluate this locked flake; see :meth:`nanopynix.rpc.LockedFlakeHandle.eval`.

        The value holds the outputs merged with the metadata of the flake, so
        a caller that copies the `nix` CLI takes
        :func:`nanopynix_helpers.flake_outputs` first. Issue #228 says why.
        """
        ...

    @abstractmethod
    async def write_lock_file(self) -> None:
        """Persist this locked flake's lock file to disk."""
        ...

    @abstractmethod
    async def metadata_json(self) -> str:
        """Return the JSON that ``nix flake metadata --json`` prints, as text.

        The whole object, and not one part of it: ``description``,
        ``originalUrl``, ``original``, ``resolvedUrl``, ``resolved``, ``url``,
        ``locked``, ``path``, ``locks``, ``fingerprint``, and the ``revision``,
        ``dirtyRevision``, ``revCount`` and ``lastModified`` of the flake when
        it has them.

        ``locks`` is the lock graph, which is the reason this exists. A flake
        lock is a graph: one node can be an input of an input, and a
        ``follows`` edge points at a path in the graph rather than at a
        reference. Nix writes that graph itself, through
        ``LockFile::toJSON()``.

        Text, and not a parsed object, because the text is what fidelity is
        claimed about. Nix builds it, and nothing here takes it apart. Call
        :func:`json.loads` on the result.

        Use :meth:`find_input` instead to ask about one input, which is a
        question about the graph rather than a rendering of it.
        """
        ...

    @abstractmethod
    async def find_input(self, path: Sequence[str], /) -> LockedNode | None:
        """Return the locked node at *path*, or ``None`` when there is none.

        *path* is an attribute path into the lock graph, so ``["nixpkgs"]``
        names a direct input and ``["mid", "leaf"]`` names an input of an
        input. A ``follows`` edge on the way is resolved, as Nix resolves it.

        This is the question ``InstallableFlake::nixpkgsFlakeRef`` asks to find
        out which ``nixpkgs`` a flake locks. ``pynix develop`` asks it for the
        same reason.

        ``None`` for a path that names no input, and also for one that names
        the root of the graph. The root carries no locked reference.
        """
        ...

    @abstractmethod
    async def release(self) -> None:
        """Release the worker-side handle for this locked flake. Idempotent."""
        ...


@runtime_checkable
class AsyncVerbosityController(Protocol[VerbosityT_co]):
    """A resource that reads and updates its own Nix log verbosity.

    The level belongs to the resource the caller holds, and not to the
    process. A session's level covers its store work and the threads Nix
    starts for itself; an evaluator's level covers that evaluator alone. See
    :class:`AsyncEvalSession` for how the two relate.

    Defined here, above its first user, rather than at the end of the module:
    :class:`AsyncEvalSession` extends it.
    """

    __slots__ = ()  # in the body, and load-bearing -- see the module docstring

    @abstractmethod
    async def get_verbosity(self) -> VerbosityT_co:
        """Return the level this resource logs at."""
        ...

    @abstractmethod
    async def set_verbosity(self, verbosity: LogLevelInput) -> VerbosityT_co:
        """Set the level this resource logs at, and return it."""
        ...


@runtime_checkable
class AsyncEvalSession[ValueT: AsyncValue = AsyncValue](AsyncVerbosityController[LogLevel], Protocol):
    """The common asynchronous evaluation and flake interface.

    Generic in the value type so an engine's evaluator yields that engine's
    values: ``inproc.EvalSession`` hands back ``inproc.Value``, not "some
    ``AsyncValue``". The parameter defaults to :class:`AsyncValue`, so a
    caller who does not care can still write a bare ``AsyncEvalSession``.

    Extends :class:`AsyncVerbosityController` rather than redeclaring
    ``get_verbosity``/``set_verbosity``. An evaluator owns its level: two
    evaluators of one session can hold different levels at the same time.
    Until an evaluator sets one, it follows its session, and a later
    ``Session.set_verbosity`` moves it. After it sets one, it keeps that
    level, and no session write moves it again. There is no way back to
    following the session.

    Two things stay at the session's level. A store is session-scoped, so
    store work logs at the session's level even while an evaluator of that
    session sits elsewhere. The threads Nix starts for itself, such as a
    substituter or a build hook, read a process-wide default that only the
    session writes, so an evaluator at ``DEBUG`` gets debug output from its
    own evaluation and not from those threads.

    A REPL is why the door is here at all -- ``pynix``'s ``:verbosity``
    command holds a repl session and nothing else.
    """

    __slots__ = ()  # in the body, and load-bearing -- see the module docstring

    @abstractmethod
    async def __aenter__(self) -> Self: ...

    @abstractmethod
    async def __aexit__(self, *args: object) -> None: ...

    @abstractmethod
    async def open(self) -> None:
        """Create this session's evaluator. Called automatically by ``async with``."""
        ...

    @abstractmethod
    async def close(self) -> None:
        """Release all values exported from this session and free the evaluator."""
        ...

    @abstractmethod
    async def configure(
        self,
        eval_settings: NixEvalSettings | None = None,
        fetch_settings: NixFetchSettings | None = None,
    ) -> None:
        """Apply live-mutable eval and fetch settings to the open evaluator."""
        ...

    @abstractmethod
    async def file(self, path: str, /) -> ValueT:
        """Evaluate the Nix expression in the file at ``path``."""
        ...

    @abstractmethod
    async def string(self, expr: str, path: str = "<string>", /) -> ValueT:
        """Evaluate the Nix expression ``expr``."""
        ...

    @abstractmethod
    async def reset_file_cache(self) -> None:
        """Discard parsed file cache entries before re-evaluating source files."""
        ...

    @abstractmethod
    async def set_eval_counters_enabled(self, enabled: bool) -> bool:
        """Turn the evaluation counters on, or off, and report the new state.

        The counters back the numeric fields of :meth:`statistics`. Nix leaves
        them off unless ``NIX_SHOW_STATS`` is set, because each increment costs
        an atomic write.

        **The scope is a process, and not this evaluator.** On ``rpc`` that
        process is the worker, which is why the call goes over the wire rather
        than setting a static here. See :meth:`statistics`, and issue #118.
        """
        ...

    @abstractmethod
    async def statistics(self) -> dict[str, Any]:
        """Report what this evaluator did, as ``NIX_SHOW_STATS=1 nix`` reports it.

        The report holds the counts of the values, the environments and the
        attribute sets, the time, and the state of the collector. The
        ``primops``, ``functions`` and ``attributes`` tables need the
        ``count-calls`` eval setting, which is off by default because the
        counting costs time.

        Nix decides the fields, and it changes them between versions. Read a
        field that a version supplies, and do not require one.

        .. warning::

           **This report is unreliable when one process holds more than one
           evaluator.** Two things belong to the process, and not to this
           evaluator:

           * ``nrExprs`` and ``nrThunks`` are static counters of
             ``libnixexpr``, so each evaluator reports the sum of every
             evaluator in the process.
           * The switch that turns counting on is one static as well, so an
             evaluator cannot count while another beside it does not.

           The other thirteen counted fields belong to this evaluator alone.
           They are still not predictable on ``inproc``, which evaluates in
           parallel in one process, because the switch above decides them all.
           ``rpc`` gives each worker its own process, so its numbers are
           stable.

           The three call tables need no such care. ``count_calls`` writes
           them into the maps of one evaluator, so they are exact on both
           engines. Issue #118 tracks the repair, and the reason that upstream
           Nix keeps a static.

           ``build_info()["capabilities"]["eval_statistics"]`` reports whether
           this build has the report at all. It is ``True`` on every supported
           Nix, because the patch that exposes the statistics reaches 2.34 and
           later, and ``supportedNixFloor`` is 2.34.
        """
        ...

    @abstractmethod
    async def lock_flake(
        self,
        ref: str,
        /,
        *,
        update_inputs: bool | list[str] = False,
        write_lock_file: bool = True,
    ) -> AsyncLockedFlake:
        """Lock a flake, optionally updating inputs; see :meth:`nanopynix.rpc.EvalSession.lock_flake`."""
        ...

    @abstractmethod
    async def eval_flake(self, ref: str, /, *, write_lock_file: bool = True) -> ValueT:
        """Lock and evaluate a flake in one step; see :meth:`nanopynix.rpc.EvalSession.eval_flake`.

        The value holds the outputs merged with the metadata of the flake, so
        a caller that copies the `nix` CLI takes
        :func:`nanopynix_helpers.flake_outputs` first. Issue #228 says why.
        """
        ...

    @abstractmethod
    async def get_flake(self, ref: str, /) -> FlakeRef:
        """Parse and resolve a flake reference without evaluating its outputs."""
        ...


@runtime_checkable
class AsyncReplSession[ValueT: AsyncValue = AsyncValue](AsyncEvalSession[ValueT], Protocol):
    """An :class:`AsyncEvalSession` that keeps a persistent Nix lexical scope.

    Extending rather than standing alone mirrors both engines, whose
    ``ReplSession`` subclasses their ``EvalSession``. That is not incidental:
    a binding entered with :meth:`line` is only useful if :meth:`string` and
    :meth:`file` can then see it, so the repl surface and the eval surface
    are one interface.
    """

    __slots__ = ()  # in the body, and load-bearing -- see the module docstring

    @property
    @abstractmethod
    def line_editors(self) -> tuple[str, ...]:
        """Editor-name substrings that support Nix's ``+LINE`` argument."""
        ...

    @abstractmethod
    async def line(self, text: str, path: str = "<string>", /) -> ValueT | None:
        """Process one Nix REPL line. Bindings return ``None``; expressions return a value."""
        ...

    @abstractmethod
    async def load_file(self, path: str, /) -> ValueT:
        """Load a Nix expression file as ``nix repl :load`` does."""
        ...

    @abstractmethod
    async def add_attrs(self, value: ValueT, /) -> list[str]:
        """Add all attributes from ``value`` to this REPL's lexical scope."""
        ...

    @abstractmethod
    async def scope_names(self) -> list[str]:
        """Return the identifiers visible in this REPL's lexical scope."""
        ...


@runtime_checkable
class AsyncSession[
    StoreT: AsyncStore = AsyncStore,
    EvalT: AsyncEvalSession[Any] = AsyncEvalSession[Any],
    ReplT: AsyncReplSession[Any] = AsyncReplSession[Any],
](AsyncVerbosityController[LogLevel], Protocol):
    """The entry point of an engine: what a caller opens, and what it hands out.

    The last shared class to get a protocol, and the gap was not eight
    scattered omissions. ``capture_logs``, ``log_stream``, ``repl``,
    ``set_settings``, ``settings``, ``settings_provenance``, ``store`` and
    ``subscribe`` are carried by both engines and declared by nothing, and
    every one of them belongs to this class and to no other. One missing
    protocol, eight symptoms.

    Generic in the three types an engine hands back, for the reason
    :class:`AsyncEvalSession` is generic in its value type: ``inproc.Session``
    yields ``inproc.Store``, not "some :class:`AsyncStore`".

    Two consequences of that, and both are load-bearing rather than incidental.

    ``StoreT`` is invariant of necessity: :meth:`eval` takes a store and
    :meth:`store` returns one. So a *bare* ``AsyncSession`` means
    ``AsyncSession[AsyncStore, ...]``, and neither engine is one -- correctly,
    because an engine's ``eval`` must reject the other engine's store. Annotate
    with the parameters solved, as ``nanopynix/tests/test_protocols.py`` does,
    and not with the bare name.

    The evaluator bounds are ``[Any]`` and not the bare protocol names.
    ``AsyncEvalSession``'s own value parameter is invariant too, so the bare
    name would mean ``AsyncEvalSession[AsyncValue]`` and exclude every engine,
    each of which yields its own value type. Parameterising ``AsyncSession`` by
    the value as well would say it more precisely, at the cost of a fourth
    parameter that every caller would have to spell.

    Extends :class:`AsyncVerbosityController` for the same reason
    :class:`AsyncEvalSession` does: the verbosity is process-wide, and a
    session is one door onto it rather than the owner of its own.
    """

    __slots__ = ()  # in the body, and load-bearing -- see the module docstring

    @abstractmethod
    async def __aenter__(self) -> Self: ...

    @abstractmethod
    async def __aexit__(self, *args: object) -> None: ...

    @abstractmethod
    async def open(self) -> None:
        """Start the engine. Called automatically by ``async with``."""
        ...

    @abstractmethod
    async def close(self) -> None:
        """Release everything this session owns, and stop the engine.

        Declared with no parameters, which is the shape both engines answer
        to. inproc also accepts ``wait``, ``timeout`` and ``force``, because
        it must drain threads it cannot kill where rpc terminates a process --
        the engine parity ledger records that under ``Session.close:params``.
        Widening an override with keyword arguments that all default is
        compatible, so the ledger entry and this declaration agree.
        """
        ...

    @abstractmethod
    def store(self, uri: StoreConfig | str | None = None) -> StoreT:
        """Return a store for this session, not yet open."""
        ...

    @abstractmethod
    def eval(
        self,
        store: StoreT,
        *,
        build_store: StoreT | None = None,
        eval_settings: NixEvalSettings | None = None,
        fetch_settings: NixFetchSettings | None = None,
    ) -> EvalT:
        """Return an evaluator bound to ``store``, not yet open."""
        ...

    @abstractmethod
    def repl(
        self,
        store: StoreT,
        *,
        build_store: StoreT | None = None,
        eval_settings: NixEvalSettings | None = None,
        fetch_settings: NixFetchSettings | None = None,
        line_editors: Sequence[str] | None = None,
    ) -> ReplT:
        """Return an evaluator that keeps a persistent Nix REPL scope.

        ``line_editors`` is per-call rather than per-session, because it
        describes one interactive front-end and nothing else consumes it.
        ``None`` means "use the engine's default".
        """
        ...

    @abstractmethod
    async def settings(self, *, overridden_only: bool = False) -> dict[str, str]:
        """Read the Nix settings this session's engine currently holds."""
        ...

    @abstractmethod
    async def set_settings(self, settings: NixGlobalSettings) -> dict[str, str]:
        """Apply global Nix settings, and return the values that took effect."""
        ...

    @abstractmethod
    async def settings_provenance(self) -> SettingsProvenance:
        """Report where each setting this session applied came from."""
        ...

    @abstractmethod
    def log_stream(self) -> AsyncIterator[LogEvent]:
        """Iterate this session's Nix log events until it closes.

        Bounded: a caller that iterates slowly loses the oldest events rather
        than delaying Nix. Both engines return
        :func:`nanopynix.logging.bus_log_stream`.
        """
        ...

    @abstractmethod
    def subscribe(self, callback: LogCallback) -> BusSubscription:
        """Call ``callback`` with each log event, and once with ``None`` at the end.

        Returns a handle -- call ``.unsubscribe()`` to stop.
        """
        ...

    @abstractmethod
    def capture_logs(self, *, max_events: int | None = None, wait_timeout: float | None = None) -> LogCapture:
        """Record typed log events for the duration of an ``async with`` block."""
        ...


__all__ = [
    "AsyncEvalSession",
    "AsyncLockedFlake",
    "AsyncReplSession",
    "AsyncSession",
    "AsyncStore",
    "AsyncValue",
    "AsyncVerbosityController",
    "GcAction",
]
