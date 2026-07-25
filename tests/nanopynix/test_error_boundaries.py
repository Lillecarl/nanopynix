"""What error detail survives each boundary, on each engine and each backend?

Three boundaries, which the error pipeline used to conflate:

* **A** Nix C++ -> Python via nanobind. Ours. The bound exception types name
  the C++ class, and ``nix_error_info.hh`` attaches the ``nix::ErrorInfo``
  (position, trace, suggestions) that ``e.what()`` cannot carry.
* **B** nanopynix worker -> client via gRPC. Ours. The type name rides the
  status message; the ``ErrorInfo`` rides the ``grpc-status-details-bin``
  trailer. Both must be wired on both ends -- see
  :mod:`nanopynix.rpc._status_details`.
* **C** Nix daemon -> client via the daemon protocol. **Not ours.** Upstream
  downgrades ``HashMismatch`` to ``OutputRejected`` on the wire
  (``common-protocol.cc:153-158``) and formats FOD hashes into prose only.
  This is why ``build_fod_hash_mismatch`` is the one cell that legitimately
  differs between the local and daemon backends.

This began as a temporary recording of what both engines *actually* raise,
used to find CIP3's error-pipeline defects; it is kept because three of its
invariants have no other home. :mod:`tests.nanopynix.test_engine_parity_semantics`
covers "both engines raise the same type" for eval failures in a far more
readable form -- what only this file covers is:

1. the same invariant across *store* and *build* failures, and across both
   backends, where the daemon protocol is a third boundary;
2. every Nix failure being catchable as ``nanopynix.NixError``;
3. the ``nix::ErrorInfo`` surviving A and B intact, compared field by field
   between the engines rather than merely "both non-empty".

(3) is the one that cannot be dropped. Boundary B fails **silently**: grpclib
omits the status-details trailer if either end lacks the codec, with no error
on either side, so a missed wiring looks exactly like success everywhere else.

Run with both backends -- plain ``pytest`` is local-only, so the daemon half
silently vanishes and every cell looks identical::

    pytest tests/nanopynix/test_error_boundaries.py --nix-test-backends local,daemon
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any, cast

import pytest
from nanopynix_bindings import errors as nanopynix_errors

import nanopynix
from nanopynix import inproc
from nanopynix.exceptions import translate_nix_exception
from tests.support.nix_environment import with_nixpkgs

if TYPE_CHECKING:
    from tests.support.nix_environment import NixTestEnvironment

# A syntactically valid store path (Nix base32 omits e/o/u/t) that is not, and
# will never be, present -- so `query_path_info` fails on lookup rather than on
# parsing, which is a different error entirely.
NONEXISTENT_PATH = "/nix/store/00000000000000000000000000000000-nanopynix-absent"

# Sentinel for "key absent", distinct from a key present with value None --
# a dropped `pos` and a `pos: None` are different findings.
_MISSING = object()


def _describe(exc: BaseException) -> dict[str, Any]:
    """Everything a caller could plausibly branch on, for one raised exception."""
    info = getattr(exc, "info", None)
    return {
        "class": type(exc).__name__,
        "module": type(exc).__module__,
        # The MRO is the real answer to "can callers catch this uniformly?"
        "mro": [f"{base.__module__}.{base.__name__}" for base in type(exc).__mro__ if base is not object],
        "is_nanopynix_NixError": isinstance(exc, nanopynix.NixError),
        "is_RuntimeError": isinstance(exc, RuntimeError),
        # These three are declared and documented on NixError but the RPC path
        # never populates them; recording them is the whole point of item 2.
        "error_type": getattr(exc, "error_type", None),
        "raw_populated": bool(getattr(exc, "raw", "")),
        "info_populated": info is not None,
        # The whole nix::ErrorInfo, so engine parity can be compared on the
        # payload's *content* rather than on "something arrived". A dropped
        # `pos`, a lost suggestion, or a mangled hint is invisible to a
        # populated/not-populated bool and to a trace *count*.
        "info": info,
        "message": str(exc),
    }


def _info_of(described: dict[str, Any]) -> dict[str, Any] | None:
    info: object = described.get("info")
    return cast("dict[str, Any]", info) if isinstance(info, dict) else None


def _info_diff(case: str, rpc: dict[str, Any] | None, inproc_: dict[str, Any] | None) -> str:
    """Which ErrorInfo keys differ between the engines, and how."""
    if rpc is None or inproc_ is None:
        return f"{case}: rpc info={rpc is not None} inproc info={inproc_ is not None}"
    differing = sorted(
        key for key in set(rpc) | set(inproc_) if rpc.get(key, _MISSING) != inproc_.get(key, _MISSING)
    )
    return f"{case}: " + "; ".join(
        f"{key}: rpc={rpc.get(key, _MISSING)!r} inproc={inproc_.get(key, _MISSING)!r}" for key in differing
    )


def _fod_expr(nixpkgs: str) -> str:
    """A fixed-output derivation whose declared hash cannot match its output.

    ``outputHash = ""`` is the same fixture ``tests/pynix/test_build.py`` uses
    to drive ``--update-fod``; the build always reports a hash mismatch.
    """
    return with_nixpkgs(
        f"""with import <nixpkgs> {{}};
