"""TEMPORARY matrix: what error detail survives each boundary, on each engine?

Started as pure scaffolding for CIP3 items 2-3 -- a recording of what both
engines *actually* raise for the same failure, against a local store and
against a daemon store. It still writes that recording to
``NANOPYNIX_ERROR_MATRIX_OUT`` (default: ``.pytest-agent/error-matrix``), but
now also asserts the invariants the recording was used to establish.

Three boundaries, which the pipeline used to conflate:

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

Run with both backends -- plain ``pytest`` is local-only, so the daemon half
silently vanishes and every cell looks identical::

    pytest tests/temp --nix-test-backends local,daemon

Delete this directory once these assertions have a permanent home in
``tests/nanopynix``.
"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import anyio
import pytest
from nanopynix_bindings import expr as nanopynix_expr
from nanopynix_bindings import store as nanopynix_store

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

_OUT_DIR = Path(os.environ.get("NANOPYNIX_ERROR_MATRIX_OUT", ".pytest-agent/error-matrix"))


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
        # Depth of the nix::ErrorInfo trace, so engine parity can be compared
        # on the payload's *content*, not just on "something arrived".
        "trace_count": _count_traces(info),
        "message": str(exc),
    }


def _count_traces(info: object) -> int | None:
    """Depth of an ErrorInfo's trace list, or ``None`` if there is no info."""
    if not isinstance(info, dict):
        return None
    traces: object = cast("dict[str, Any]", info).get("traces")
    return len(cast("list[Any]", traces)) if isinstance(traces, list) else 0


def _trace_count(described: dict[str, Any]) -> int | None:
    count: object = described.get("trace_count")
    return count if isinstance(count, int) else None


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
async def test_record_error_matrix(
    shared_nix_environment: NixTestEnvironment,
    nixpkgs_path: str,
) -> None:
    """Record both engines' errors for this backend, and assert they agree."""
    backend = shared_nix_environment.backend
    matrix = {
        engine: await _collect_engine(shared_nix_environment, engine=engine, nixpkgs=nixpkgs_path)
        for engine in ("rpc", "inproc")
    }

    out_dir = anyio.Path(_OUT_DIR)
    await out_dir.mkdir(parents=True, exist_ok=True)
    await (out_dir / f"{backend}.json").write_text(
        json.dumps(matrix, indent=2, sort_keys=True),
        encoding="utf-8",
    )

    # Also emit to captured stdout so the pytest-agent detail file is readable
    # on its own without going hunting for the JSON.
    for engine, cases in matrix.items():
        for case, described in sorted(cases.items()):
            print(  # noqa: T201 -- diagnostic scaffolding, see module docstring
                f"[{backend}/{engine}/{case}] "
                f"{described['module']}.{described['class']} "
                f"NixError={described.get('is_nanopynix_NixError')} "
                f"error_type={described.get('error_type')!r} "
                f"raw={described.get('raw_populated')} info={described.get('info_populated')}",
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

    # Same failure, same detail -- not just "both non-empty". The trace is what
    # makes the payload worth carrying, so compare its depth across engines.
    trace_mismatches = [
        f"{case}: rpc={_trace_count(matrix['rpc'][case])} inproc={_trace_count(matrix['inproc'][case])}"
        for case in sorted(matrix["rpc"])
        if not case.startswith("build_")
        and not case.endswith("__structured_status")
        and _trace_count(matrix["rpc"][case]) != _trace_count(matrix["inproc"][case])
    ]
    assert not trace_mismatches, f"engines disagree on trace depth ({backend}): {trace_mismatches}"


def test_inproc_raises_the_public_hierarchy_not_raw_bindings() -> None:
    """The raw nanobind classes must not be what reaches inproc callers.

    The bound classes still exist and still have no relationship to the public
    hierarchy -- that part is unchanged, and is exactly why inproc translates
    at its call chokepoint instead of relying on inheritance.
    """
    # Unchanged upstream fact: the bound types are unrelated to NixError.
    assert not issubclass(nanopynix_expr.EvalError, nanopynix.NixError)
    assert not issubclass(nanopynix_store.InvalidPath, nanopynix.NixError)
    assert nanopynix_expr.EvalError is not nanopynix.EvalError

    # ...which is why translation is required, and must be total over the
    # types the bindings actually register.
    for module, names in (
        (nanopynix_expr, ("EvalError", "ParseError", "TypeError", "UndefinedVarError", "ThrownError")),
        (nanopynix_store, ("InvalidPath", "Unsupported", "BadStorePath")),
    ):
        for name in names:
            bound = getattr(module, name)
            translated = translate_nix_exception(bound("boom"))
            assert translated is not None, f"{name} has no public counterpart"
            assert isinstance(translated, nanopynix.NixError)

    assert inproc is not None
