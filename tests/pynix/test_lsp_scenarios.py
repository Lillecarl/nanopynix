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


async def test_hover_inside_a_tfref_string_resolves_the_cross_resource_reference(
    terranix_driver: LspDriver,
) -> None:
    """Marker LSPOINT1 sits on the 'r' of `random_password.example.result` inside `content`'s tfRef string."""
    scenario = Scenario(
        _LOCAL_NIX.as_uri(),
        _LOCAL_NIX.read_text(),
        [
            GoTo("1"),
            ExpectHover(contains="generated random string"),
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