runCommand "nanopynix-fod-{uuid.uuid4().hex[:8]}" {{
  outputHash = "";
  outputHashAlgo = "sha256";
  outputHashMode = "flat";
}} "printf '%s\\\\n' payload > $out"
""",
        nixpkgs,
    )


def _failing_build_expr(nixpkgs: str) -> str:
    """A plain (non-FOD) build failure -- the builder exits non-zero."""
    return with_nixpkgs(
        f"""with import <nixpkgs> {{}};
runCommand "nanopynix-buildfail-{uuid.uuid4().hex[:8]}" {{}} "exit 1"
""",
        nixpkgs,
    )


# Eval-only cases. These fail inside the evaluator, in-process, so the store
# backend is irrelevant to them -- they discriminate *engines*, not backends.
_EVAL_CASES: dict[str, str] = {
    "eval_undefined_variable": "nanopynix_no_such_variable",
    "eval_type_error": '1 + "not a number"',
    "eval_throw": 'builtins.throw "nanopynix matrix throw"',
    "eval_parse_error": "let in in",
    "eval_missing_attr": "{ a = 1; }.nonexistent",
    "eval_infinite_recursion": "let x = x; in x",
}


async def _capture(operation: Any) -> dict[str, Any]:
    """Await *operation*, returning a description of however it failed."""
    try:
        await operation
    except Exception as exc:
        return _describe(exc)
    return {"class": None, "message": "DID NOT RAISE"}


async def _collect_engine(
    environment: NixTestEnvironment,
    *,
    engine: str,
    nixpkgs: str,
) -> dict[str, dict[str, Any]]:
    """Run every failure case on one engine, returning case -> description."""
    results: dict[str, dict[str, Any]] = {}
    # `Any` is not laziness -- it is the finding. There is no shared static type
    # that both engines' Session/Store satisfy, so engine-agnostic code like
    # this cannot be typed: pyright rejects `nix.eval(store)` because
    # inproc.Store and rpc.Store are unrelated nominal types. A real parity
    # guarantee (CIP3 items 4/24) is what would make this annotation honest.
    session_factory: Any = environment.rpc_session if engine == "rpc" else environment.inproc_session

    async with session_factory() as nix, nix.store() as store:
        results["store_invalid_path"] = await _capture(store.query_path_info(NONEXISTENT_PATH))

        evaluator = nix.eval(store)
        async with evaluator as eval_session:
            for name, expr in _EVAL_CASES.items():
                results[name] = await _capture(eval_session.string(expr))

            for name, expr in (
                ("build_plain_failure", _failing_build_expr(nixpkgs)),
                ("build_fod_hash_mismatch", _fod_expr(nixpkgs)),
            ):
                value = await eval_session.string(expr)
                # Two views of the same failure. `value.build()` is the raising
                # API callers actually use; `build_paths_with_results` returns
                # Nix's structured BuildResult -- whose `status` field carries
                # the failure *kind* ("hash-mismatch", "output-rejected", ...)
                # that the raising path currently throws away entirely.
                derived_path = await _drv_path(eval_session, expr)
                results[name] = await _capture(value.build())
                results[f"{name}__structured_status"] = await _build_status(store, derived_path)

    return results


async def _drv_path(eval_session: Any, expr: str) -> str:
    """Evaluate *expr*'s ``.drvPath`` as a plain, context-free string.

    Done as its own expression rather than by navigating the value, because
    every route through the value API hits an engine asymmetry (all CIP3
    findings): rpc's ValueProxy has no derived-path accessor at all while
    inproc's Value has ``get_derived_path``; ``attr()`` is sync on rpc and
    async on inproc (item 9); and the string accessor is ``coerce_str`` on rpc
    but ``as_string`` on inproc (item 8). ``unsafeDiscardStringContext`` is
    needed because ``coerce_str`` refuses a string carrying store-path context.
    """
    value = await eval_session.string(f"builtins.unsafeDiscardStringContext (({expr}).drvPath)")
    to_string = getattr(value, "as_string", None) or value.coerce_str
    return await to_string()


async def _build_status(store: Any, derived_path: str) -> dict[str, Any]:
    """Record Nix's own structured BuildResult for a failing derived path."""
    try:
        built = await store.build_paths_with_results([derived_path])
    except Exception as exc:
        return {"class": type(exc).__name__, "module": type(exc).__module__, "message": str(exc)}
    return {
        "class": None,
        "module": None,
        "message": "",
        "results": [
            {"success": result.success, "status": result.status, "error_msg": result.error_msg}
            for result in built
        ],
    }


