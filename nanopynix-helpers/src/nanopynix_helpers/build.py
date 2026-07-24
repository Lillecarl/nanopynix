"""Build a Nix target, auto-patching fixed-output hashes on mismatch."""

from __future__ import annotations

from typing import TYPE_CHECKING

from anyio import Path as AsyncPath

from nanopynix.exceptions import StoreError
from nanopynix_helpers.fod import (
    FodSourceUpdateError,
    derivation_name_from_path,
    extract_fod_hash_mismatch,
    extract_unique_fod_hash_mismatch,
    find_fod_hash_literal,
    mismatch_is_target_fod,
    replace_fod_hash,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path

    from nanopynix import Session, Store
    from nanopynix.rpc.client import EvalSession, ValueProxy


class FodBuildError(RuntimeError):
    """A build could not be completed, or safely retried, after a FOD mismatch."""


async def build_with_fod_update(  # noqa: C901, PLR0912, PLR0913 tracked complexity/arg-count debt, see TODO.md
    evaluate: Callable[[], Awaitable[ValueProxy]],
    *,
    nix: Session,
    eval_session: EvalSession,
    evaluation_store: Store,
    build_store: Store | None = None,
    update_fod: bool,
    source_file: Path | None,
    max_updates: int = 10,
    dry_run: bool = False,
    on_hash_update: Callable[[Path, str, str], None] | None = None,
) -> tuple[dict[str, str], int]:
    """Build a target, applying only unambiguous plain-string FOD updates.

    *evaluate* is re-invoked on every retry so a caller backed by a mutable
    source file observes its own edits. On a fixed-output hash mismatch that
    is verified to belong to the evaluated target's own derivation closure,
    *source_file* is patched in place with Nix's reported hash and the build
    is retried, up to *max_updates* times. *on_hash_update*, if given, is
    called with the source file and its before/after contents each time a
    hash is patched (e.g. to print a diff) before the file is written.
    """
    updates = 0
    while True:
        root: ValueProxy | None = None
        build_error: StoreError | None = None
        outputs: dict[str, str] = {}
        async with nix.capture_logs() as logs:
            try:
                root = await evaluate()
                outputs = await (root.build() if build_store is None else root.build(store=build_store))
            except StoreError as exc:
                build_error = exc
        if build_error is None:
            return outputs, updates
        mismatch = extract_fod_hash_mismatch(build_error.msg)
        # ``LogCapture`` has now observed the request-finalized marker, so its
        # event list includes Nix diagnostics delivered after the RPC response.
        if mismatch is None:
            mismatch = extract_unique_fod_hash_mismatch(
                event.message_without_ansi for event in logs.events if event.message_without_ansi is not None
            )
        if not update_fod or mismatch is None:
            if root is not None:
                await root.release()
            raise build_error
        if mismatch.drv_path is not None and (
            root is None or not await mismatch_is_target_fod(evaluation_store, root, mismatch)
        ):
            if root is not None:
                await root.release()
            raise FodBuildError(
                "fixed-output hash mismatch was not found in the evaluated target derivation closure",
            ) from build_error
        if root is not None:
            await root.release()
        if source_file is None:
            raise FodBuildError("--update-fod currently requires --file") from build_error
        if updates >= max_updates:
            raise FodBuildError(f"stopped after {max_updates} fixed-output hash updates") from build_error
        try:
            source = await AsyncPath(source_file).read_text()
            literal = find_fod_hash_literal(
                source,
                mismatch.specified,
                derivation_name=derivation_name_from_path(mismatch.drv_path),
            )
            updated = replace_fod_hash(source, literal, mismatch.got)
        except (OSError, FodSourceUpdateError) as source_exc:
            raise FodBuildError(f"cannot update fixed-output hash: {source_exc}") from source_exc
        if on_hash_update is not None:
            on_hash_update(source_file, source, updated)
        updates += 1
        if dry_run:
            return {}, updates
        await AsyncPath(source_file).write_text(updated)
        await eval_session.reset_file_cache()
