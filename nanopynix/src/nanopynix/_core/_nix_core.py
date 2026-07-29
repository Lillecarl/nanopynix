"""Shared direct-pointer Nix operations used by in-process and worker APIs."""

from __future__ import annotations

from typing import TYPE_CHECKING

from nanopynix_bindings import expr as nanopynix_expr, store as nanopynix_store, util as nanopynix_util

from nanopynix._typechecking import BEARTYPING
from nanopynix.settings import SettingsProvenance

if TYPE_CHECKING or BEARTYPING:
    from collections.abc import Mapping, Sequence


def build_mode_value(build_mode: nanopynix_store.BuildMode | int | None) -> int:
    """Normalise the three ways a caller may name a build mode to Nix's int.

    Shared because both engines accept the same three spellings and must agree
    on what each means: the enum, the raw int Nix uses on the wire, and
    ``None`` for "normal". Previously one copy lived on rpc's ValueProxy while
    inproc simply typed the parameter ``Any`` and called ``int()`` on it, which
    accepted anything with an ``__int__`` and rejected the enum's own name.
    """
    if build_mode is None:
        return nanopynix_store.BuildMode.Normal.value
    if isinstance(build_mode, int):
        return build_mode
    return build_mode.value


class NixCore:
    """Pointer-level Nix operations that must run on a Nix thread.

    This class deliberately has no asyncio, handle registry, or protobuf
    dependency. ``nanopynix.inproc`` retains its objects directly, while the
    worker wraps the same objects in RPC handles.
    """

    def initialize(
        self,
        *,
        settings: Mapping[str, str],
        load_config: bool,
        verbosity: int | None,
    ) -> SettingsProvenance:
        """Initialise Nix, then apply ``settings``, and report what came from where.

        The settings are applied **after** ``init_libstore``, not before.
        ``initLibStore`` is the call that reads ``nix.conf`` and ``NIX_CONFIG``,
        through ``loadConfFile``, so settings applied first are overwritten by
        whatever the host configured. It reads no setting of its own -- it is
        ``initLibUtil``, ``loadConfFile``, ``preloadNSS`` and
        ``curl_global_init`` -- so nothing needs a value early.

        This ordering is load-bearing rather than cosmetic. With the settings
        applied first, a caller's value was silently discarded for every key the
        host's ``nix.conf`` also named: measured with ``max-jobs``, the caller
        asked for 4, ``nix.conf`` said 99, and Nix used 99.
        """
        nanopynix_util.init_libstore(load_config=load_config)
        # Whatever loadConfFile just set is, by definition, the overridden set.
        from_config = dict(nanopynix_util.list_settings(overridden_only=True))
        # Reset the bookkeeping only -- no value changes -- so the same query
        # after applying our settings reports ours and nothing else.
        nanopynix_util.reset_overridden()

        for name, value in settings.items():
            nanopynix_util.set_setting(name, value)
        applied = dict(nanopynix_util.list_settings(overridden_only=True))

        if verbosity is not None:
            nanopynix_util.set_verbosity(verbosity)
        nanopynix_expr.init_libexpr()
        return SettingsProvenance(from_config=from_config, applied=applied)

    # Not keyword-only: the worker dispatches it through `run_request`, which
    # forwards positional arguments only.
    def list_settings(self, overridden_only: bool = False) -> dict[str, str]:
        """Read Nix's global settings registry.

        With ``overridden_only``, only the settings something has set, which is
        what tells an applied value apart from a default.
        """
        return dict(nanopynix_util.list_settings(overridden_only=overridden_only))

    def apply_settings(self, settings: Mapping[str, str]) -> dict[str, str]:
        """Write ``settings`` into Nix's global registry, and read each one back.

        The value comes back from Nix rather than being echoed, so a caller
        sees what Nix made of it. An unknown name raises, because a setting
        that goes nowhere is the failure this whole layer exists to prevent.

        This does not reach a store or an evaluator that already exists. Both
        read their settings while Nix constructs them, so the callers guard
        against that by refusing to write while either is open.
        """
        for name, value in settings.items():
            nanopynix_util.set_setting(name, value)
        applied: dict[str, str] = {}
        for name in settings:
            read_back: str | None = nanopynix_util.get_setting(name)
            if read_back is None:
                # `set_setting` raises for a name Nix does not know, so a name
                # that vanishes between the write and the read is a defect in
                # the bindings rather than a caller mistake.
                raise RuntimeError(f"Nix accepted the setting {name!r} and then reported no value for it")
            applied[name] = read_back
        return applied

    def open_store(self, uri: str) -> nanopynix_store.Store:
        return nanopynix_store.open_store(uri)

    def open_eval_state(
        self,
        store: nanopynix_store.Store,
        nix_path: Sequence[str],
        build_store: nanopynix_store.Store | None = None,
        eval_settings: Mapping[str, str] | None = None,
        fetch_settings: Mapping[str, str] | None = None,
    ) -> nanopynix_expr.EvalState:
        return nanopynix_expr.EvalState(
            store,
            list(nix_path),
            build_store,
            dict(eval_settings) if eval_settings else {},
            dict(fetch_settings) if fetch_settings else {},
        )

    def get_verbosity(self) -> int:
        return nanopynix_util.get_verbosity()

    def set_verbosity(self, verbosity: int) -> int:
        nanopynix_util.set_verbosity(verbosity)
        return nanopynix_util.get_verbosity()