@pytest.mark.anyio
async def test_error_detail_survives_every_boundary(
    shared_nix_environment: NixTestEnvironment,
    nixpkgs_path: str,
    agent_notes: Any,
) -> None:
    """Both engines, one backend: same exception type, same ErrorInfo."""
    backend = shared_nix_environment.backend
    matrix = {
        engine: await _collect_engine(shared_nix_environment, engine=engine, nixpkgs=nixpkgs_path)
        for engine in ("rpc", "inproc")
    }

    # Every cell is recorded as a pytest-agent note rather than written to a
    # side file: a note lands in the run summary, in notes.jsonl, and in this
    # test's own log, so the recording is readable in the same turn that
    # produced it without hunting for JSON.
    for engine, cases in matrix.items():
        for case, described in sorted(cases.items()):
            agent_notes.note(
                **{
                    f"{backend}/{engine}/{case}": (
                        f"{described['module']}.{described['class']} "
                        f"NixError={described.get('is_nanopynix_NixError')} "
                        f"error_type={described.get('error_type')!r} "
                        f"raw={described.get('raw_populated')} info={described.get('info_populated')}"
                    )
                }
            )

    # ── The invariant this whole file exists to defend ──────────────
    #
    # Same failure, same backend, both engines -> same exception type and same
    # error_type. Process isolation is the only thing rpc has that inproc does
    # not, and none of these failures are about process isolation, so there is
    # no legitimate reason for the two to disagree.
    mismatches = [
        f"{case}: rpc={matrix['rpc'][case]['class']} inproc={matrix['inproc'][case]['class']}"
        for case in sorted(matrix["rpc"])
        if not case.endswith("__structured_status")
        and (
            matrix["rpc"][case]["class"] != matrix["inproc"][case]["class"]
            or matrix["rpc"][case]["error_type"] != matrix["inproc"][case]["error_type"]
        )
    ]
    assert not mismatches, f"engines disagree on {backend}: " + "; ".join(mismatches)

    # Every Nix failure must be catchable as nanopynix.NixError, on both
    # engines. This is what `except nanopynix.NixError` silently missed before.
    not_nix_errors = [
        f"{engine}/{case}"
        for engine, cases in matrix.items()
        for case, described in cases.items()
        if not case.endswith("__structured_status") and not described["is_nanopynix_NixError"]
    ]
    assert not not_nix_errors, f"not NixError subclasses: {not_nix_errors}"

    # ── Structured detail (nix::ErrorInfo), boundary A ──────────────
    #
    # Anything raised by the C++ evaluator or store carries a `nix::ErrorInfo`
    # -- position, evaluation trace, suggestions -- which C++ is the only place
    # to have. Both engines now propagate it -- inproc via nix_error_info.hh's
    # raw/info attributes, rpc via those same attributes forwarded through the
    # grpc-status-details-bin trailer -- so assert it rather than record it.
    #
    # Deliberately NOT asserted for the two `build_*` cases: those are built by
    # `build_error_from_result` out of Nix's BuildResult{status, error_msg,
    # drv_path}, which has no ErrorInfo anywhere in it on either engine or
    # either backend. Empty raw/info there is the honest answer, not a gap.
    # rpc is asserted alongside inproc, not merely recorded, because boundary
    # B's failure mode is *silent*: grpclib omits the status-details trailer if
    # either end lacks the codec, with no error on either side. A missed wiring
    # would look exactly like success everywhere except here.
    missing_detail = [
        f"{engine}/{case} (raw={described['raw_populated']} info={described['info_populated']})"
        for engine, cases in matrix.items()
        for case, described in cases.items()
        if not case.startswith("build_")
        and not case.endswith("__structured_status")
        and not (described["raw_populated"] and described["info_populated"])
    ]
    assert not missing_detail, f"lost nix::ErrorInfo for: {missing_detail}"

    # Same failure, same detail -- not just "both non-empty". Compare the whole
    # ErrorInfo, because a dropped `pos`, a lost suggestion, or a mangled hint
    # is invisible to a populated/not-populated bool. rpc's copy has been
    # through the wire encoding and inproc's has not, so this is also the only
    # place that would catch the encoding itself losing a field.
    info_mismatches = [
        _info_diff(case, _info_of(matrix["rpc"][case]), _info_of(matrix["inproc"][case]))
        for case in sorted(matrix["rpc"])
        if not case.startswith("build_")
        and not case.endswith("__structured_status")
        and _info_of(matrix["rpc"][case]) != _info_of(matrix["inproc"][case])
    ]
    assert not info_mismatches, f"engines disagree on ErrorInfo ({backend}): {info_mismatches}"


