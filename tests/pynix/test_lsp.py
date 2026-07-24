from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from lsprotocol import types
from pynix._lsp._context import parse_directives
from pynix._lsp._handlers import _completion, _document_symbols, _hover, _sync_document
from pynix._lsp._syntax import (
    completion_target_at,
    identifier_path_at,
    parse_errors,
    string_literal_path_at,
    top_level_lambda_formals,
    top_level_symbols,
)

from tests.support.lsp_cursor import cursor_after
from tests.support.lsp_environment import asset

if TYPE_CHECKING:
    from pynix._lsp._handlers import PynixLanguageServer


# ── parse_directives ─────────────────────────────────────────────────


def test_parse_directives_missing_returns_empty() -> None:
    assert parse_directives("{ }\n") == []


def test_parse_directives_finds_a_well_formed_header() -> None:
    directives = parse_directives("# pynix-lsp: cfg = config.services.foo\n{ }\n")
    assert len(directives) == 1
    assert directives[0].name == "cfg"
    assert directives[0].expr == "config.services.foo"


def test_parse_directives_finds_multiple_headers() -> None:
    source = "# pynix-lsp: config = 1\n# pynix-lsp: options = 2\n{ }\n"
    directives = parse_directives(source)
    assert [(d.name, d.expr) for d in directives] == [("config", "1"), ("options", "2")]


def test_parse_directives_allows_a_license_header_first() -> None:
    source = "# SPDX-License-Identifier: MIT\n\n# pynix-lsp: cfg = 1\n{ }\n"
    directives = parse_directives(source)
    assert len(directives) == 1
    assert directives[0].name == "cfg"


def test_parse_directives_records_the_directives_true_source_line() -> None:
    """The blank line between the license header and the directive must not shift `.line`."""
    source = "# SPDX-License-Identifier: MIT\n\n# pynix-lsp: cfg = 1\n{ }\n"
    directives = parse_directives(source)
    assert directives[0].line == 2


def test_parse_directives_ignores_a_directive_past_the_scan_window() -> None:
    source = "\n".join(["# filler"] * 10 + ["# pynix-lsp: cfg = 1", "{ }"])
    assert parse_directives(source) == []


def test_parse_directives_rejects_a_malformed_line() -> None:
    assert parse_directives("# pynix-lsp: not a valid directive\n") == []


# ── tree-sitter syntax helpers ───────────────────────────────────────


def test_identifier_path_at_resolves_a_multi_segment_attrpath() -> None:
    source = "cfg.services.foo.enable"
    offset = source.index("services")
    assert identifier_path_at(source, offset) == ["cfg", "services"]


def test_identifier_path_at_resolves_the_final_segment() -> None:
    source = "cfg.services.foo.enable"
    offset = source.index("enable")
    assert identifier_path_at(source, offset) == ["cfg", "services", "foo", "enable"]


def test_identifier_path_at_resolves_a_bare_variable() -> None:
    source = "cfg"
    assert identifier_path_at(source, 1) == ["cfg"]


def test_identifier_path_at_returns_none_off_an_identifier() -> None:
    source = "1 + 2"
    assert identifier_path_at(source, 2) is None


def test_identifier_path_at_resolves_a_binding_attrpath_key() -> None:
    """A definition key like `services.foo = true;` has no base variable to anchor on."""
    source = "{ services.foo = true; }"
    offset = source.index("foo")
    assert identifier_path_at(source, offset) == ["services", "foo"]


def test_string_literal_path_at_truncates_to_the_segment_under_the_cursor() -> None:
    """Cursor on the first segment returns just that segment, not the whole dotted chain."""
    source = '"random_id.suffix.hex"'
    offset = source.index("random_id")
    assert string_literal_path_at(source, offset) == ["random_id"]


def test_string_literal_path_at_truncates_through_a_middle_segment() -> None:
    source = '"random_id.suffix.hex"'
    offset = source.index("suffix")
    assert string_literal_path_at(source, offset) == ["random_id", "suffix"]


def test_string_literal_path_at_on_the_final_segment_returns_the_whole_chain() -> None:
    source = '"random_id.suffix.hex"'
    offset = source.index("hex")
    assert string_literal_path_at(source, offset) == ["random_id", "suffix", "hex"]


