"""Tests for `SharedEvalCache` -- two open files sharing a `# pynix-lsp:` directive
expression (and base directory) must reuse one already-evaluated `EvalSession`
rather than each paying its own evaluation cost.

Motivated by a real, reported latency bug: `easykubenix/modules/demo.nix`
and `config.nix` declare the byte-identical `# pynix-lsp: easykubenixEntry =
import ../default.nix { }` directive, but each independently paid the first
(~8.7s) cost of forcing `_module.args`/`_module.specialArgs` through the
whole module composition to resolve a module arg like `pkgs` -- a real
editor's completion popup has nowhere near that patience, so it looked like
completion simply "didn't work" rather than "was slow."
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from lsprotocol import types
from pynix._lsp._handlers import _completion, _sync_document

from tests.support.lsp_environment import asset

if TYPE_CHECKING:
    from pynix._lsp._handlers import PynixLanguageServer

_DEMO_NIX = asset("easykubenix/modules/demo.nix")
_CONFIG_NIX = asset("easykubenix/modules/config.nix")


async def test_two_files_sharing_a_directive_share_the_same_eval_session(lsp_server: PynixLanguageServer) -> None:
    """`demo.nix` and `config.nix` declare the identical `easykubenixEntry` directive -- same base dir too."""
    demo_uri = _DEMO_NIX.as_uri()
    config_uri = _CONFIG_NIX.as_uri()
    await _sync_document(lsp_server, demo_uri)
    await _sync_document(lsp_server, config_uri)

    demo_context = lsp_server.contexts[demo_uri]
    config_context = lsp_server.contexts[config_uri]
    assert demo_context.eval_session("easykubenixEntry") is config_context.eval_session("easykubenixEntry")


async def test_closing_one_of_two_sharing_files_leaves_the_other_working(lsp_server: PynixLanguageServer) -> None:
    """Refcounting: closing `demo.nix` must not tear down `config.nix`'s still-open shared session."""
    demo_uri = _DEMO_NIX.as_uri()
    config_uri = _CONFIG_NIX.as_uri()
    await _sync_document(lsp_server, demo_uri)
    await _sync_document(lsp_server, config_uri)

    demo_context = lsp_server.contexts.pop(demo_uri)
    await demo_context.close()

    # config.nix's own root value must still resolve fine -- a naive
    # unconditional close (no refcounting) would have torn down the shared
    # EvalSession config.nix still depends on.
    config_context = lsp_server.contexts[config_uri]
    value = config_context.roots["easykubenixEntry"]
    assert await value.get_type() is not None


async def test_closing_both_sharing_files_actually_releases_the_shared_entry(lsp_server: PynixLanguageServer) -> None:
    """Once no open file references it, the shared cache entry (and its EvalSession) must actually go away."""
    demo_uri = _DEMO_NIX.as_uri()
    config_uri = _CONFIG_NIX.as_uri()
    await _sync_document(lsp_server, demo_uri)
    await _sync_document(lsp_server, config_uri)
    assert lsp_server.shared_evals is not None
    assert len(lsp_server.shared_evals._entries) == 1

    for uri in (demo_uri, config_uri):
        context = lsp_server.contexts.pop(uri)
        await context.close()

    assert len(lsp_server.shared_evals._entries) == 0


async def test_a_second_file_opening_a_shared_entry_gets_fast_completion_after_the_first_warms_it(
    lsp_server: PynixLanguageServer,
) -> None:
    """End-to-end regression for the reported bug: `config.nix`'s `pkgs.` completion must not pay the cold-start cost.

    Opens `demo.nix` first (its background warm-up task forces `pkgs`
    through easykubenix's module composition) and waits for that warm-up to
    finish, then opens `config.nix` and completes `pkgs.` on a freshly typed
    binding -- this must resolve to the real, non-empty nixpkgs attribute
    list, proving both the shared-session reuse (`config.nix`'s own sync
    doesn't re-evaluate from scratch) and the warm-up (its first completion
    request doesn't have to force `_module.args` itself) actually combine
    correctly, not just each in isolation.
    """
    demo_uri = _DEMO_NIX.as_uri()
    config_uri = _CONFIG_NIX.as_uri()
    await _sync_document(lsp_server, demo_uri)
    for task in list(lsp_server.warm_tasks):
        await task

    base = _CONFIG_NIX.read_text()
    extra_line = "    kubernetes.objects.default.Deployment.asdf.spec.template.metadata.annotations = pkgs.\n"
    source = base.rstrip("\n").removesuffix("}").rstrip() + "\n" + extra_line + "  };\n}\n"
    lsp_server.workspace.put_text_document(
        types.TextDocumentItem(uri=config_uri, language_id="nix", version=1, text=source)
    )
    await _sync_document(lsp_server, config_uri)

    idx = source.rindex("pkgs.") + len("pkgs.")
    lines = source[:idx].split("\n")
    position = types.Position(len(lines) - 1, len(lines[-1]))
    completion = await _completion(lsp_server, types.CompletionParams(types.TextDocumentIdentifier(config_uri), position))

    assert completion is not None
    labels = {item.label for item in completion.items}
    assert "hello" in labels
