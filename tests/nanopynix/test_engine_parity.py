"""Signature parity between the ``inproc`` and ``rpc`` engines.

``test_protocols.py`` already checks that each engine satisfies the protocols
in :mod:`nanopynix.protocols`. That is a different, weaker claim: a protocol
pins only the members it declares, so a method present on one engine and
absent on the other -- or present on both under different names, or with its
parameters in a different order -- is invisible to it. Every such difference
found here was invisible there.

How weak that is, concretely: :class:`~nanopynix.protocols.AsyncValue`
declares eight members, all lifecycle and forcing (``force``, ``to_python``,
``realise_string``, ``realise_argv``, ``edit_location``, ``release``, and the
two context-manager hooks). Attribute access, coercion,
calling, list indexing and building -- the entire surface a caller actually
reaches for -- sit outside it, which is exactly where the divergences below
are concentrated.

The rule this encodes is the project's own: **process isolation is the only
thing rpc has that inproc does not, so an asymmetry is a defect unless
process isolation forces it.** Accordingly every difference must appear in
:data:`LEDGER` with a justification, and the check runs both ways --
an unlisted difference fails as new drift, and a listed difference that has
stopped occurring fails so the entry gets deleted rather than accumulating
into a rubber stamp.

The ledger is deliberately *not* a list of things that are fine. Entries marked
DEFECT record work to do, and are here so the drift is counted instead of
rediscovered. There are none left: every remaining entry is TRANSPORT, and the
retirement comments between them say what happened to the rest.

That is a floor to hold, not a finish line. This file compares *names* --
members, and parameter lists -- so it is blind to two classes with different
names for the same thing (which is how the object-lifetime exceptions diverged
unnoticed) and to two identical signatures that behave differently.
:mod:`tests.nanopynix.test_engine_parity_semantics` is the other half, and the
one that grows from here.
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass
from typing import Any

from nanopynix import inproc
from nanopynix.rpc.client import _session as rpc_private
from nanopynix.rpc.client import session as rpc_session
from nanopynix.rpc.client import store as rpc_store

# Every rpc call can outlive its worker -- the process can die, or wedge, with
# the caller holding a socket. An in-process call has no such failure mode, so
# there is nothing for an inproc `timeout` to mean. This is the one difference
# process isolation genuinely forces, it is always the last parameter, and
# listing its ~30 occurrences individually would drown the ledger in noise.
TIMEOUT_PARAM = "timeout"

PAIRS: list[tuple[str, type, type]] = [
    ("Session", inproc.Session, rpc_session.Session),
    ("Store", inproc.Store, rpc_store.Store),
    ("EvalSession", inproc.EvalSession, rpc_private.EvalSession),
    ("ReplSession", inproc.ReplSession, rpc_private.ReplSession),
    ("Value", inproc.Value, rpc_private.ValueProxy),
    ("LockedFlake", inproc.LockedFlake, rpc_private.LockedFlakeHandle),
]

# Pairs whose classes subclass another pair's classes on *both* engines, mapped
# to that base pair. See differences_for's `derived` argument for why they are
# treated differently. test_derived_pairs_really_subclass_their_base keeps this
# honest -- the filter is only sound while the subclass relationship holds.
DERIVED_PAIRS: dict[str, str] = {"ReplSession": "EvalSession"}


@dataclass(frozen=True)
class Difference:
    """One way the two engines disagree about a member."""

    pair: str
    member: str
    kind: str
    detail: str

    @property
    def key(self) -> str:
        return f"{self.pair}.{self.member}:{self.kind}"


def _public_members(cls: type) -> dict[str, Any]:
    return {name: obj for name, obj in inspect.getmembers(cls) if not name.startswith("_")}


def _parameters(func: Any) -> list[str] | None:
    """Parameter names after ``self``, with a trailing ``timeout`` normalised away."""
    try:
        names = [p.name for p in inspect.signature(func).parameters.values()][1:]
    except (TypeError, ValueError):
        # Builtins and C-level callables have no introspectable signature.
        return None
    return names[:-1] if names and names[-1] == TIMEOUT_PARAM else names


def _compare_member(pair: str, name: str, left: Any, right: Any) -> list[Difference]:
    left_is_property, right_is_property = isinstance(left, property), isinstance(right, property)
    if left_is_property != right_is_property:
        return [
            Difference(pair, name, "property", f"inproc property={left_is_property} rpc property={right_is_property}")
        ]
    if left_is_property or not (callable(left) and callable(right)):
        return []

    found: list[Difference] = []
    left_async = inspect.iscoroutinefunction(left)
    right_async = inspect.iscoroutinefunction(right)
    if left_async != right_async:
        found.append(Difference(pair, name, "async", f"inproc async={left_async} rpc async={right_async}"))

    left_params, right_params = _parameters(left), _parameters(right)
    if left_params is not None and right_params is not None and left_params != right_params:
        found.append(Difference(pair, name, "params", f"inproc{left_params} rpc{right_params}"))
    return found


def differences_for(pair: str, inproc_cls: type, rpc_cls: type, *, derived: bool = False) -> list[Difference]:
    """Report every way the two engines' versions of one class disagree.

    ``derived`` marks a pair whose two classes each subclass the two classes of
    another pair in :data:`PAIRS` -- today only ``ReplSession`` over
    ``EvalSession``. For those, differences in members neither subclass defines
    itself are dropped, because they are the base pair's differences seen
    through inheritance and are already reported there. Reporting them twice
    would make the ledger count one piece of work as two. An override always
    appears in its own class body, so a genuine subclass-level divergence is
    never masked.
    """
    left, right = _public_members(inproc_cls), _public_members(rpc_cls)
    found: list[Difference] = [
        Difference(pair, name, "inproc-only", "present on inproc, absent on rpc")
        for name in sorted(set(left) - set(right))
    ]
    found.extend(
        Difference(pair, name, "rpc-only", "present on rpc, absent on inproc")
        for name in sorted(set(right) - set(left))
    )
    for name in sorted(set(left) & set(right)):
        found.extend(_compare_member(pair, name, left[name], right[name]))
    if derived:
        declared_here = set(vars(inproc_cls)) | set(vars(rpc_cls))
        found = [difference for difference in found if difference.member in declared_here]
    return found


def observed_differences() -> list[Difference]:
    return [
        difference
        for pair, left, right in PAIRS
        for difference in differences_for(pair, left, right, derived=pair in DERIVED_PAIRS)
    ]


# Every known difference, with why it exists.
#
# TRANSPORT -- process isolation genuinely forces it. These are the only
#              entries that are *allowed* to stay.
# DEFECT    -- the same concept spelled two ways, or a capability one engine
#              simply lacks. Each of these is work to do; they are recorded so
#              the drift is counted rather than rediscovered.
LEDGER: dict[str, str] = {
    # ── Session ────────────────────────────────────────────────────
    "Session.run:inproc-only": "TRANSPORT: dispatches onto the session's Nix thread; rpc has no such thread to target.",
    # "Session.capture_logs:rpc-only" retired here. LogCapture was rpc's, in
    # rpc.client.session, over a ContextVar in rpc.client._pool -- but nothing
    # about recording log events depends on where Nix runs, and inproc already
    # had every part: the bus, the operation ids, and the request_finalized
    # events that tell wait() a capture is complete. The class moved down to
    # nanopynix.logging and both engines return it. The one thing that had to
    # be built was inproc's end of the ACTIVE_LOG_CAPTURES tagging contract,
    # which lives in _next_operation_id -- the allocation point all four of
    # inproc's dispatch paths share, as WorkerClient.invoke is rpc's.
    "Session.claim_eval:rpc-only": "TRANSPORT: leases a worker-side evaluator slot. Nothing to lease in-process.",
    "Session.release_eval:rpc-only": "TRANSPORT: the release half of claim_eval.",
    # Recorded as a DEFECT until it was looked at properly. All three
    # parameters follow from one fact -- inproc's Nix work runs on threads in
    # this process, and a thread cannot be killed -- so closing has to wait for
    # that work (`wait`), bound the wait (`timeout`), and be able to drop what
    # has not started yet (`force`), with `resume()` behind them so a close that
    # gives up leaves the session usable. rpc's work runs in a subprocess that
    # gets terminated and then killed; there is no wait phase, so none of the
    # three has anything to refer to. This is TIMEOUT_PARAM's note read in the
    # other direction: there it is an inproc call that has no timeout to mean,
    # here it is an rpc close.
    #
    # `force` is the one worth spelling out, because a plausible rpc reading
    # exists and is wrong: "kill the worker now" abandons work that has already
    # started, where inproc's force only drops work that has not. Same word,
    # more destructive act -- worse than not having it.
    "Session.close:params": "TRANSPORT: inproc must drain threads it cannot kill; rpc terminates a process. wait/timeout/force bound a wait phase rpc does not have.",
    # ── Store ──────────────────────────────────────────────────────
    "Store.call:inproc-only": "TRANSPORT: runs an L1 store method on the Nix thread.",
    "Store.rpc:rpc-only": "TRANSPORT: the generated StoreService proxy -- the escape hatch to the wire itself.",
    "Store.store_handle:rpc-only": "TRANSPORT: worker-side handle used to wire a store into a remote session.",
    # Nothing else. Every remaining Store operation exists on both engines,
    # and `nanopynix.protocols.AsyncStore` declares them, so a new one added to
    # a single engine fails conformance in tests/nanopynix/test_protocols.py
    # before it can reach this ledger.
    # ── EvalSession ────────────────────────────────────────────────
    "EvalSession.run:inproc-only": "TRANSPORT: dispatches onto the evaluator's dedicated thread.",
    "EvalSession.has_pending_work:inproc-only": "TRANSPORT: introspects that same thread's queue.",
    "EvalSession.release_locked_flake:rpc-only": "TRANSPORT: frees a worker-side handle; inproc's LockedFlake is a local object.",
    # get_verbosity/set_verbosity were here as rpc-only. inproc's EvalSession
    # has them now, delegating to its Session: verbosity is process-wide, so
    # this is one setting reachable from two places rather than two settings.
    # rpc has always been shaped that way, and a REPL is why -- pynix's
    # :verbosity command holds a ReplSession and nothing else.
    # ── ReplSession ────────────────────────────────────────────────
    # Fourteen entries used to live here. inproc's ReplSession was a narrow
    # line-oriented wrapper holding an EvalSession; rpc's subclassed one. The
    # ledger recorded the disagreement without resolving it, because the
    # question underneath was a definition, not a bug: what *is* a repl
    # session?
    #
    # It is an interactive evaluator. Entering `x = 1` is only useful if the
    # same object can then evaluate `x + 1`, so the repl surface and the eval
    # surface cannot be separated -- and rpc's own docstring had said exactly
    # that, promising bindings would stay visible to later string and file
    # calls, while inproc's shape made it unexpressible. inproc's ReplSession now
    # subclasses EvalSession too, which is also what the only real consumer
    # needs: pynix's repl makes more calls to inherited EvalSession methods
    # than to repl-specific ones.
    #
    # Thirteen of the fourteen were the inherited EvalSession surface and are
    # simply gone. The fourteenth, `line_editors`, was pure client config with
    # no transport dimension; inproc takes it as a `repl()` argument.
    #
    # ReplSession now contributes no entries at all: its own surface matches,
    # and what it inherits is counted once against EvalSession (see
    # DERIVED_PAIRS).
    # ── Value ──────────────────────────────────────────────────────
    # Twelve entries used to live here: the `as_*` strict family (inproc-only)
    # and a `coerce_*` family (rpc-only). Both are gone, by opposite routes.
    #
    # `as_*` is the FFI boundary -- turning a Nix value into a Python one is the
    # one thing no Nix expression can do -- so it was added to rpc, and both
    # engines now raise the same NixTypeError because the check runs in the
    # worker.
    #
    # `coerce_*` was deleted instead. `coerce_str` was `builtins.toString`
    # reimplemented (and wrong: "true" where Nix says "1"), and coerce_int/
    # float/bool had no Nix counterpart at all. `apply()` replaced the lot on
    # both engines, and reaches every other builtin besides.
    # Three more entries retired with those: inproc's `type() -> str` (rpc
    # spelled it `get_type() -> NixType`, so porting a caller meant changing
    # how the result was compared, not just the name), rpc-only `force_as`,
    # and inproc's `close()` alias for `release()`. All resolved by deleting
    # the odd one out rather than by adding its twin to the other engine.
    "Value.nix_type:rpc-only": "TRANSPORT: a sync property peeking at the type already known locally, no round trip. In-process there is no round trip to avoid, so `type` is always cheap and a separate peek would mean nothing.",
    "Value.handle:rpc-only": "TRANSPORT: the worker-side value handle this proxy stands for.",
    # Three more retired here, the last of the Value cluster. `attr` and
    # `list_get` were "Value.attr:async"/"Value.list_get:async": inproc awaited
    # them, rpc returned a lazy child synchronously, so chained selection had
    # to be spelled two ways. inproc adopted rpc's shape rather than the
    # reverse -- deferring is the property worth having, and rpc could not give
    # it up without turning every `a.attr("x").attr("y")` into nested awaits.
    # "Value.call:params" was an arity difference; both now take `*args` and
    # share one curried implementation on CoreValue.
}


def test_engine_parity_ledger_is_exact() -> None:
    """Every engine difference is accounted for, and every account is still real."""
    observed = {difference.key: difference for difference in observed_differences()}

    undocumented = sorted(set(observed) - set(LEDGER))
    assert not undocumented, "new inproc/rpc divergence -- justify it in LEDGER or remove it:\n" + "\n".join(
        f"  {key}  ({observed[key].detail})" for key in undocumented
    )

    stale = sorted(set(LEDGER) - set(observed))
    assert not stale, "LEDGER documents differences that no longer exist -- delete these entries:\n" + "\n".join(
        f"  {key}" for key in stale
    )


# ── The harness's own teeth ───────────────────────────────────────────
#
# A drift detector that cannot detect drift passes silently forever. These
# run the comparison against synthetic pairs whose divergence is known.


class _Reference:
    async def shared(self, name: str) -> None: ...
    async def only_here(self) -> None: ...
    async def renamed_param(self, index: int) -> None: ...
    async def reordered(self, store: str, mode: int) -> None: ...
    async def sync_on_one_side(self) -> None: ...


class _Drifted:
    # ASYNC109 wants asyncio.timeout here, but the whole point of this method
    # is to *be* the shape the harness normalises away.
    async def shared(self, name: str, timeout: float | None = None) -> None: ...  # noqa: ASYNC109 -- deliberately mirrors the rpc signature under test
    async def renamed_param(self, idx: int) -> None: ...
    async def reordered(self, mode: int, store: str) -> None: ...
    def sync_on_one_side(self) -> None: ...
    async def added_here(self) -> None: ...


def _keys(pair: str = "Synthetic") -> set[str]:
    return {difference.key for difference in differences_for(pair, _Reference, _Drifted)}


def test_derived_pairs_really_subclass_their_base() -> None:
    """The inherited-difference filter is only sound while the subclassing holds.

    If either engine stopped subclassing, ``differences_for`` would go on
    dropping real divergences as "already reported on the base pair" when
    nothing is inherited at all -- silently, and in the direction that hides
    drift rather than inventing it.
    """
    by_name = {name: (inproc_cls, rpc_cls) for name, inproc_cls, rpc_cls in PAIRS}
    for derived_name, base_name in DERIVED_PAIRS.items():
        derived_inproc, derived_rpc = by_name[derived_name]
        base_inproc, base_rpc = by_name[base_name]
        assert issubclass(derived_inproc, base_inproc), f"inproc {derived_name} no longer subclasses {base_name}"
        assert issubclass(derived_rpc, base_rpc), f"rpc {derived_name} no longer subclasses {base_name}"


def test_harness_detects_a_member_present_on_only_one_side() -> None:
    assert "Synthetic.only_here:inproc-only" in _keys()
    assert "Synthetic.added_here:rpc-only" in _keys()


def test_harness_detects_a_renamed_parameter() -> None:
    """A renamed parameter is a silent break for keyword callers.

    ``Value.list_get`` spelled this ``index`` on one engine and ``idx`` on the
    other until it was unified; the synthetic pair keeps the detector honest
    now that no real divergence of this shape remains.
    """
    assert "Synthetic.renamed_param:params" in _keys()


def test_harness_detects_reordered_parameters() -> None:
    """Reordered parameters are silent breakage for positional callers.

    ``Value.build`` took ``(store, build_mode)`` in opposite orders across the
    engines until it was unified; the synthetic pair below is what keeps this
    detector covered now that the real instance is gone.
    """
    assert "Synthetic.reordered:params" in _keys()


def test_harness_detects_a_sync_async_split() -> None:
    assert "Synthetic.sync_on_one_side:async" in _keys()


def test_harness_normalises_a_trailing_timeout_away() -> None:
    """`shared` differs only by rpc's trailing timeout, which is not a divergence."""
    assert not any(key.startswith("Synthetic.shared:") for key in _keys())