def test_completion_target_at_splits_prefix_and_partial() -> None:
    source = "cfg.services.serv"
    assert completion_target_at(source, len(source)) == (["cfg", "services"], "serv")


def test_completion_target_at_handles_a_trailing_dot() -> None:
    source = "cfg.services."
    assert completion_target_at(source, len(source)) == (["cfg", "services"], "")


def test_completion_target_at_returns_none_with_no_identifier_before_cursor() -> None:
    source = "1 + "
    assert completion_target_at(source, len(source)) is None


def test_parse_errors_flags_broken_syntax() -> None:
    assert parse_errors(asset("syntax_error.nix").read_text()) != []


def test_parse_errors_is_empty_for_valid_syntax() -> None:
    assert parse_errors("{ a = 1; b = 2; }") == []


def test_top_level_symbols_lists_bindings() -> None:
    """Walks every binding, not just the outermost attrset's -- see top_level_symbols' docstring."""
    paths = [symbol.path for symbol in top_level_symbols(asset("module_outline.nix").read_text())]
    assert paths == ["options.services.foo.enable", "config", "systemd.services.foo"]


def test_top_level_lambda_formals_lists_declared_names() -> None:
    source = "{ config, pkgs, lib, ... }: { }"
    assert top_level_lambda_formals(source) == ["config", "pkgs", "lib"]


def test_top_level_lambda_formals_returns_none_for_a_plain_attrset() -> None:
    assert top_level_lambda_formals("{ a = 1; }") is None


def test_top_level_lambda_formals_returns_none_for_a_single_identifier_lambda() -> None:
    assert top_level_lambda_formals("x: x") is None


# ── cursor_after ─────────────────────────────────────────────────────


def test_cursor_after_finds_the_default_offset_of_one() -> None:
    assert cursor_after("cfg.serv", "serv") == types.Position(line=0, character=5)


def test_cursor_after_finds_the_targets_own_line_without_a_needle() -> None:
    """No `needle` means `target` must be searched for directly, not defaulted to line 0."""
    assert cursor_after("# header\ncfg.\n", "cfg.", offset=4) == types.Position(line=1, character=4)


def test_cursor_after_respects_a_custom_offset() -> None:
    assert cursor_after("cfg.services.serv", "services", offset=3) == types.Position(line=0, character=7)


def test_cursor_after_narrows_the_search_with_a_needle() -> None:
    source = "port = 1;\nservices.example-daemon.port = 2;\n"
    assert cursor_after(source, "port", needle="example-daemon") == types.Position(line=1, character=25)


# ── in-process integration tests (real evaluator, no subprocess) ─────
#
# These drive the real handler functions directly (not a stdio subprocess,
# unlike test_lsp_e2e.py) against nix files checked into test_lsp/, so header
# directives and relative imports behave exactly as they would for a real
# user's file on disk. `lsp_server` comes from tests/support/lsp_environment.py.


async def test_hover_resolves_a_real_evaluated_value(lsp_server: PynixLanguageServer) -> None:
    uri = asset("hover_target.nix").as_uri()
    await _sync_document(lsp_server, uri)

    source = asset("hover_target.nix").read_text()
    position = cursor_after(source, "value", needle="cfg.nested", offset=0)
    hover = await _hover(lsp_server, types.HoverParams(types.TextDocumentIdentifier(uri), position))

    assert hover is not None
    assert isinstance(hover.contents, types.MarkupContent)
    assert "42" in hover.contents.value
    assert "int" in hover.contents.value


async def test_completion_lists_real_attribute_names(lsp_server: PynixLanguageServer) -> None:
    uri = asset("completion_target.nix").as_uri()
    await _sync_document(lsp_server, uri)

    source = asset("completion_target.nix").read_text()
    position = cursor_after(source, "cfg.", offset=len("cfg."))
    completion = await _completion(lsp_server, types.CompletionParams(types.TextDocumentIdentifier(uri), position))

    assert completion is not None
    labels = {item.label for item in completion.items}
    assert labels == {"enable", "extraConfig"}


