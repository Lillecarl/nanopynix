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

The ledger is deliberately *not* a list of things that are fine. Most entries
are marked DEFECT: they record work to do, and they are here so the drift is
counted instead of rediscovered.
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


def differences_for(pair: str, inproc_cls: type, rpc_cls: type) -> list[Difference]:
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
    return found


def observed_differences() -> list[Difference]:
    return [difference for pair, left, right in PAIRS for difference in differences_for(pair, left, right)]


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
    "Session.capture_logs:rpc-only": "DEFECT: log capture is engine-independent. inproc callers have no equivalent.",
    "Session.claim_eval:rpc-only": "TRANSPORT: leases a worker-side evaluator slot. Nothing to lease in-process.",
    "Session.release_eval:rpc-only": "TRANSPORT: the release half of claim_eval.",
    "Session.repl:rpc-only": "DEFECT: inproc puts repl() on EvalSession instead -- see EvalSession.repl below. One of the two placements is wrong.",
    "Session.close:params": "DEFECT: inproc takes wait/timeout/force, rpc takes nothing. Shutdown is exactly where rpc needs those knobs most.",
    "Session.eval:params": "DEFECT: inproc accepts a separate build_store, rpc cannot. Evaluating against one store while building in another is not a transport concern.",
    # ── Store ──────────────────────────────────────────────────────
    "Store.call:inproc-only": "TRANSPORT: runs an L1 store method on the Nix thread.",
    "Store.rpc:rpc-only": "TRANSPORT: the generated StoreService proxy -- the escape hatch to the wire itself.",
    "Store.store_handle:rpc-only": "TRANSPORT: worker-side handle used to wire a store into a remote session.",
    # The rest are ordinary store operations with no worker in them. libstore
    # exposes all of these in-process; nothing but effort is missing.
    **{
        f"Store.{name}:rpc-only": "DEFECT: a plain libstore operation the inproc engine never got. No transport reason."
        for name in (
            "add_indirect_root",
            "add_perm_root",
            "add_temp_root",
            "add_to_store",
            "compute_store_path",
            "copy_closure",
            "ensure_path",
            "find_roots",
            "optimise_store",
            "store_dirs",
            "verify_store",
        )
    },
    # ── EvalSession ────────────────────────────────────────────────
    "EvalSession.run:inproc-only": "TRANSPORT: dispatches onto the evaluator's dedicated thread.",
    "EvalSession.has_pending_work:inproc-only": "TRANSPORT: introspects that same thread's queue.",
    "EvalSession.repl:inproc-only": "DEFECT: the mirror of Session.repl above -- the two engines disagree on which object owns repl().",
    "EvalSession.get_flake:rpc-only": "DEFECT: resolving a flake ref without evaluating it is pure libexpr.",
    "EvalSession.eval_locked_flake:rpc-only": "DEFECT: inproc has lock_flake but no way to evaluate the result by handle.",
    "EvalSession.release_locked_flake:rpc-only": "TRANSPORT: frees a worker-side handle; inproc's LockedFlake is a local object.",
    "EvalSession.write_lock_file:rpc-only": "DEFECT: writing a lock file touches the filesystem, not the worker.",
    "EvalSession.get_verbosity:rpc-only": "DEFECT: inproc exposes verbosity on Session only. Same setting, two homes.",
    "EvalSession.set_verbosity:rpc-only": "DEFECT: as get_verbosity.",
    "EvalSession.string:params": "DEFECT: the same parameter named `expression` on inproc and `expr` on rpc.",
    # ── ReplSession ────────────────────────────────────────────────
    # inproc's ReplSession is a narrow line-oriented object; rpc's also carries
    # the whole EvalSession surface. Neither shape is forced by transport.
    **{
        f"ReplSession.{name}:rpc-only": "DEFECT: rpc's ReplSession re-exposes the EvalSession surface; inproc's does not. The two disagree on what a repl session *is*."
        for name in (
            "close",
            "configure",
            "eval_flake",
            "eval_locked_flake",
            "file",
            "get_flake",
            "get_verbosity",
            "line_editors",
            "lock_flake",
            "open",
            "release_locked_flake",
            "set_verbosity",
            "string",
            "write_lock_file",
        )
    },
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
    # Not a rename: inproc.type is `async -> str` (a name like "string") and
    # rpc.get_type is `async -> NixType`. Porting a caller means changing how
    # the result is compared, not just what the method is called.
    "Value.type:inproc-only": "DEFECT: rpc spells this get_type *and* returns a different type -- NixType enum vs inproc's str name.",
    "Value.get_type:rpc-only": "DEFECT: inproc spells this `type` and returns a str name rather than this NixType enum.",
    "Value.nix_type:rpc-only": "TRANSPORT: a sync property peeking at the type already known locally, no round trip. In-process there is no round trip to avoid, so `type` is always cheap and a separate peek would mean nothing.",
    "Value.close:inproc-only": "DEFECT: rpc spells value teardown release() only; inproc has both close() and release().",
    "Value.force_as:rpc-only": (
        "DEFECT: this is rpc's *strict* accessor -- the real counterpart of inproc's as_int/as_float/"
        "as_bool/as_string, reached by NixType argument rather than by four method names. One of the "
        "two spellings should win; see the strict-family entries above."
    ),
    "Value.handle:rpc-only": "TRANSPORT: the worker-side value handle this proxy stands for.",
    "Value.attr:async": "DEFECT: inproc awaits, rpc returns a proxy synchronously. Callers cannot be written once against both.",
    "Value.list_get:async": "DEFECT: as Value.attr.",
    "Value.list_get:params": "DEFECT: the same parameter named `index` on inproc and `idx` on rpc. Positional on both, so only a keyword caller breaks.",
    "Value.call:params": "DEFECT: an arity difference, not a rename -- inproc takes exactly one `argument`, rpc takes `*args`. A two-argument call works on one engine only.",
    "Value.build:params": "DEFECT (cosmetic): order is reversed, (store, build_mode) vs (build_mode, store), but both are keyword-only so no call site can break. Noise for anyone reading both APIs, not a hazard.",
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


def test_harness_detects_a_member_present_on_only_one_side() -> None:
    assert "Synthetic.only_here:inproc-only" in _keys()
    assert "Synthetic.added_here:rpc-only" in _keys()


def test_harness_detects_a_renamed_parameter() -> None:
    """`index` vs `idx` is a real divergence in Value.list_get today."""
    assert "Synthetic.renamed_param:params" in _keys()


def test_harness_detects_reordered_parameters() -> None:
    """Value.build reverses (store, build_mode) -- silent breakage for positional callers."""
    assert "Synthetic.reordered:params" in _keys()


def test_harness_detects_a_sync_async_split() -> None:
    assert "Synthetic.sync_on_one_side:async" in _keys()


def test_harness_normalises_a_trailing_timeout_away() -> None:
    """`shared` differs only by rpc's trailing timeout, which is not a divergence."""
    assert not any(key.startswith("Synthetic.shared:") for key in _keys())
