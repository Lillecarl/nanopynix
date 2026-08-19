"""Does the store metadata we return still carry what Nix computed?

``test_store_engine_parity_semantics`` asks whether the two engines *agree*.
That is a different question from whether either is right, and it is blind to
the failure this file exists for: both engines share ``CoreStore``, so a field
Nix fills in and the bindings never read is missing identically on both sides
and reads as perfect agreement.

Three fields were being dropped that way, all of them silently:

* ``Derivation.structured_attrs`` -- Nix's parser moves the ``__json``
  attribute *out of* ``env`` and into ``Derivation::structuredAttrs``, so
  reading only ``env`` returned nothing at all for a
  ``__structuredAttrs = true`` derivation. The assertions below check the
  attributes are absent from ``env``, not just present in the new field:
  presence alone would have passed before the fix too, on a derivation that
  simply did not use structured attrs.
* ``PathInfo.sigs`` -- never read from ``ValidPathInfo::sigs``. Signing a path
  is what makes this non-vacuous; a locally added path has no signatures, so
  asserting ``[]`` would pass against a field that does not exist.
* ``Derivation.input_drvs`` nesting -- ``DerivedPathMap`` is a tree, and the
  old code kept the first output of each child and never recursed.

Each test therefore has to fail against the pre-fix bindings. Where that costs
setup -- a signing key, a dynamic derivation -- the setup is the point.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import anyio
import pytest

from nanopynix.models import DerivationOutputs
from nanopynix_testing.nix_markers import LINUX_CHROOT_BUILD
from test_support.subprocess_output import run_process

if TYPE_CHECKING:
    from pathlib import Path

    from nanopynix_testing.nix_environment import InprocSessionFactory, NixTestEnvironment, RpcSessionFactory

# A derivation that uses structured attrs, with two attributes -- one scalar
# and one nested -- that exist *only* in the __json payload.
STRUCTURED_ATTRS_DRV = """
derivation {
  name = "structured-attrs-fidelity";
  system = builtins.currentSystem;
  builder = "/bin/sh";
  __structuredAttrs = true;
  fidelityList = [ 1 2 3 ];
  fidelityNested = { inner = "value"; };
}
"""

# `builtins.outputOf` is the only way to get a *nested* inputDrvs node: the
# outer derivation depends on an output of a derivation that is itself produced
# by building `inner`, which is exactly the DerivedPathMap child level the old
# flattening could not represent.
DYNAMIC_DERIVATION_DRV = """
let
  inner = derivation {
    name = "inner";
    system = builtins.currentSystem;
    builder = "/bin/sh";
    __contentAddressed = true;
    outputHashMode = "recursive";
    outputHashAlgo = "sha256";
  };