async def test_completion_resolves_options_through_a_full_module_system_eval(
    lsp_server: PynixLanguageServer,
) -> None:
    """config.<path> completion against a real lib.evalModules merge across mod1.nix + mod2.nix.

    mod1.nix declares `options.programs.example.enable`; mod2.nix's own body
    references `config.programs.example.enable` (a realistic self-referential
    NixOS module pattern). mod2.nix's `moduleEntry` directive derives a
    `config` root from the whole merged `(import ./default.nix).config`, so
    completing mid-identifier there must see the option mod1.nix declared,
    not just mod2.nix's own text.
    """
    uri = asset("module_system/mod2.nix").as_uri()
    await _sync_document(lsp_server, uri)

    source = asset("module_system/mod2.nix").read_text()
    position = cursor_after(
        source,
        "example",
        needle="config.programs.example.enable",
        offset=len("exa"),
    )
    completion = await _completion(lsp_server, types.CompletionParams(types.TextDocumentIdentifier(uri), position))

    assert completion is not None
    labels = {item.label for item in completion.items}
    assert labels == {"example"}


@pytest.mark.parametrize("fixture_name", ["config1.nix", "config2.nix"])
async def test_bare_attrpath_completion_falls_back_to_the_options_tree(
    lsp_server: PynixLanguageServer,
    fixture_name: str,
) -> None:
    """A definition key like `services.example-daemon.enable = true;` has no `config.`/`options.` prefix at all.

    config1.nix wraps its definitions in an explicit `config = { ... };`;
    config2.nix uses the implicit/flat NixOS module style (no `config` key at
    all -- the whole returned attrset IS config). Both must resolve the same
    way: when the typed path doesn't start with any bound name, fall back to
    the file's `options` root and walk the whole path directly.
    """
    uri = asset(f"module_system/{fixture_name}").as_uri()
    await _sync_document(lsp_server, uri)

    source = asset(f"module_system/{fixture_name}").read_text()
    position = cursor_after(
        source,
        "services.example-daemon",
        needle="services.example-daemon.enable",
        offset=len("services.exam"),
    )
    completion = await _completion(lsp_server, types.CompletionParams(types.TextDocumentIdentifier(uri), position))

    assert completion is not None
    labels = {item.label for item in completion.items}
    assert labels == {"example-daemon"}


@pytest.mark.parametrize("fixture_name", ["config1.nix", "config2.nix"])
async def test_pkgs_completion_lists_real_nixpkgs_attributes(
    lsp_server: PynixLanguageServer,
    fixture_name: str,
) -> None:
    """`pkgs.hel` -> real nixpkgs completion, not a guess.

    Both fixtures declare `pkgs` as a top-level lambda formal, so it's
    inferred from `moduleEntry`'s `_module.specialArgs.pkgs` (the same
    evalModules call as `config`/`options`) -- the exact nixpkgs instance
    (overlays and all) this module's own `pkgs` argument would actually be
    at eval time.
    """
    uri = asset(f"module_system/{fixture_name}").as_uri()
    await _sync_document(lsp_server, uri)

    source = asset(f"module_system/{fixture_name}").read_text()
    position = cursor_after(
        source,
        "pkgs.hello",
        needle="programs.example.package = pkgs.hello",
        offset=len("pkgs.hel"),
    )
    completion = await _completion(lsp_server, types.CompletionParams(types.TextDocumentIdentifier(uri), position))

    assert completion is not None
    labels = {item.label for item in completion.items}
    assert "hello" in labels


async def _hover_value(lsp_server: PynixLanguageServer, uri: str, source: str, needle: str, name: str) -> str:
    position = cursor_after(source, name, needle=needle)
    hover = await _hover(lsp_server, types.HoverParams(types.TextDocumentIdentifier(uri), position))
    assert hover is not None
    assert isinstance(hover.contents, types.MarkupContent)
    return hover.contents.value


async def test_module_arg_prefers_specialargs_over_module_args(lsp_server: PynixLanguageServer) -> None:
    """`testArg` is set in both `_module.args` and `_module.specialArgs` with different values.

    nixpkgs' `lib/modules.nix` `applyModuleArgs` resolves each formal as
    `args.${name} or config._module.args.${name}`, where `args` is built
    from `specialArgs` -- so on a name present in both, specialArgs must win.
    """
    uri = asset("module_system/precedence.nix").as_uri()
    await _sync_document(lsp_server, uri)
    source = asset("module_system/precedence.nix").read_text()
    value = await _hover_value(lsp_server, uri, source, "_unused", "testArg")
    assert "fromSpecialArgs" in value