def test_inproc_raises_the_public_hierarchy_not_raw_bindings() -> None:
    """The raw nanobind classes must not be what reaches inproc callers.

    The bound classes still exist and still have no relationship to the public
    hierarchy -- that part is unchanged, and is exactly why inproc translates
    at its call chokepoint instead of relying on inheritance.
    """
    # Unchanged upstream fact: the bound types are unrelated to NixError.
    assert not issubclass(nanopynix_errors.EvalError, nanopynix.NixError)
    assert not issubclass(nanopynix_errors.InvalidPath, nanopynix.NixError)
    assert nanopynix_errors.EvalError is not nanopynix.EvalError

    # ...which is why translation is required, and must be total over the
    # types the bindings actually register. They all live in one module now:
    # a single translator owns the nix::Error hierarchy, so the classes it
    # dispatches to had to stop being scattered across expr/store/util.
    for name in (
        "Error",
        "EvalBaseError",
        "EvalError",
        "ParseError",
        "TypeError",
        "UndefinedVarError",
        "AssertionError",
        "ThrownError",
        "InvalidPath",
        "Unsupported",
        "BadStorePath",
        "SysError",
        "UsageError",
        "UnimplementedError",
    ):
        bound = getattr(nanopynix_errors, name)
        translated = translate_nix_exception(bound("boom"))
        assert translated is not None, f"{name} has no public counterpart"
        assert isinstance(translated, nanopynix.NixError)

    assert inproc is not None