in derivation {
  name = "outer";
  system = builtins.currentSystem;
  builder = "/bin/sh";
  args = [ (builtins.outputOf inner.outPath "out") ];
}
"""


async def _drv_path(session: Any, store: Any, expr: str) -> str:
    async with session.eval(store) as evaluator:
        value = await evaluator.string(expr)
        return await value.attr("drvPath").as_string()


def _check_structured_attrs(derivation: Any) -> None:
    """The payload must be present *and* the attributes absent from ``env``."""
    assert derivation.structured_attrs is not None, "a __structuredAttrs derivation reported no structured_attrs"

    payload = json.loads(derivation.structured_attrs)
    assert payload["fidelityList"] == [1, 2, 3], payload
    assert payload["fidelityNested"] == {"inner": "value"}, payload

    # The half that makes this a fidelity test rather than a smoke test: before
    # the fix these attributes were reported by nothing, because Nix had
    # already removed them from env by the time the bindings saw the drv.
    assert "fidelityList" not in derivation.env, derivation.env
    assert "fidelityNested" not in derivation.env, derivation.env


async def test_inproc_read_derivation_keeps_structured_attrs(inproc_session: InprocSessionFactory) -> None:
    async with inproc_session() as session, session.store() as store:
        drv = await _drv_path(session, store, STRUCTURED_ATTRS_DRV)
        _check_structured_attrs(await store.read_derivation(drv))


async def test_rpc_read_derivation_keeps_structured_attrs(rpc_session: RpcSessionFactory) -> None:
    async with rpc_session() as session, session.store() as store:
        drv = await _drv_path(session, store, STRUCTURED_ATTRS_DRV)
        _check_structured_attrs(await store.read_derivation(drv))


# --- signatures -----------------------------------------------------------


async def _run(*command: str) -> str:
    result = await run_process(command)
    if result.returncode != 0:
        raise RuntimeError(f"{command[0]} {result.describe()}")
    return result.stdout.strip()


@pytest.fixture
async def signed_store_path(
    shared_nix_environment: NixTestEnvironment,
    tmp_path: Path,
) -> tuple[str, str]:
    """A path in the test store carrying one real signature, and the key name.

    Signing is done with the ``nix`` CLI because nanopynix exposes no binding
    for ``Store::addSignatures`` -- deliberately, since nothing needed it. That
    makes the CLI the only way to produce the input this test needs, and the
    test is worth more than the binding would be.
    """
    key_name = "fidelity-test.example"
    secret_key, public_key = tmp_path / "secret.key", tmp_path / "public.key"
    await _run("nix-store", "--generate-binary-cache-key", key_name, str(secret_key), str(public_key))

    source = tmp_path / "signed.txt"
    await anyio.Path(source).write_text("signed content\n")
    store_uri = shared_nix_environment.store_uri
    path = await _run("nix", "store", "add-path", "--store", store_uri, str(source), "--name", "signed.txt")
    await _run("nix", "store", "sign", "--store", store_uri, "--key-file", str(secret_key), path)
    return path, key_name


def _check_signature(info: Any, key_name: str) -> None:
    assert info.sigs, "a signed path reported no signatures"
    assert any(sig.startswith(f"{key_name}:") for sig in info.sigs), info.sigs


async def test_inproc_query_path_info_reports_signatures(
    inproc_session: InprocSessionFactory,
    signed_store_path: tuple[str, str],
) -> None:
    path, key_name = signed_store_path
    async with inproc_session() as session, session.store() as store:
        _check_signature(await store.query_path_info(path), key_name)


async def test_rpc_query_path_info_reports_signatures(
    rpc_session: RpcSessionFactory,
    signed_store_path: tuple[str, str],
) -> None:
    path, key_name = signed_store_path
    async with rpc_session() as session, session.store() as store:
        _check_signature(await store.query_path_info(path), key_name)


# --- input_drvs nesting ---------------------------------------------------


def _check_nested_input_drvs(derivation: Any) -> None:
    """One input must carry a child node, not a flattened output name."""
    children = {path: node.dynamic_outputs for path, node in derivation.input_drvs.items() if node.dynamic_outputs}
    assert children, f"no input carried a dynamic-output child: {derivation.input_drvs!r}"

    for child_map in children.values():
        for child in child_map.values():
            # The type is the assertion. The old wire shape was
            # `map<string, string>`, so this could only ever have been a
            # str -- there was nowhere for a subtree to go.
            assert isinstance(child, DerivationOutputs), f"child is {type(child).__name__}, not a node"
            assert child.outputs, f"child node carries no outputs: {child!r}"


async def test_inproc_read_derivation_keeps_nested_input_drvs(inproc_session: InprocSessionFactory) -> None:
    async with inproc_session() as session, session.store() as store:
        drv = await _drv_path(session, store, DYNAMIC_DERIVATION_DRV)
        _check_nested_input_drvs(await store.read_derivation(drv))


async def test_rpc_read_derivation_keeps_nested_input_drvs(rpc_session: RpcSessionFactory) -> None:
    async with rpc_session() as session, session.store() as store:
        drv = await _drv_path(session, store, DYNAMIC_DERIVATION_DRV)
        _check_nested_input_drvs(await store.read_derivation(drv))


@dataclass(frozen=True)
class _StandInNode:
    """One node of a ``DerivedPathMap``, which a test can build.

    ``nanopynix_bindings.store.DerivationOutputs`` is the real one, and
    nanobind binds it for reading only, so it has no constructor.
    ``_derivation_outputs`` takes the ``_DerivedPathNode`` protocol rather
    than that class, and this is the class that protocol exists for. The two
    fields are the whole of it.
    """

    outputs: list[str]
    dynamic_outputs: dict[str, _StandInNode]


def test_the_input_drvs_builder_recurses_and_keeps_every_output() -> None:
    """The Python half of the recursion, including the case Nix will not produce on demand.

    The two tests above reach a real one-level-deep ``DerivedPathMap`` through
    a dynamic derivation, which is what proves the C++ recurses. They do not
    cover a child with *several* outputs, or a second level -- both are legal
    ``DerivedPathMap`` shapes that are awkward to construct on purpose. The old
    code truncated a multi-output child to ``*child.value.begin()``, so that
    case is worth pinning even from a synthetic node.
    """
    from nanopynix._core._objects import _derivation_outputs  # noqa: PLC0415 -- private, exercised only here

    node = _derivation_outputs(
        _StandInNode(
            outputs=["out"],
            dynamic_outputs={
                "first": _StandInNode(
                    outputs=["a", "b"],
                    dynamic_outputs={"deeper": _StandInNode(outputs=["c"], dynamic_outputs={})},
                ),
            },
        )
    )

    assert node.outputs == ["out"]
    first = node.dynamic_outputs["first"]
    assert first.outputs == ["a", "b"], "a child with two outputs lost one"
    assert first.dynamic_outputs["deeper"].outputs == ["c"], "the second level was not built"


# --- built_outputs --------------------------------------------------------
#
# `BuildResult::Success::builtOutputs` names the store path of each output a
# build produced. `nix build` answers its own output paths from this field and
# from nothing else (`installables.cc`), and nanopynix reported none of it, so
# a caller had to query the store again to find its own build.
#
# The derivation below is deliberately **input-addressed**. An earlier reading
# of Nix said this field is filled only for a content-addressed derivation.
# That is wrong: `checkPathValidity` emplaces every output whose known path is
# valid, and `DerivationGoal` adds the wanted output back when it is missing.
# A content-addressed subject would have hidden that.
#
# Issue #142 records why the neighbouring timing fields stay unexposed: Nix
# loses them in the goal system on 2.34 and 2.35.

BUILT_OUTPUTS_DRV = """
derivation {
  name = "nanopynix-built-outputs-fidelity";
  system = builtins.currentSystem;
  builder = "/bin/sh";
  args = [ "-c" "echo built > $out" ];
}
"""


def _check_built_outputs(results: Any, store_dir: str) -> None:
    """One result, one output, and a real store path for it."""
    assert len(results) == 1, f"expected one result, got {results!r}"
    built = results[0].built_outputs
    assert built, "a successful build reported no built outputs"
    assert sorted(built) == ["out"], f"unexpected output names: {sorted(built)}"

    out_path = built["out"].out_path
    assert out_path.startswith(f"{store_dir}/"), f"{out_path} is not in {store_dir}"
    assert out_path.endswith("-nanopynix-built-outputs-fidelity"), out_path

    # Empty, and present. Nix signs a realisation when it registers one under
    # `ca-derivations`; a plain local build registers none. The assertion is
    # that the field exists and is a list, which is what a signed output would
    # fill in.
    assert built["out"].signatures == [], built["out"].signatures


@LINUX_CHROOT_BUILD
async def test_inproc_build_reports_the_path_it_produced(inproc_session: InprocSessionFactory) -> None:
    async with inproc_session() as session, session.store() as store:
        drv = await _drv_path(session, store, BUILT_OUTPUTS_DRV)
        results = await store.build_paths_with_results([f"{drv}^*"])
        _check_built_outputs(results, (await store.store_dirs()).store_dir)


@LINUX_CHROOT_BUILD
async def test_rpc_build_reports_the_path_it_produced(rpc_session: RpcSessionFactory) -> None:
    async with rpc_session() as session, session.store() as store:
        drv = await _drv_path(session, store, BUILT_OUTPUTS_DRV)
        results = await store.build_paths_with_results([f"{drv}^*"])
        _check_built_outputs(results, (await store.store_dirs()).store_dir)


@LINUX_CHROOT_BUILD
async def test_an_opaque_request_builds_nothing_and_says_so(inproc_session: InprocSessionFactory) -> None:
    """A plain store path is a fetch, not a build, so it produces no output.

    `outputs` is already empty for this case, and `built_outputs` has to agree.
    Without this the empty map would read as "the field is never filled",
    which is what the two tests above would then be proving.
    """
    async with inproc_session() as session, session.store() as store:
        drv = await _drv_path(session, store, BUILT_OUTPUTS_DRV)
        built = await store.build_paths_with_results([f"{drv}^*"])
        fetched = built[0].built_outputs["out"].out_path

        results = await store.build_paths_with_results([fetched])

        assert len(results) == 1
        assert results[0].outputs == [], "an opaque request selects no outputs"
        assert results[0].built_outputs == {}, "an opaque request produced no outputs"