async def test_module_arg_falls_back_to_module_args_when_absent_from_specialargs(
    lsp_server: PynixLanguageServer,
) -> None:
    """`onlyInArgs` exists only in `_module.args`, so it must still resolve via the fallback tier."""
    uri = asset("module_system/precedence.nix").as_uri()
    await _sync_document(lsp_server, uri)
    source = asset("module_system/precedence.nix").read_text()
    value = await _hover_value(lsp_server, uri, source, "_unused", "onlyInArgs")
    assert "onlyArgsValue" in value


async def test_module_arg_completion_is_gated_on_the_files_own_declared_formals(
    lsp_server: PynixLanguageServer,
) -> None:
    """`notDeclared` exists in `_module.specialArgs` but isn't one of this file's own lambda formals.

    Resolving it must fail -- offering it anyway would suggest a name that
    isn't actually in scope in this file, since real Nix would reject it as
    an undefined variable too.
    """
    uri = asset("module_system/precedence.nix").as_uri()
    await _sync_document(lsp_server, uri)
    source = asset("module_system/precedence.nix").read_text()
    position = cursor_after(source, "notDeclared", needle="_unused")
    hover = await _hover(lsp_server, types.HoverParams(types.TextDocumentIdentifier(uri), position))
    assert hover is None


async def test_progressive_completion_after_typing_a_trailing_dot(lsp_server: PynixLanguageServer) -> None:
    """Drives real didChange-shaped in-memory edits, not a static fixture read.

    A completion bug can depend on the exact in-progress text (e.g. working
    right after `programs` but breaking the instant `.` is appended) in a
    way a single fixture snapshot with an artificial cursor offset can't
    catch -- reading the file from disk always sees a single frozen state.
    This seeds the workspace via `put_text_document` and mutates it via
    `update_text_document`, exactly like real `textDocument/didChange`
    notifications would, to check completion after each keystroke.
    """
    uri = asset("module_system/config2.nix").as_uri()
    base_source = asset("module_system/config2.nix").read_text()
    lsp_server.workspace.put_text_document(
        types.TextDocumentItem(uri=uri, language_id="nix", version=1, text=base_source),
    )
    await _sync_document(lsp_server, uri)

    lines = base_source.splitlines()
    line_index = next(i for i, line in enumerate(lines) if "services.example-daemon.enable" in line)

    for version, (typed, expected_labels) in enumerate(
        [("programs", {"programs"}), ("programs.", {"example"})],
        start=2,
    ):
        edited_lines = list(lines)
        edited_lines[line_index] = f"  {typed}"
        edited_source = "\n".join(edited_lines) + "\n"
        lsp_server.workspace.update_text_document(
            types.VersionedTextDocumentIdentifier(uri=uri, version=version),
            types.TextDocumentContentChangeWholeDocument(text=edited_source),
        )
        await _sync_document(lsp_server, uri)

        position = types.Position(line=line_index, character=len(f"  {typed}"))
        completion = await _completion(lsp_server, types.CompletionParams(types.TextDocumentIdentifier(uri), position))

        assert completion is not None, f"no completion after typing {typed!r}"
        labels = {item.label for item in completion.items}
        assert labels == expected_labels, f"typing {typed!r} got {labels}, expected {expected_labels}"


async def test_hover_on_a_bare_definition_key_shows_the_declared_option(lsp_server: PynixLanguageServer) -> None:
    """Hovering the `port` in `services.example-daemon.port = 9090;` (config1.nix) shows its option doc."""
    uri = asset("module_system/config1.nix").as_uri()
    await _sync_document(lsp_server, uri)

    source = asset("module_system/config1.nix").read_text()
    position = cursor_after(source, "port", needle="services.example-daemon.port")
    hover = await _hover(lsp_server, types.HoverParams(types.TextDocumentIdentifier(uri), position))

    assert hover is not None
    assert isinstance(hover.contents, types.MarkupContent)
    assert "Port the example daemon listens on." in hover.contents.value


async def test_sync_document_reports_a_diagnostic_when_context_eval_fails(lsp_server: PynixLanguageServer) -> None:
    uri = asset("broken_context.nix").as_uri()
    published: list[types.PublishDiagnosticsParams] = []
    lsp_server.text_document_publish_diagnostics = published.append  # type: ignore[method-assign] -- swap the real RPC notification for a test recorder

    await _sync_document(lsp_server, uri)

    assert published
    messages = [d.message for d in published[-1].diagnostics]
    assert any("boom" in message for message in messages)


