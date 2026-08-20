"""The implementation of the ``pynix build`` command.

``pynix.build`` holds the command class and its options, and this module holds
what ``run`` needs. ``pynix._impl`` says why: the parser loads every subcommand module
on every start, and none of these imports is needed to list an option.
"""

from __future__ import annotations

import contextlib
import difflib
import functools
import shutil
import tempfile
from contextlib import AsyncExitStack, asynccontextmanager

# A real import, not a TYPE_CHECKING one: `libpynix` resolves the annotations
# of a command to build its parser, so `Path` has to exist as an object and not
# just as a lazy PEP 563 string.
from pathlib import Path
from typing import TYPE_CHECKING, Any

import anyio
import anyio.to_thread
import structlog
from anyio import Path as AnyioPath
from nanopynix_helpers.build import FodBuildError, build_with_fod_update

import nanopynix
from nanopynix._typechecking import BEARTYPING
from pynix import _impl
from pynix._util import error_console, error_exit, nix_session, print_json, report_and_exit
from pynix.build import Build

if TYPE_CHECKING or BEARTYPING:
    from collections.abc import AsyncGenerator

    # The protocol, and not one engine's class, for the reason
    # `nanopynix_helpers.build` gives. Issue #232.
    from nanopynix.protocols import AsyncValue as ValueProxy
from pynix.target import (
    EvaluationTarget,
    EvaluationTargetError,
    base_attr_search,
    configuration_kind,
    configuration_message,
    evaluate_target,
)

logger = structlog.get_logger("pynix.build")
#: Explicitly the daemon, never "auto" -- see _promote_to_host_store.
_HOST_STORE_URI = "daemon"


@asynccontextmanager
async def _overlay_namespace(
    enabled: bool,
    overlay_dir: Path | None,
) -> AsyncGenerator[nanopynix.OverlayNamespace | None]:
    """Yield the overlay layout for this build, and clean it up if it is ours.

    A directory the user named survives, which is the point of naming one. An
    unnamed one is temporary and goes away with the build.
    """
    if not enabled:
        yield None
        return

    if overlay_dir is not None:
        await AnyioPath(overlay_dir).mkdir(parents=True, exist_ok=True)
        yield nanopynix.OverlayNamespace.under(overlay_dir)
        return

    root = await anyio.to_thread.run_sync(functools.partial(tempfile.mkdtemp, prefix="pynix-overlay-"))
    try:
        yield nanopynix.OverlayNamespace.under(root)
    finally:
        await anyio.to_thread.run_sync(_force_rmtree, root)


def _force_rmtree(root: str) -> None:
    """Remove *root*, including the read-only directories a store leaves.

    Store directories are ``r-xr-xr-x``, and removing what is inside one needs
    the write bit on the directory itself. This process owns them, because the
    namespace mapped it to root, so it can put the bit back.
    """
    for dirpath, dirnames, _ in Path(root).walk():
        for name in dirnames:
            with contextlib.suppress(OSError):
                (dirpath / name).chmod(0o700)
    shutil.rmtree(root, ignore_errors=True)


async def _promote_to_host_store(nix: Any, store: Any, outputs: dict[str, str]) -> None:
    """Copy the outputs of a namespaced build into the host store.

    ``daemon`` by name rather than ``auto``. The worker is root inside its own
    user namespace, so ``auto`` would resolve to a *local* store at
    ``/nix/store`` -- which is the overlay mount, not the host store.

    Signature checking is off because these paths were built here a moment ago
    and nothing has signed them. The daemon still decides: it accepts unsigned
    paths from a trusted user and refuses them otherwise.
    """
    paths = sorted(set(outputs.values()))
    if not paths:
        return
    async with nix.store(_HOST_STORE_URI) as host:
        logger.info("pynix build promoting outputs to the host store", paths=len(paths))
        await store.copy_closure(paths, host, check_sigs=False)


async def _require_a_local_file(target: EvaluationTarget) -> None:
    """Refuse ``--update-fod`` unless ``--file`` names a local file.

    ``--update-fod`` writes the new hash back into the source. A fetched tree
    lives in the store, which is read-only, and a lookup path resolves to one.
    """
    try:
        reference = await target.file_reference()
    except EvaluationTargetError as exc:
        report_and_exit(exc)
    if reference is None or reference.local_path is None:
        error_exit("--update-fod requires --file to name a local file")


async def _evaluate_build_target(target: EvaluationTarget, session: Any) -> ValueProxy:
    try:
        root = await evaluate_target(target, session, auto_call_file=True, attr_search=base_attr_search())
    except EvaluationTargetError as exc:
        raise BuildTargetError(str(exc)) from exc
    # **Before the build, and not after it.** Nix answers a configuration with
    # `selected value is not a derivation`, which is true and is the end of the
    # road: it says neither what the value is nor which path would work. The
    # test costs two attribute reads, and only a value that is an attribute set
    # reaches even the first one.
    kind = await configuration_kind(root)
    if kind is not None:
        raise BuildTargetError(configuration_message(kind, target.selected_attr()))
    return root


class BuildTargetError(EvaluationTargetError):
    pass


