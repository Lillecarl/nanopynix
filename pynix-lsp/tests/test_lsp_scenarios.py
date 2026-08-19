"""Marker/scenario-driven LSP tests -- see pynix/tests/support/lsp_markers.py and lsp_scenario.py.

Each scenario below runs twice: once against ``InProcessDriver`` (direct
handler calls, no wire protocol) and once against ``WireDriver`` wrapping the
``lsp_wire`` fixture's in-memory ``pytest_lsp.LanguageClient`` <->
``PynixLanguageServer`` pair (real JSON-RPC framing, same event loop). Both
runs share the same ``Scenario``/``Action`` list -- that's the point: one
scenario, two fidelity levels, instead of hand-duplicating each test.

The expectations below are deliberately ones that hold on *both* backends.
Real e2e (a genuine `pynix-lsp` subprocess, a real client like Helix)
additionally drives client-side behaviour this repo's server never sees at
all -- automatic completion popups on trigger characters, client-side fuzzy
filtering/sorting of completion items, debouncing -- so an e2e-only scenario
can legitimately expect things these two backends structurally can't (and
shouldn't try to match).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

import nanopynix
from lsp_support.lsp_drivers import InProcessDriver, WireDriver
from lsp_support.lsp_environment import asset
from lsp_support.lsp_scenario import (
    Delete,
    ExpectCompletion,
    ExpectDefinition,
    ExpectDiagnostics,
    ExpectDocumentHighlightCount,
    ExpectHover,
    ExpectNoCompletion,
    ExpectNoDefinition,
    ExpectNoRename,
    ExpectReferencesCount,
    ExpectText,
    GoTo,
    InsertAfterCursor,
    Rename,
    Scenario,
    Select,
    Type,
)
from nanopynix_testing.nix_markers import LINUX_CHROOT_BUILD

#: What `pkgs.stdenv.hostPlatform.system` renders to on the host that runs
#: these tests.
#:
#: **The value is the proof, and not the subject.** Each hover below reads
#: `_module.args.pkgs` through the module system, and the system string is what
#: comes back when that resolution works. The tests named it `x86_64-linux`,
#: which is one host and not every host, so each of them failed on macOS with
#: `aarch64-darwin` in the hover. Ask Nix, as the other pynix tests do.
_HOST_SYSTEM = nanopynix.current_system()

if TYPE_CHECKING:
    import pytest_lsp

    from lsp_support.lsp_scenario import LspDriver
    from pynix_lsp._handlers import PynixLanguageServer

_LOCAL_NIX = asset("terranix/modules/local.nix")
_NULL_NIX = asset("terranix/modules/null.nix")
_CONFIG_NIX = asset("terranix/modules/config.nix")
_EASYKUBENIX_DEMO_NIX = asset("easykubenix/modules/demo.nix")
_EASYKUBENIX_CONFIG_NIX = asset("easykubenix/modules/config.nix")
_MODULE_SYSTEM_CONFIG1_NIX = asset("module_system/config1.nix")
_DEFINITION_SCOPE_NIX = asset("module_system/definition_scope.nix")
_DEFINITION_IMPORT_NIX = asset("module_system/definition_import.nix")


def _driver_for(request: pytest.FixtureRequest) -> LspDriver:
    """Build only the backend this parametrization actually asked for.

    Naming ``lsp_server`` and ``lsp_wire`` as plain fixture parameters would
    set *both* up for every test -- two ``PynixLanguageServer``s, two RPC
    sessions, two stores -- and immediately discard one. Resolving the chosen
    one lazily halves the per-test fixture cost across all of these scenarios.

    The cost of that laziness is that the discarded fixture also leaves the
    test's declared fixture closure, and ``nix_backend`` is parametrized off
    that closure (``nanopynix_testing.nix_runtime``'s ``pytest_generate_tests``
    keys on ``metafunc.fixturenames``). Each fixture below therefore keeps
    naming ``nix_backend`` for its parametrizing side effect -- see the
    comment there.
    """
    if request.param == "in_process":
        server: PynixLanguageServer = request.getfixturevalue("lsp_server")
        return InProcessDriver(server)
    wire: tuple[PynixLanguageServer, pytest_lsp.LanguageClient] = request.getfixturevalue("lsp_wire")
    _wire_server, wire_client = wire
    return WireDriver(wire_client)


# ``nix_backend`` is named by each fixture below but never read: it has to stay
# in the declared fixture closure so these scenarios keep running once per
# configured backend. Dropping it does not silently halve the matrix -- no
# plain ``nix_backend`` fixture exists, so the run errors out -- but it is the
# reason an apparently unused parameter is here. See ``_driver_for`` above.
@pytest.fixture(params=["in_process", "wire"])
def terranix_driver(request: pytest.FixtureRequest, nix_backend: str) -> LspDriver:
    """Both backends for the terranix scenarios below, parametrized so each scenario runs against each."""
    del nix_backend
    return _driver_for(request)


@pytest.fixture(params=["in_process", "wire"])
def module_system_driver(request: pytest.FixtureRequest, nix_backend: str) -> LspDriver:
    """Both backends for the module-system definition/rename/highlight/references scenarios below."""
    del nix_backend
    return _driver_for(request)


@pytest.fixture(params=["in_process", "wire"])
def easykubenix_driver(
    request: pytest.FixtureRequest,
    nix_backend: str,
    easykubenix_openapi_schema: str,
) -> LspDriver:
    """Both backends for the easykubenix scenarios below, parametrized so each scenario runs against each.

    ``easykubenix_openapi_schema`` is requested for its side effect: these
    scenarios resolve Kinds through a ``fetchurl``-ed OpenAPI document that
    nothing else in the chain ever builds. See its docstring in ``conftest``.
    """
    del easykubenix_openapi_schema, nix_backend
    return _driver_for(request)


@LINUX_CHROOT_BUILD
async def test_hover_inside_a_tfref_string_resolves_the_cross_resource_reference(
    terranix_driver: LspDriver,
) -> None:
    """Marker LSPOINT1 sits on `result`, the last segment of `random_password.example.result`."""
    scenario = Scenario(
        _LOCAL_NIX.as_uri(),
        _LOCAL_NIX.read_text(),
        [
            GoTo("1"),
            ExpectHover(contains="generated random string"),
        ],
    )
    await scenario.run(terranix_driver)


@LINUX_CHROOT_BUILD
async def test_hover_on_the_resource_type_segment_of_a_tfref_string_summarizes_the_type(
    terranix_driver: LspDriver,
) -> None:
    """Marker LSPOINT7 sits on `random_password`, the *type* segment, in the same string as LSPOINT1.

    Same string, different cursor column, different hover -- proving
    `string_literal_path_at` is now position-sensitive rather than always
    resolving to the string's final segment regardless of where the cursor
    actually sits.
    """
    scenario = Scenario(
        _LOCAL_NIX.as_uri(),
        _LOCAL_NIX.read_text(),
        [
            GoTo("7"),
            ExpectHover(contains="`result`"),
        ],
    )
    await scenario.run(terranix_driver)


@LINUX_CHROOT_BUILD
async def test_hover_on_a_resource_type_name_summarizes_its_attributes(
    terranix_driver: LspDriver,
) -> None:
    """Marker LSPOINT6 sits on `local_file`, the type segment, in `content6`'s tfRef string.

    Regression coverage for the original gap: hovering the resource type
    name itself (not a specific attribute) previously returned None outright
    (`_schema_path_at` rejected anything shorter than 4 segments) -- this now
    renders the block's own JSON Schema conversion, listing its declared
    attributes.
    """
    scenario = Scenario(
        _LOCAL_NIX.as_uri(),
        _LOCAL_NIX.read_text(),
        [
            GoTo("6"),
            ExpectHover(contains="`filename`"),
        ],
    )
    await scenario.run(terranix_driver)


@LINUX_CHROOT_BUILD
async def test_select_delete_and_retype_inside_a_tfref_string_updates_completion(
    terranix_driver: LspDriver,
) -> None:
    """Select 'result' (marker 2), delete it, type 'len', and expect `length` in the completion list.

    Exercises a real incremental edit sequence (select -> delete -> type),
    not a static fixture snapshot with an artificial cursor offset -- the
    same shape of bug class the existing progressive-completion test in
    test_lsp.py catches, but expressed as a reusable marker-driven scenario.
    """
    scenario = Scenario(
        _LOCAL_NIX.as_uri(),
        _LOCAL_NIX.read_text(),
        [
            Select("2"),
            Delete(),
            Type("len"),
            ExpectCompletion(labels=frozenset({"length"})),
        ],
    )
    await scenario.run(terranix_driver)


@LINUX_CHROOT_BUILD
async def test_clearing_and_retyping_a_whole_line_still_completes(
    terranix_driver: LspDriver,
) -> None:
    """Marker LSLINE3 spans the whole `output.greeting_path...` line -- clear it, retype from scratch.

    Uses ``InsertAfterCursor`` to close the string's quote without moving the
    cursor, mirroring a real editor's bracket/quote auto-pairing (Helix
    inserts the matching ``"`` the moment you type the opening one, cursor
    left in between). An unterminated string is a separate, harder problem --
    tree-sitter's error recovery can't produce a proper string node for it,
    since there's no closing quote to bound where the string ends -- and is
    out of scope here.
    """
    scenario = Scenario(
        _LOCAL_NIX.as_uri(),
        _LOCAL_NIX.read_text(),
        [
            Select("3"),
            Delete(),
            Type('  output.new_output.value = lib.tfRef "'),
            InsertAfterCursor('"'),
            Type("random_id.suffix.he"),
            ExpectCompletion(labels=frozenset({"hex"})),
        ],
    )
    await scenario.run(terranix_driver)


async def test_bare_top_level_prefix_completes_block_type_keywords(
    terranix_driver: LspDriver,
) -> None:
    """Marker LSLINE3, cleared and retyped with a bare `res` -- expects `resource` from the core schema.

    Regression coverage for a real gap found via manual testing: typing a
    block-type keyword from scratch at the top level (no `.` yet, so this
    goes through the plain identifier-lexical-scan completion path, not the
    schema-attribute one) previously fell straight through to None, since
    only 3+-segment attribute paths were handled at all.
    """
    scenario = Scenario(
        _LOCAL_NIX.as_uri(),
        _LOCAL_NIX.read_text(),
        [
            Select("3"),
            Delete(),
            Type("res"),
            ExpectCompletion(labels=frozenset({"resource"})),
        ],
    )
    await scenario.run(terranix_driver)


@LINUX_CHROOT_BUILD
async def test_resource_type_completion_lists_the_full_provider_catalog(
    terranix_driver: LspDriver,
) -> None:
    """Marker LSLINE3, cleared and retyped with `resource.rand` -- expects every locked `random_*` type.

    Regression coverage for a real gap found via manual testing: typing a
    resource type name previously only completed against types *already
    used* in the file (the generic root-value fallback in ``_handlers.py``,
    reading a real but narrow Nix value) since TerranixDialect itself only
    ever handled 3+-segment attribute paths. This exercises the wider,
    schema-sourced catalog now returned for the 1-segment ("resource.<type>")
    case -- ``random_password``/``random_string`` aren't used anywhere in
    ``local.nix``, so this only passes if the provider schema (not just
    already-configured values) is actually being consulted.
    """
    scenario = Scenario(
        _LOCAL_NIX.as_uri(),
        _LOCAL_NIX.read_text(),
        [
            Select("3"),
            Delete(),
            Type("resource.rand"),
            ExpectCompletion(labels=frozenset({"random_id", "random_password", "random_string"})),
        ],
    )
    await scenario.run(terranix_driver)


@LINUX_CHROOT_BUILD
async def test_hover_on_a_core_meta_argument_resolves_via_the_tofu_core_schema(
    terranix_driver: LspDriver,
) -> None:
    """Marker LSPOINT4 sits on `count` in `lib.tfRef "local_file.greeting.count"`.

    `count` isn't in any provider's schema -- it's one of OpenTofu's own
    meta-arguments, understood by every resource/data/module block
    regardless of provider (see tools/tofu-core-schema, which exports
    opentofu-schema's built-in block schema). This only passes if
    TerranixDialect actually merges that core schema in alongside the
    provider schema (`byte_length` etc., already covered by other tests).
    """
    scenario = Scenario(
        _LOCAL_NIX.as_uri(),
        _LOCAL_NIX.read_text(),
        [
            GoTo("4"),
            ExpectHover(contains="number of instances"),
        ],
    )
    await scenario.run(terranix_driver)


@LINUX_CHROOT_BUILD
async def test_completion_after_a_partial_core_meta_argument_name(
    terranix_driver: LspDriver,
) -> None:
    """Marker LSPOINT5 sits right after `c` in `lib.tfRef "local_file.greeting.c"` -- expects `count`."""
    scenario = Scenario(
        _LOCAL_NIX.as_uri(),
        _LOCAL_NIX.read_text(),
        [
            GoTo("5"),
            ExpectCompletion(labels=frozenset({"count"})),
        ],
    )
    await scenario.run(terranix_driver)


@LINUX_CHROOT_BUILD
async def test_hover_on_a_resource_instance_name_also_summarizes_its_type(
    terranix_driver: LspDriver,
) -> None:
    """Marker LSPOINT8 sits on `greeting`, the *instance* name (not a tfRef string, a real binding key).

    A 3-segment path (`resource.local_file.greeting`) hits the same
    resource-type-summary branch as the 2-segment type-name case above --
    there's only one schema block per *type*, not per instance, so this is
    intentionally the same rendering, not a separate instance-specific one.
    """
    scenario = Scenario(
        _LOCAL_NIX.as_uri(),
        _LOCAL_NIX.read_text(),
        [
            GoTo("8"),
            ExpectHover(contains="`filename`"),
        ],
    )
    await scenario.run(terranix_driver)


async def test_hover_on_a_module_arg_resolves_via_terranixs_own_module_system(
    terranix_driver: LspDriver,
) -> None:
    """Marker LSPOINT9 (in null.nix, not local.nix) sits on `system` in `pkgs.stdenv.hostPlatform.system`.

    `null.nix` declares `{ lib, pkgs, ... }:` -- `pkgs` isn't a terranix
    concept at all, it only resolves because `../default.nix`'s
    `moduleSystem` (a real, un-sanitized `lib.evalModules` result, unlike
    terranix's own `core/default.nix` wrapper which discards `_module`
    entirely) sets `_module.args.pkgs`, and `TerranixDialect.derive_roots`
    binds it as a `moduleEntry` root so `ModuleSystemDialect`'s existing
    `_module.args` resolution (the same mechanism real NixOS modules use for
    `pkgs`) picks it up for free.
    """
    scenario = Scenario(
        _NULL_NIX.as_uri(),
        _NULL_NIX.read_text(),
        [
            GoTo("9"),
            ExpectHover(contains=_HOST_SYSTEM),
        ],
    )
    await scenario.run(terranix_driver)


@LINUX_CHROOT_BUILD
async def test_unknown_attribute_reports_tf001_unless_suppressed_on_its_own_line(terranix_driver: LspDriver) -> None:
    """Neither `content2` nor `content5` (`local_file.greeting`) are real attributes.

    `content5` has a trailing `# noqa: TF001 -- ...` comment and must NOT be
    reported; `content2` has no such comment on its own line and must still
    fire -- proving suppression is scoped to the specific line it's written
    on, not "any noqa comment anywhere in the document silences every
    diagnostic of that code."
    """
    scenario = Scenario(
        _LOCAL_NIX.as_uri(),
        _LOCAL_NIX.read_text(),
        [
            ExpectDiagnostics("TF001", contains="content2"),
            ExpectDiagnostics("TF001", contains="content5", absent=True),
        ],
    )
    await scenario.run(terranix_driver)


@LINUX_CHROOT_BUILD
async def test_resource_type_completion_still_works_inside_an_explicit_config_wrapper(
    terranix_driver: LspDriver,
) -> None:
    """config.nix wraps its definitions in `config = { ... };` -- the NixOS-module convention, see config.nix's own comment.

    Regression coverage for exactly the manual check that motivated this
    fixture: resource *type* completion (mirroring
    ``test_resource_type_completion_lists_the_full_provider_catalog``) must
    resolve identically whether or not the module wraps its definitions in
    `config`, since `TerranixDialect`'s roots come from the already-merged
    `.config` and `completion_target_at` only ever looks at the innermost
    enclosing binding's own attrpath, never an outer `config` key.
    """
    scenario = Scenario(
        _CONFIG_NIX.as_uri(),
        _CONFIG_NIX.read_text(),
        [
            Select("1"),
            Delete(),
            Type("resource.rand"),
            ExpectCompletion(labels=frozenset({"random_id", "random_password", "random_string"})),
        ],
    )
    await scenario.run(terranix_driver)


async def test_hover_on_a_module_arg_resolves_for_easykubenix_files(easykubenix_driver: LspDriver) -> None:
    """Marker LSPOINT1 sits on `system` in `pkgs.stdenv.hostPlatform.system`.

    Unlike terranix, easykubenix needs no un-hiding workaround at all --
    ``../easykubenix/default.nix`` goes through easykubenix's own real entry
    point, whose ``passthru.eval`` already exposes the raw ``lib.evalModules``
    result. This proves the plain ``ModuleSystemDialect`` (no new
    easykubenix-specific Python code) resolves `_module.args` for an
    easykubenix file's own top-level lambda formals exactly like it already
    does for terranix's `null.nix` and any ordinary NixOS module.
    """
    scenario = Scenario(
        _EASYKUBENIX_DEMO_NIX.as_uri(),
        _EASYKUBENIX_DEMO_NIX.read_text(),
        [
            GoTo("1"),
            ExpectHover(contains=_HOST_SYSTEM),
        ],
    )
    await scenario.run(easykubenix_driver)


async def test_hover_on_a_real_easykubenix_option_description(easykubenix_driver: LspDriver) -> None:
    """Marker LSPOINT2 sits on `objects`, the second segment of a flat top-level binding.

    Deliberately shallow: nixpkgs' module system does not expose per-instance
    `options.<path>` recursion for `attrsOf submodule`-typed options via
    plain attribute access at all (confirmed with an isolated `evalModules`
    repro, independent of easykubenix's own design -- the real mechanism is
    `type.getSubOptions`, used internally by documentation tooling, not a
    live per-instance tree). `options.kubernetes.objects` itself is the
    deepest path this mechanism can resolve, and it carries a real,
    substantial `mkOption` description -- proving options-tree hover works
    for an easykubenix module with zero new Python code.
    """
    scenario = Scenario(
        _EASYKUBENIX_DEMO_NIX.as_uri(),
        _EASYKUBENIX_DEMO_NIX.read_text(),
        [
            GoTo("2"),
            ExpectHover(contains="grouped by namespace"),
        ],
    )
    await scenario.run(easykubenix_driver)


async def test_hover_on_a_module_arg_still_works_inside_an_explicit_config_wrapper(
    easykubenix_driver: LspDriver,
) -> None:
    """config.nix wraps its definitions in `config = { ... };` -- see config.nix's own comment.

    Regression coverage mirroring
    ``test_resource_type_completion_still_works_inside_an_explicit_config_wrapper``
    for terranix: `_module.args` resolution (marker LSPOINT1, same
    `pkgs.stdenv.hostPlatform.system` proof as demo.nix's LSPOINT1) must
    resolve identically whether or not the module wraps its definitions in
    `config`, since `identifier_path_at` only ever looks at the innermost
    enclosing binding's own attrpath, never an outer `config` key.
    """
    scenario = Scenario(
        _EASYKUBENIX_CONFIG_NIX.as_uri(),
        _EASYKUBENIX_CONFIG_NIX.read_text(),
        [
            GoTo("1"),
            ExpectHover(contains=_HOST_SYSTEM),
        ],
    )
    await scenario.run(easykubenix_driver)


async def test_completion_right_after_the_top_level_kubernetes_dot_lists_real_options(
    easykubenix_driver: LspDriver,
) -> None:
    """Marker LSPOINTTOP sits right after `kubernetes.`, before `objects` -- an empty-partial trigger-on-dot position.

    Not EasykubenixDialect's own doing (it defers for a 1-segment prefix) --
    falls back to the generic options-tree completion ModuleSystemDialect
    already provides. Included so every dot boundary along this path has an
    explicit test, not just the ones the newer dialect handles.
    """
    scenario = Scenario(
        _EASYKUBENIX_CONFIG_NIX.as_uri(),
        _EASYKUBENIX_CONFIG_NIX.read_text(),
        [
            GoTo("TOP"),
            ExpectCompletion(labels=frozenset({"objects", "resources", "apiMappings"})),
        ],
    )
    await scenario.run(easykubenix_driver)


async def test_completion_right_after_the_objects_dot_lists_declared_namespace_names(
    easykubenix_driver: LspDriver,
) -> None:
    """Marker LSPOINTNS sits right after `kubernetes.objects.`, before `default` -- an empty-partial trigger-on-dot position.

    ``EasykubenixDialect._namespace_names`` sources this from the real,
    already-declared `Namespace` objects in the `none` bucket
    (`kubernetes.objects.none.Namespace.default` is declared earlier in
    this same file) rather than every namespace bucket key already used for
    some other Kind.
    """
    scenario = Scenario(
        _EASYKUBENIX_CONFIG_NIX.as_uri(),
        _EASYKUBENIX_CONFIG_NIX.read_text(),
        [
            GoTo("NS"),
            ExpectCompletion(labels=frozenset({"default"}), exact=True),
        ],
    )
    await scenario.run(easykubenix_driver)


async def test_completion_right_after_the_namespace_dot_lists_known_kind_names(
    easykubenix_driver: LspDriver,
) -> None:
    """Marker LSPOINTKIND sits right after `kubernetes.objects.default.`, before `Deployment`.

    ``EasykubenixDialect._kind_names`` sources this from
    `config.kubernetes.apiMappings`'s own keys -- every Kind this project
    knows an apiVersion for (bundled `apiResources/v1.33.json` plus any
    project-declared extras), a strictly richer source than the OpenAPI
    schema alone since a CRD Kind can appear here with no corresponding
    upstream schema definition.
    """
    scenario = Scenario(
        _EASYKUBENIX_CONFIG_NIX.as_uri(),
        _EASYKUBENIX_CONFIG_NIX.read_text(),
        [
            GoTo("KIND"),
            ExpectCompletion(labels=frozenset({"Deployment", "DaemonSet", "ConfigMap"})),
        ],
    )
    await scenario.run(easykubenix_driver)


async def test_completion_right_after_the_kind_dot_does_not_crash_with_no_name_source(
    easykubenix_driver: LspDriver,
) -> None:
    """Marker LSPOINTNAME sits right after `kubernetes.objects.default.Deployment.`, before `p4`.

    The object's own instance name is an arbitrary new name being chosen --
    there's no sensible existing source to suggest from, so
    ``EasykubenixDialect.complete`` deliberately returns None here (deferring
    down the dialect chain), and every other dialect/fallback also has
    nothing to offer at this position -- pinning that down explicitly
    (rather than leaving it untested) confirms this doesn't crash or hang
    now that four different dialects/fallbacks all get a chance at it.
    """
    scenario = Scenario(
        _EASYKUBENIX_CONFIG_NIX.as_uri(),
        _EASYKUBENIX_CONFIG_NIX.read_text(),
        [
            GoTo("NAME"),
            ExpectNoCompletion(),
        ],
    )
    await scenario.run(easykubenix_driver)


async def test_completion_right_after_the_instance_name_dot_lists_top_level_object_fields(
    easykubenix_driver: LspDriver,
) -> None:
    """Marker LSPOINTF1 sits right after `...Deployment.p5.`, before `spec`.

    The shallowest schema-backed case: no `$ref` hop needed at all, just the
    Deployment definition's own top-level `properties`.
    """
    scenario = Scenario(
        _EASYKUBENIX_CONFIG_NIX.as_uri(),
        _EASYKUBENIX_CONFIG_NIX.read_text(),
        [
            GoTo("F1"),
            ExpectCompletion(labels=frozenset({"apiVersion", "kind", "metadata", "spec", "status"})),
        ],
    )
    await scenario.run(easykubenix_driver)


async def test_completion_right_after_the_spec_dot_lists_deployment_spec_fields(
    easykubenix_driver: LspDriver,
) -> None:
    """Marker LSPOINTF2 sits right after `...p6.spec.`, before `template` -- one `$ref` hop (Deployment -> DeploymentSpec)."""
    scenario = Scenario(
        _EASYKUBENIX_CONFIG_NIX.as_uri(),
        _EASYKUBENIX_CONFIG_NIX.read_text(),
        [
            GoTo("F2"),
            ExpectCompletion(labels=frozenset({"replicas", "selector", "template", "strategy"})),
        ],
    )
    await scenario.run(easykubenix_driver)


async def test_completion_right_after_the_template_dot_lists_pod_template_spec_fields(
    easykubenix_driver: LspDriver,
) -> None:
    """Marker LSPOINTF3 sits right after `...p7.spec.template.`, before `spec` -- two `$ref` hops deep (-> PodTemplateSpec)."""
    scenario = Scenario(
        _EASYKUBENIX_CONFIG_NIX.as_uri(),
        _EASYKUBENIX_CONFIG_NIX.read_text(),
        [
            GoTo("F3"),
            ExpectCompletion(labels=frozenset({"metadata", "spec"}), exact=True),
        ],
    )
    await scenario.run(easykubenix_driver)


async def test_completion_right_after_the_second_spec_dot_lists_pod_spec_fields(
    easykubenix_driver: LspDriver,
) -> None:
    """Marker LSPOINTF4 sits right after `...p8.spec.template.spec.`, before `containers` -- three `$ref` hops deep (-> PodSpec).

    Proves arbitrary-depth `$ref` resolution genuinely works end-to-end
    through the real LSP handler, not just the standalone
    ``pynix_lsp._jsonschema``/``_easykubenix_schema`` functions in isolation.
    """
    scenario = Scenario(
        _EASYKUBENIX_CONFIG_NIX.as_uri(),
        _EASYKUBENIX_CONFIG_NIX.read_text(),
        [
            GoTo("F4"),
            ExpectCompletion(labels=frozenset({"containers", "volumes", "restartPolicy"})),
        ],
    )
    await scenario.run(easykubenix_driver)


async def test_hover_on_a_kind_name_summarizes_it_via_the_openapi_schema(easykubenix_driver: LspDriver) -> None:
    """Marker LSPOINT3 sits on `Deployment` in `kubernetes.objects.default.Deployment.demo.spec.replicas`.

    Unlike LSPOINT2's shallow ``options.kubernetes.objects`` case (a fixed,
    schema-independent mkOption description shared by every Kind),
    ``EasykubenixDialect`` resolves the *specific* Kind's own OpenAPI
    definition -- via ``config.kubernetes.apiMappings.Deployment`` (="apps/v1")
    and the pinned schema at ``../default.nix``'s ``openApiSchemaPath`` -- and
    renders that definition's top-level description.
    """
    scenario = Scenario(
        _EASYKUBENIX_DEMO_NIX.as_uri(),
        _EASYKUBENIX_DEMO_NIX.read_text(),
        [
            GoTo("3"),
            ExpectHover(contains="declarative updates"),
        ],
    )
    await scenario.run(easykubenix_driver)


async def test_hover_on_a_field_inside_a_kubernetes_object_body_resolves_via_the_openapi_schema(
    easykubenix_driver: LspDriver,
) -> None:
    """Marker LSPOINT4 sits on `replicas` in `...Deployment.demo.spec.replicas = 1;`.

    The real per-field case this dialect exists for: `kubernetes.nix`'s
    object bodies are `freeformType = ekn.lib.kubeValueType` (a generic
    recursive JSON-ish type), so Nix itself has zero structural knowledge of
    what `spec.replicas` means -- that only exists in the Kubernetes OpenAPI
    schema, resolved here by walking `spec` (a `$ref` to `DeploymentSpec`)
    then `replicas` through `pynix_lsp._jsonschema.walk`.
    """
    scenario = Scenario(
        _EASYKUBENIX_DEMO_NIX.as_uri(),
        _EASYKUBENIX_DEMO_NIX.read_text(),
        [
            GoTo("4"),
            ExpectHover(contains="Number of desired pods"),
        ],
    )
    await scenario.run(easykubenix_driver)


async def test_completion_inside_a_kubernetes_object_body_lists_openapi_schema_fields(
    easykubenix_driver: LspDriver,
) -> None:
    """Marker range LSSTART5/LSEND5 spans `replicas` on a second Deployment, retyped as `rep`.

    Proves ``EasykubenixDialect.complete`` (not just ``hover``) walks the
    OpenAPI schema -- ``list_properties`` at ``("spec",)`` for `Deployment`
    must include `replicas` (and its DeploymentSpec siblings) as real,
    schema-sourced completion items.
    """
    scenario = Scenario(
        _EASYKUBENIX_DEMO_NIX.as_uri(),
        _EASYKUBENIX_DEMO_NIX.read_text(),
        [
            Select("5"),
            Delete(),
            Type("rep"),
            ExpectCompletion(labels=frozenset({"replicas"})),
        ],
    )
    await scenario.run(easykubenix_driver)


async def test_hover_on_a_field_written_in_nested_attrset_style_still_resolves_via_the_openapi_schema(
    easykubenix_driver: LspDriver,
) -> None:
    """Marker LSPOINT6 sits on `replicas` inside `Deployment.demoNested = { spec = { replicas = 2; }; };`.

    Regression coverage for a real gap found via manual testing:
    `identifier_path_at` alone only ever resolves the *innermost* binding's
    own flat attrpath (`["replicas"]` here), losing the outer
    `kubernetes.objects.default.Deployment.demoNested.spec` prefix entirely
    since it never walks up through the wrapping `spec = { ... };`/`Deployment.
    demoNested = { ... };` bindings. `EasykubenixDialect` recovers this via
    `enclosing_binding_path_at` (see pynix-lsp/src/pynix_lsp/_syntax.py).
    """
    scenario = Scenario(
        _EASYKUBENIX_DEMO_NIX.as_uri(),
        _EASYKUBENIX_DEMO_NIX.read_text(),
        [
            GoTo("6"),
            ExpectHover(contains="Number of desired pods"),
        ],
    )
    await scenario.run(easykubenix_driver)


async def test_completion_on_a_bare_key_in_nested_attrset_style_recovers_the_kind_context(
    easykubenix_driver: LspDriver,
) -> None:
    """Marker range LSSTART7/LSEND7 spans `metadata` on a bare key inside `Deployment.demoNested2 = { ... };`.

    The exact bug reported via manual testing: completing a bare (dot-less)
    key typed directly inside a Kubernetes object's nested-style body (e.g.
    retyping `metadata` from scratch as `me`) returned nothing at all --
    `completion_target_at`'s own lexical scan sees only the empty local
    prefix `[]` for a bare identifier, with zero awareness of the enclosing
    `kubernetes.objects.default.Deployment.demoNested2` context.
    `EasykubenixDialect.complete` now prepends `enclosing_binding_path_at`'s
    result before checking the Kind-anchored shape.
    """
    scenario = Scenario(
        _EASYKUBENIX_DEMO_NIX.as_uri(),
        _EASYKUBENIX_DEMO_NIX.read_text(),
        [
            Select("7"),
            Delete(),
            Type("me"),
            ExpectCompletion(labels=frozenset({"metadata"})),
        ],
    )
    await scenario.run(easykubenix_driver)


async def test_renaming_a_let_binding_updates_every_reference_but_not_a_shadowed_inner_one(
    module_system_driver: LspDriver,
) -> None:
    """Marker GDEF sits on the outer `greeting`'s own name; GREF sits on one of its references.

    `definition_scope.nix` also declares an inner, shadowing `let greeting =
    "shadowed"; in greeting` -- renaming the outer binding must leave that
    inner one completely untouched, proving the shadowing-aware scope walk
    in `local_scope_at` (not just a naive whole-file text replace).
    """
    scenario = Scenario(
        _DEFINITION_SCOPE_NIX.as_uri(),
        _DEFINITION_SCOPE_NIX.read_text(),
        [
            GoTo("GDEF"),
            Rename("salutation"),
            ExpectText(contains='salutation = "hello"'),
            ExpectText(contains="first = salutation +"),
            ExpectText(contains="second = salutation +"),
            ExpectText(contains="outer = salutation;"),
            ExpectText(contains='greeting = "shadowed"'),
            ExpectText(contains="in\n    greeting;"),
        ],
    )
    await scenario.run(module_system_driver)


async def test_renaming_a_formal_updates_a_sibling_defaults_reference_too(
    module_system_driver: LspDriver,
) -> None:
    """Marker ADEF sits on formal `a` in `{ a, b ? a + 1 }:` -- its scope is the whole function, not just the body.

    Regression coverage for the exact scoping bug found and fixed while
    building this feature: `b`'s default expression (`a + 1`) is a sibling
    formal's default, not part of the function body, but is still in `a`'s
    scope per Nix's own formals semantics -- see `_binding_site_at` in
    `_syntax.py`.
    """
    scenario = Scenario(
        _DEFINITION_SCOPE_NIX.as_uri(),
        _DEFINITION_SCOPE_NIX.read_text(),
        [
            GoTo("ADEF"),
            Rename("x"),
            ExpectText(contains="{ x, b ? x + 1 }:"),
            ExpectText(contains="x + b;"),
        ],
    )
    await scenario.run(module_system_driver)


async def test_document_highlight_and_references_count_every_span_in_the_shadowing_aware_scope(
    module_system_driver: LspDriver,
) -> None:
    """Marker GREF sits on a reference (not the definition) -- highlight/references must still find the whole scope.

    The outer `greeting` has 1 definition + 3 references (`first`, `second`,
    `outer`) -- 4 spans for document highlight (always includes the
    definition), 4 for references with `include_declaration`, 3 without.
    """
    scenario = Scenario(
        _DEFINITION_SCOPE_NIX.as_uri(),
        _DEFINITION_SCOPE_NIX.read_text(),
        [
            GoTo("GREF"),
            ExpectDocumentHighlightCount(4),
            ExpectReferencesCount(4, include_declaration=True),
            ExpectReferencesCount(3, include_declaration=False),
        ],
    )
    await scenario.run(module_system_driver)


async def test_prepare_rename_refuses_on_a_nixos_option_reference(module_system_driver: LspDriver) -> None:
    """Marker ENABLE (in config1.nix) sits on `enable` in a bare option-definition attrpath, not a local binding.

    v1 rename is deliberately local-lexical-scope only -- a NixOS option
    reference resolves through real Nix evaluation, not syntax, so it must
    not be offered for rename at all.
    """
    scenario = Scenario(
        _MODULE_SYSTEM_CONFIG1_NIX.as_uri(),
        _MODULE_SYSTEM_CONFIG1_NIX.read_text(),
        [
            GoTo("ENABLE"),
            ExpectNoRename(),
        ],
    )
    await scenario.run(module_system_driver)


async def test_go_to_definition_on_an_import_path_literal_jumps_to_the_imported_file(
    module_system_driver: LspDriver,
) -> None:
    """Marker IMPORT (in definition_import.nix) sits inside the `./mod2.nix` path literal of an `imports = [ ... ];` entry."""
    scenario = Scenario(
        _DEFINITION_IMPORT_NIX.as_uri(),
        _DEFINITION_IMPORT_NIX.read_text(),
        [
            GoTo("IMPORT"),
            ExpectDefinition(line=0, character=0, file_suffix="mod2.nix"),
        ],
    )
    await scenario.run(module_system_driver)


async def test_go_to_definition_on_a_nixos_option_reference_lands_on_its_declaration(
    module_system_driver: LspDriver,
) -> None:
    """Marker ENABLE (in config1.nix) sits on `enable`, a bare option-definition attrpath resolving through `options`.

    `mod3.nix` declares `services.example-daemon.enable = lib.mkEnableOption
    ...;` on its own line 4 (1-based), column 5 -- nixpkgs' `lib/options.nix`
    `declarationPositions` bookkeeping, read via `_option_declaration_locations`
    -- so this only passes if the whole real-evaluation path (not just the
    syntax-only local-scope engine) is actually being consulted for
    `textDocument/definition`.
    """
    scenario = Scenario(
        _MODULE_SYSTEM_CONFIG1_NIX.as_uri(),
        _MODULE_SYSTEM_CONFIG1_NIX.read_text(),
        [
            GoTo("ENABLE"),
            ExpectDefinition(line=3, character=4, file_suffix="mod3.nix"),
        ],
    )
    await scenario.run(module_system_driver)


async def test_go_to_definition_on_a_composed_package_attribute_gracefully_finds_nothing(
    module_system_driver: LspDriver,
) -> None:
    """Marker PKGSHELLO (in config1.nix) sits on `hello` in `pkgs.hello`.

    `builtins.unsafeGetAttrPos` returns null for nixpkgs packages composed
    via `makeScope`/overlays (confirmed during this feature's design pass) --
    a graceful `None` is the correct, tested outcome here, not a bug.
    """
    scenario = Scenario(
        _MODULE_SYSTEM_CONFIG1_NIX.as_uri(),
        _MODULE_SYSTEM_CONFIG1_NIX.read_text(),
        [
            GoTo("PKGSHELLO"),
            ExpectNoDefinition(),
        ],
    )
    await scenario.run(module_system_driver)