async def test_sync_document_reports_a_context_diagnostic_at_the_directives_own_line(
    lsp_server: PynixLanguageServer,
) -> None:
    """The directive isn't on line 0 here, so a stale `(0,0)`-always diagnostic would miss it."""
    uri = asset("broken_context_second_line.nix").as_uri()
    published: list[types.PublishDiagnosticsParams] = []
    lsp_server.text_document_publish_diagnostics = published.append  # type: ignore[method-assign] -- swap the real RPC notification for a test recorder

    await _sync_document(lsp_server, uri)

    assert published
    diagnostic = next(d for d in published[-1].diagnostics if "boom" in d.message)
    assert diagnostic.range.start.line == 1


async def test_sync_document_reports_a_diagnostic_for_broken_syntax(lsp_server: PynixLanguageServer) -> None:
    uri = asset("syntax_error.nix").as_uri()
    published: list[types.PublishDiagnosticsParams] = []
    lsp_server.text_document_publish_diagnostics = published.append  # type: ignore[method-assign] -- swap the real RPC notification for a test recorder

    await _sync_document(lsp_server, uri)

    assert published
    assert published[-1].diagnostics != []


async def test_context_expression_resolves_relative_to_the_files_own_directory(
    lsp_server: PynixLanguageServer,
) -> None:
    uri = asset("relative_import.nix").as_uri()
    await _sync_document(lsp_server, uri)

    context = lsp_server.contexts[uri]
    assert context.errors == {}
    assert context.roots["cfg"] is not None


async def test_document_symbol_lists_bindings_via_the_real_handler(lsp_server: PynixLanguageServer) -> None:
    uri = asset("module_outline.nix").as_uri()
    symbols = _document_symbols(lsp_server, uri)
    names = [symbol.name for symbol in symbols]
    assert names == ["options.services.foo.enable", "config", "systemd.services.foo"]


# ── last-good-tree fallback (tree-sitter-nix's unbounded error recovery) ──


async def test_document_symbols_fall_back_to_the_last_error_free_parse(lsp_server: PynixLanguageServer) -> None:
    """Regression test: an incomplete ``formals`` list doesn't stay contained.

    tree-sitter-nix's error recovery keeps trying to extend the
    comma-separated formal list across everything that follows, shredding a
    real, already-complete binding arbitrarily far downstream in the same
    file into unrecognizable fragments -- not just the token actually being
    typed. Falling back to the last snapshot that parsed with zero
    ERROR/MISSING nodes means an in-progress edit near the top of a file
    doesn't blank out the outline for unrelated, already-complete bindings
    further down.
    """
    uri = "file:///last-good-tree-outline-test.nix"
    good_source = (
        "{\n"
        "  resource.local_file.greeting = {\n"
        '    content = "hello";\n'
        "  };\n"
        "  resource.local_file.third = {\n"
        '    content = "world";\n'
        "  };\n"
        "}\n"
    )
    lsp_server.workspace.put_text_document(
        types.TextDocumentItem(uri=uri, language_id="nix", version=1, text=good_source),
    )
    await _sync_document(lsp_server, uri)
    good_names = {symbol.name for symbol in _document_symbols(lsp_server, uri)}
    assert "resource.local_file.third" in good_names

    broken_source = '{\n  f = { a, b\n  resource.local_file.third = {\n    content = "world";\n  };\n}\n'
    lsp_server.workspace.update_text_document(
        types.VersionedTextDocumentIdentifier(uri=uri, version=2),
        types.TextDocumentContentChangeWholeDocument(text=broken_source),
    )
    await _sync_document(lsp_server, uri)
    assert lsp_server.has_parse_errors[uri]

    names_while_broken = {symbol.name for symbol in _document_symbols(lsp_server, uri)}
    assert "resource.local_file.third" in names_while_broken


