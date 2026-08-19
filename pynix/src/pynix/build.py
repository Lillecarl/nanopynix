from __future__ import annotations

# A real import, not a TYPE_CHECKING one: clypi resolves the annotations on
# the command below at runtime to build its argument parser, so `Path` has to
# exist as an object and not just as a lazy PEP 563 string.
from pathlib import Path
from typing import override

from clypi import arg

from pynix import _impl
from pynix._settings import (
    ConfiguredCommand,
    attr_option,
    eval_store_option,
    file_option,
    flake_option,
    print_build_logs_option,
    store_option,
    verbosity_option,
)


def _no_sandbox_paths() -> list[str]:
    """Default for ``--sandbox-path``. A named function rather than ``list``,
    which would give the field an unparameterised ``list[Unknown]``."""
    return []


class Build(ConfiguredCommand):
    """Build a Nix derivation value"""

    file: str | None = file_option()

    attr: str | None = attr_option()

    flake: str | None = flake_option()

    store: str = store_option("Store URI to build with.")

    eval_store: str | None = eval_store_option()

    # None, not a literal list: an absent flag leaves the value to `[nix]`, to
    # `PYNIX_NIX_*`, and then to the built-in defaults, in that order. These
    # used to be clypi defaults passed straight into the settings model as
    # keyword arguments, and pydantic-settings ranks the init source above the
    # environment -- so `PYNIX_NIX_SUBSTITUTERS` never once took effect.
    substituters: str | None = arg(None, help="Space-separated substituter URLs.")

    trusted_public_keys: str | None = arg(None, help="Space-separated substituter public keys.")

    # None, not a literal level: nanopynix leaves Nix's own compiled-in default
    # (info) alone when no verbosity is named, and pynix has no reason to
    # disagree with the library it dogfoods. This used to force notice, so
    # `pynix build` was quieter than every other consumer of the same session.
    verbosity: str | None = verbosity_option()

    print_build_logs: bool = print_build_logs_option()

    update_fod: bool = arg(False, help="Update plain fixed-output hash literals after a hash mismatch.")

    dry_run: bool = arg(False, help="Show --update-fod changes without writing or rebuilding.")

    namespaced: bool = arg(
        False,
        help=(
            "Build in a private user namespace, against an overlay store whose lower layer is the host "
            "store. Nothing is copied in, the host store does not change, and this process owns the "
            "sandbox settings that the daemon otherwise controls. Linux only."
        ),
    )

    overlay_dir: Path | None = arg(
        None,
        help=(
            "Keep the overlay's upper layer here, instead of in a temporary directory that is deleted "
            "on exit. Reuse the same directory to keep what earlier --namespaced builds produced. "
            "Implies --namespaced."
        ),
    )

    copy_back: bool = arg(
        True,
        # Snake case, not the dashed spelling: clypi normalises the parsed
        # option to snake case before it compares against this.
        negative="no_copy_back",
        help=(
            "Copy the outputs of a --namespaced build into the host store when the build succeeds. "
            "Without it the outputs are gone when the worker exits."
        ),
    )

    sandbox_path: list[str] = arg(
        default_factory=_no_sandbox_paths,
        help=(
            "Extra path to mount into the build sandbox, as /inside=/outside or /path. Repeatable. "
            "Requires --namespaced, because the daemon does not let a client change its sandbox."
        ),
    )

    @override
    async def run(self) -> None:
        await _impl.build.run_build(self)
