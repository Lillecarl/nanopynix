"""Marker/scenario-driven LSP tests -- see tests/support/lsp_markers.py and lsp_scenario.py.

Each scenario below runs twice: once against ``InProcessDriver`` (direct
handler calls, no wire protocol) and once against ``WireDriver`` wrapping the
``lsp_wire`` fixture's in-memory ``pytest_lsp.LanguageClient`` <->
``PynixLanguageServer`` pair (real JSON-RPC framing, same event loop). Both
runs share the same ``Scenario``/``Action`` list -- that's the point: one
scenario, two fidelity levels, instead of hand-duplicating each test.

The expectations below are deliberately ones that hold on *both* backends.
Real e2e (a genuine `pynix lsp` subprocess, a real client like Helix)
additionally drives client-side behaviour this repo's server never sees at
all -- automatic completion popups on trigger characters, client-side fuzzy
filtering/sorting of completion items, debouncing -- so an e2e-only scenario
can legitimately expect things these two backends structurally can't (and
shouldn't try to match).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from tests.support.lsp_drivers import InProcessDriver, WireDriver
from tests.support.lsp_environment import asset
from tests.support.lsp_scenario import (
    Delete,
    ExpectCompletion,
    ExpectDiagnostics,
    ExpectHover,
    GoTo,
    InsertAfterCursor,
    Scenario,
    Select,
    Type,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    import pytest_lsp
    from pynix._lsp._handlers import PynixLanguageServer

    from tests.support.lsp_scenario import LspDriver

_LOCAL_NIX = asset("terranix/modules/local.nix")
_NULL_NIX = asset("terranix/modules/null.nix")
_CONFIG_NIX = asset("terranix/modules/config.nix")
_EASYKUBENIX_DEMO_NIX = asset("easykubenix/modules/demo.nix")
_EASYKUBENIX_CONFIG_NIX = asset("easykubenix/modules/config.nix")


@pytest.fixture(params=["in_process", "wire"])
async def terranix_driver(
    request: pytest.FixtureRequest,
    lsp_server: PynixLanguageServer,
    lsp_wire: tuple[PynixLanguageServer, pytest_lsp.LanguageClient],
) -> AsyncIterator[LspDriver]:
    """Both backends for the terranix scenarios below, parametrized so each scenario runs against each."""
    if request.param == "in_process":
        yield InProcessDriver(lsp_server)
        return
    _wire_server, wire_client = lsp_wire
    yield WireDriver(wire_client)


@pytest.fixture(params=["in_process", "wire"])
async def easykubenix_driver(
    request: pytest.FixtureRequest,
    lsp_server: PynixLanguageServer,
    lsp_wire: tuple[PynixLanguageServer, pytest_lsp.LanguageClient],
) -> AsyncIterator[LspDriver]:
    """Both backends for the easykubenix scenarios below, parametrized so each scenario runs against each."""
    if request.param == "in_process":
        yield InProcessDriver(lsp_server)
        return
    _wire_server, wire_client = lsp_wire
    yield WireDriver(wire_client)


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
            ExpectHover(contains="x86_64-linux"),
        ],
    )
    await scenario.run(terranix_driver)


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
            ExpectHover(contains="x86_64-linux"),
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
            ExpectHover(contains="x86_64-linux"),
        ],
    )
    await scenario.run(easykubenix_driver)