async def test_hover_falls_back_to_the_last_error_free_parse(lsp_server: PynixLanguageServer) -> None:
    """Same corruption as above, but for hover -- and on a byte-identical
    cursor position, since the corrupting edit replaces one line in place
    rather than inserting new lines, so the target line's position is the
    same in both the good and broken snapshots."""
    uri = asset("module_system/last_good_tree.nix").as_uri()
    good_source = asset("module_system/last_good_tree.nix").read_text()
    lsp_server.workspace.put_text_document(
        types.TextDocumentItem(uri=uri, language_id="nix", version=1, text=good_source),
    )
    await _sync_document(lsp_server, uri)
    assert not lsp_server.has_parse_errors[uri]

    broken_source = good_source.replace(
        "services.example-daemon.enable = true;",
        "broken = { a, b",
    )
    assert broken_source != good_source
    lsp_server.workspace.update_text_document(
        types.VersionedTextDocumentIdentifier(uri=uri, version=2),
        types.TextDocumentContentChangeWholeDocument(text=broken_source),
    )
    await _sync_document(lsp_server, uri)
    assert lsp_server.has_parse_errors[uri]

    position = cursor_after(broken_source, "port", needle="services.example-daemon.port")
    hover = await _hover(lsp_server, types.HoverParams(types.TextDocumentIdentifier(uri), position))

    assert hover is not None
    assert isinstance(hover.contents, types.MarkupContent)
    assert "Port the example daemon listens on." in hover.contents.value


# ── TerranixDialect (real tofu providers schema -json, via the fixture's own wrapper) ──


async def test_hover_shows_provider_schema_description_for_a_resource_attribute(
    lsp_server: PynixLanguageServer,
) -> None:
    """Hovering `byte_length` in `resource.random_id.suffix.byte_length = 4;` shows the real provider schema."""
    uri = asset("terranix/modules/random.nix").as_uri()
    await _sync_document(lsp_server, uri)

    source = asset("terranix/modules/random.nix").read_text()
    position = cursor_after(source, "byte_length", needle="random_id.suffix.byte_length")
    hover = await _hover(lsp_server, types.HoverParams(types.TextDocumentIdentifier(uri), position))

    assert hover is not None
    assert isinstance(hover.contents, types.MarkupContent)
    assert "number of random bytes to produce" in hover.contents.value
    assert "4" in hover.contents.value


async def test_completion_lists_real_schema_attribute_names_for_a_resource_type(
    lsp_server: PynixLanguageServer,
) -> None:
    """Completing after `resource.random_id.suffix.` lists the real schema's attribute names."""
    uri = asset("terranix/modules/random.nix").as_uri()
    await _sync_document(lsp_server, uri)

    source = asset("terranix/modules/random.nix").read_text()
    position = cursor_after(source, "byte_length", needle="random_id.suffix.byte_length", offset=0)
    completion = await _completion(lsp_server, types.CompletionParams(types.TextDocumentIdentifier(uri), position))

    assert completion is not None
    labels = {item.label for item in completion.items}
    assert {"byte_length", "prefix", "keepers", "hex", "b64_url", "b64_std", "dec", "id"} <= labels


async def test_hover_resolves_a_cross_resource_reference_inside_a_tfref_string(
    lsp_server: PynixLanguageServer,
) -> None:
    """Hovering inside `lib.tfRef "random_id.suffix.hex"` resolves against `random_id`'s real schema."""
    uri = asset("terranix/modules/null.nix").as_uri()
    await _sync_document(lsp_server, uri)

    source = asset("terranix/modules/null.nix").read_text()
    position = cursor_after(source, "hex", needle="random_id.suffix.hex")
    hover = await _hover(lsp_server, types.HoverParams(types.TextDocumentIdentifier(uri), position))

    assert hover is not None
    assert isinstance(hover.contents, types.MarkupContent)
    assert "padded hexadecimal digits" in hover.contents.value


async def test_completion_inside_a_tfref_string_lists_matching_schema_attribute_names(
    lsp_server: PynixLanguageServer,
) -> None:
    """Completing mid-token inside `lib.tfRef "random_id.suffix.he"` lists matching schema attribute names."""
    uri = asset("terranix/modules/null.nix").as_uri()
    await _sync_document(lsp_server, uri)

    source = asset("terranix/modules/null.nix").read_text()
    position = cursor_after(source, "hex", needle="random_id.suffix.hex", offset=2)
    completion = await _completion(lsp_server, types.CompletionParams(types.TextDocumentIdentifier(uri), position))

    assert completion is not None
    labels = {item.label for item in completion.items}
    assert labels == {"hex"}