async def _build_target(  # noqa: PLR0913 -- tracked complexity/arg-count debt, see TODO.md
    target: EvaluationTarget,
    session: Any,
    *,
    nix: Any,
    evaluation_store: Any,
    build_store: Any = None,
    update_fod: bool,
    dry_run: bool,
) -> tuple[dict[str, str], int]:
    """Build a target, applying only unambiguous plain-string FOD updates."""

    async def _evaluate() -> ValueProxy:
        root = await _evaluate_build_target(target, session)
        logger.info("pynix build target evaluated")
        return root

    # The local file, and not the raw argument. --update-fod rewrites the hash
    # in the source, so a fetched tree in the store and a lookup path have no
    # file to rewrite. `run()` refuses both before it reaches this point.
    reference = await target.file_reference()
    source_file = reference.local_path if reference is not None else None

    try:
        return await build_with_fod_update(
            _evaluate,
            nix=nix,
            eval_session=session,
            evaluation_store=evaluation_store,
            build_store=build_store,
            update_fod=update_fod,
            source_file=source_file,
            dry_run=dry_run,
            on_hash_update=_print_diff,
        )
    except FodBuildError as exc:
        raise BuildTargetError(str(exc)) from exc


def _print_diff(path: Path, before: str, after: str) -> None:
    error_console.print(
        "".join(
            difflib.unified_diff(
                before.splitlines(keepends=True),
                after.splitlines(keepends=True),
                fromfile=str(path),
                tofile=str(path),
            ),
        ),
        markup=False,
        highlight=False,
        end="",
    )


async def run_build(command: Build) -> None:
    """The body of :meth:`pynix.build.Build.run`."""
    target = EvaluationTarget.from_command(command)
    try:
        target.validate(required=True)
    except EvaluationTargetError as exc:
        report_and_exit(exc)
    if command.update_fod:
        await _require_a_local_file(target)
    if command.dry_run and not command.update_fod:
        error_exit("--dry-run requires --update-fod")

    namespaced = _resolve_namespaced(command)

    settings = _impl.settings.nix_settings(
        substituters=command.substituters,
        trusted_public_keys=command.trusted_public_keys,
    )
    if command.sandbox_path:
        settings = settings.model_copy(update={"sandbox_paths": list(command.sandbox_path)})

    async with AsyncExitStack() as stack:
        namespace = await stack.enter_async_context(_overlay_namespace(namespaced, command.overlay_dir))
        # Passed only when there is one, which is the convention
        # nix_session documents: the test suite substitutes a double for
        # it, and a keyword that is always present makes every such double
        # wrong even for the ordinary build that never wanted a namespace.
        namespace_kwargs: dict[str, Any] = {} if namespace is None else {"namespace": namespace}
        nix = await stack.enter_async_context(
            nix_session(
                settings=settings,
                verbosity=command.verbosity,
                print_build_logs=command.print_build_logs,
                **namespace_kwargs,
            )
        )
        # None, not "auto", when namespaced: the session already defaults
        # to its overlay store, and naming "auto" here would open a
        # different store instead.
        store_uri = None if namespaced else command.store
        promote = namespaced and command.copy_back
        try:
            if command.eval_store is None:
                async with nix.store(store_uri) as store:
                    async with nix.eval(store) as session:
                        logger.info("pynix build evaluating target")
                        outputs, updates = await _build_target(
                            target,
                            session,
                            nix=nix,
                            evaluation_store=store,
                            update_fod=command.update_fod,
                            dry_run=command.dry_run,
                        )
                    logger.info("pynix build finished")
                    if promote:
                        await _promote_to_host_store(nix, store, outputs)
            else:
                async with (
                    nix.store(command.eval_store) as eval_store,
                    nix.store(store_uri) as build_store,
                ):
                    async with nix.eval(eval_store) as session:
                        logger.info("pynix build evaluating target")
                        outputs, updates = await _build_target(
                            target,
                            session,
                            nix=nix,
                            evaluation_store=eval_store,
                            build_store=build_store,
                            update_fod=command.update_fod,
                            dry_run=command.dry_run,
                        )
                    logger.info("pynix build finished")
                    if promote:
                        await _promote_to_host_store(nix, build_store, outputs)
        except BuildTargetError as exc:
            report_and_exit(exc)

    print_json({"outputs": outputs, "updatedFods": updates, "dryRun": command.dry_run})


def _resolve_namespaced(command: Build) -> bool:
    """Decide whether this build gets its own namespace, and reject the
    flag combinations that cannot mean anything."""
    namespaced = command.namespaced or command.overlay_dir is not None
    if command.sandbox_path and not namespaced:
        error_exit("--sandbox-path requires --namespaced: the daemon owns the sandbox of the host store")
    # What the caller typed, and not what `command.store` holds: a namespaced
    # build owns its store, so naming one on the command line is a
    # contradiction. A store that the environment or the configuration file
    # supplied is not a request about *this* build, and must not refuse it.
    if namespaced and "store" in command.explicit_options:
        error_exit("--namespaced builds in its own overlay store, so it cannot be combined with --store")
    return namespaced
