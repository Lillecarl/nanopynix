"""Immutable NixConfig class for constructing nix.conf content.

Every option from the Nix 2.34 reference manual is represented as a
typed wrapper on NixConfig. Mutation on any wrapper returns a full
copy of the entire NixConfig with the change applied.

Usage:
    cfg = NixConfig()
    cfg2 = cfg.substituters.add("https://cache.nixos.org")
    cfg3 = cfg2.require_sigs.set(False)
    cfg4 = cfg3.experimental_features.add("ca-derivations", "dynamic-derivations")
    print(cfg4.to_nix_conf())

Unset by default -- only explicitly set values render in output.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Iterator


@dataclass(frozen=True)
class NixBool:
    """Bool-typed Nix config setting. Unset by default."""

    _name: str
    _value: bool
    _is_set: bool
    _parent: NixConfig | None

    def set(self, value: bool = True) -> NixConfig:
        assert self._parent is not None
        return replace(
            self._parent, **{self._name: replace(self, _value=value, _is_set=True)}
        )

    def unset(self) -> NixConfig:
        assert self._parent is not None
        return replace(self._parent, **{self._name: replace(self, _is_set=False)})

    @property
    def value(self) -> bool | None:
        return self._value if self._is_set else None

    def render(self) -> str | None:
        if not self._is_set:
            return None
        return "true" if self._value else "false"

    def __bool__(self) -> bool:
        return self._value if self._is_set else False

    def __eq__(self, other: object) -> bool:
        if isinstance(other, NixBool):
            return self._value == other._value and self._is_set == other._is_set
        if isinstance(other, bool):
            return self._is_set and self._value == other
        return NotImplemented

    def __repr__(self) -> str:
        if not self._is_set:
            return f"NixBool({self._name}, unset)"
        return f"NixBool({self._name}, {self._value})"


@dataclass(frozen=True)
class NixInt:
    """Int-typed Nix config setting. Unset by default."""

    _name: str
    _value: int
    _is_set: bool
    _parent: NixConfig | None

    def set(self, value: int) -> NixConfig:
        assert self._parent is not None
        return replace(
            self._parent, **{self._name: replace(self, _value=value, _is_set=True)}
        )

    def unset(self) -> NixConfig:
        assert self._parent is not None
        return replace(self._parent, **{self._name: replace(self, _is_set=False)})

    @property
    def value(self) -> int | None:
        return self._value if self._is_set else None

    def render(self) -> str | None:
        if not self._is_set:
            return None
        return str(self._value)

    def __eq__(self, other: object) -> bool:
        if isinstance(other, NixInt):
            return self._value == other._value and self._is_set == other._is_set
        if isinstance(other, int):
            return self._is_set and self._value == other
        return NotImplemented

    def __repr__(self) -> str:
        if not self._is_set:
            return f"NixInt({self._name}, unset)"
        return f"NixInt({self._name}, {self._value})"


@dataclass(frozen=True)
class NixStr:
    """Str-typed Nix config setting. Unset by default."""

    _name: str
    _value: str
    _is_set: bool
    _parent: NixConfig | None

    def set(self, value: str) -> NixConfig:
        assert self._parent is not None
        return replace(
            self._parent, **{self._name: replace(self, _value=value, _is_set=True)}
        )

    def unset(self) -> NixConfig:
        assert self._parent is not None
        return replace(self._parent, **{self._name: replace(self, _is_set=False)})

    @property
    def value(self) -> str | None:
        return self._value if self._is_set else None

    def render(self) -> str | None:
        if not self._is_set or not self._value:
            return None
        return self._value

    def __eq__(self, other: object) -> bool:
        if isinstance(other, NixStr):
            return self._value == other._value and self._is_set == other._is_set
        if isinstance(other, str):
            return self._is_set and self._value == other
        return NotImplemented

    def __repr__(self) -> str:
        if not self._is_set:
            return f"NixStr({self._name}, unset)"
        return f"NixStr({self._name}, {self._value!r})"


@dataclass(frozen=True)
class NixList:
    """List-typed Nix config setting. Unset by default, supports add/remove."""

    _name: str
    _value: tuple[str, ...]
    _is_set: bool
    _parent: NixConfig | None

    def set(self, *values: str) -> NixConfig:
        assert self._parent is not None
        return replace(
            self._parent, **{self._name: replace(self, _value=values, _is_set=True)}
        )

    def add(self, *values: str) -> NixConfig:
        assert self._parent is not None
        new_value = self._value + values if self._is_set else values
        return replace(
            self._parent, **{self._name: replace(self, _value=new_value, _is_set=True)}
        )

    def remove(self, *values: str) -> NixConfig:
        assert self._parent is not None
        if not self._is_set:
            return self._parent
        new_value = tuple(v for v in self._value if v not in values)
        return replace(self._parent, **{self._name: replace(self, _value=new_value)})

    def unset(self) -> NixConfig:
        assert self._parent is not None
        return replace(self._parent, **{self._name: replace(self, _is_set=False)})

    @property
    def value(self) -> tuple[str, ...] | None:
        return self._value if self._is_set else None

    def render(self) -> str | None:
        if not self._is_set or not self._value:
            return None
        return " ".join(self._value)

    def __len__(self) -> int:
        return len(self._value) if self._is_set else 0

    def __iter__(self):
        return iter(self._value) if self._is_set else iter(())

    def __contains__(self, item: str) -> bool:
        return self._is_set and item in self._value

    def __eq__(self, other: object) -> bool:
        if isinstance(other, NixList):
            return self._value == other._value and self._is_set == other._is_set
        if isinstance(other, (tuple, list)):
            return self._is_set and self._value == tuple(other)
        return NotImplemented

    def __repr__(self) -> str:
        if not self._is_set:
            return f"NixList({self._name}, unset)"
        return f"NixList({self._name}, {self._value})"


def _mk_bool(name: str, parent: NixConfig | None = None) -> NixBool:
    return NixBool(_name=name, _value=False, _is_set=False, _parent=parent)


def _mk_int(name: str, parent: NixConfig | None = None) -> NixInt:
    return NixInt(_name=name, _value=0, _is_set=False, _parent=parent)


def _mk_str(name: str, parent: NixConfig | None = None) -> NixStr:
    return NixStr(_name=name, _value="", _is_set=False, _parent=parent)


def _mk_list(name: str, parent: NixConfig | None = None) -> NixList:
    return NixList(_name=name, _value=(), _is_set=False, _parent=parent)


@dataclass(frozen=True)
class NixConfig:
    """Immutable Nix configuration. Mutation via typed wrappers returns a new copy.

    Usage:
        cfg = NixConfig()
        cfg2 = cfg.substituters.add("https://cache.nixos.org")
        cfg3 = cfg2.require_sigs.set(False)
        print(cfg3.to_nix_conf())

    All settings start unset (not rendered). Use .set()/.add() to enable,
    .unset() to disable.
    """

    # ── Bool settings ──────────────────────────────────────────────

    abort_on_warn: NixBool = field(default_factory=lambda: _mk_bool("abort_on_warn"))
    accept_flake_config: NixBool = field(
        default_factory=lambda: _mk_bool("accept_flake_config")
    )
    allow_dirty: NixBool = field(default_factory=lambda: _mk_bool("allow_dirty"))
    allow_dirty_locks: NixBool = field(
        default_factory=lambda: _mk_bool("allow_dirty_locks")
    )
    allow_import_from_derivation: NixBool = field(
        default_factory=lambda: _mk_bool("allow_import_from_derivation")
    )
    allow_new_privileges: NixBool = field(
        default_factory=lambda: _mk_bool("allow_new_privileges")
    )
    allow_symlinked_store: NixBool = field(
        default_factory=lambda: _mk_bool("allow_symlinked_store")
    )
    allow_unsafe_native_code_during_evaluation: NixBool = field(
        default_factory=lambda: _mk_bool("allow_unsafe_native_code_during_evaluation")
    )
    always_allow_substitutes: NixBool = field(
        default_factory=lambda: _mk_bool("always_allow_substitutes")
    )
    auto_allocate_uids: NixBool = field(
        default_factory=lambda: _mk_bool("auto_allocate_uids")
    )
    auto_optimise_store: NixBool = field(
        default_factory=lambda: _mk_bool("auto_optimise_store")
    )
    builders_use_substitutes: NixBool = field(
        default_factory=lambda: _mk_bool("builders_use_substitutes")
    )
    compress_build_log: NixBool = field(
        default_factory=lambda: _mk_bool("compress_build_log")
    )
    eval_cache: NixBool = field(default_factory=lambda: _mk_bool("eval_cache"))
    fallback: NixBool = field(default_factory=lambda: _mk_bool("fallback"))
    filter_syscalls: NixBool = field(
        default_factory=lambda: _mk_bool("filter_syscalls")
    )
    fsync_metadata: NixBool = field(default_factory=lambda: _mk_bool("fsync_metadata"))
    fsync_store_paths: NixBool = field(
        default_factory=lambda: _mk_bool("fsync_store_paths")
    )
    http2: NixBool = field(default_factory=lambda: _mk_bool("http2"))
    ignore_try: NixBool = field(default_factory=lambda: _mk_bool("ignore_try"))
    impersonate_linux_26: NixBool = field(
        default_factory=lambda: _mk_bool("impersonate_linux_26")
    )
    keep_build_log: NixBool = field(default_factory=lambda: _mk_bool("keep_build_log"))
    keep_derivations: NixBool = field(
        default_factory=lambda: _mk_bool("keep_derivations")
    )
    keep_env_derivations: NixBool = field(
        default_factory=lambda: _mk_bool("keep_env_derivations")
    )
    keep_failed: NixBool = field(default_factory=lambda: _mk_bool("keep_failed"))
    keep_going: NixBool = field(default_factory=lambda: _mk_bool("keep_going"))
    keep_outputs: NixBool = field(default_factory=lambda: _mk_bool("keep_outputs"))
    preallocate_contents: NixBool = field(
        default_factory=lambda: _mk_bool("preallocate_contents")
    )
    print_missing: NixBool = field(default_factory=lambda: _mk_bool("print_missing"))
    pure_eval: NixBool = field(default_factory=lambda: _mk_bool("pure_eval"))
    require_drop_supplementary_groups: NixBool = field(
        default_factory=lambda: _mk_bool("require_drop_supplementary_groups")
    )
    require_sigs: NixBool = field(default_factory=lambda: _mk_bool("require_sigs"))
    restrict_eval: NixBool = field(default_factory=lambda: _mk_bool("restrict_eval"))
    run_diff_hook: NixBool = field(default_factory=lambda: _mk_bool("run_diff_hook"))
    sandbox_fallback: NixBool = field(
        default_factory=lambda: _mk_bool("sandbox_fallback")
    )
    substitute: NixBool = field(default_factory=lambda: _mk_bool("substitute"))
    sync_before_registering: NixBool = field(
        default_factory=lambda: _mk_bool("sync_before_registering")
    )
    trace_function_calls: NixBool = field(
        default_factory=lambda: _mk_bool("trace_function_calls")
    )
    trace_import_from_derivation: NixBool = field(
        default_factory=lambda: _mk_bool("trace_import_from_derivation")
    )
    trace_verbose: NixBool = field(default_factory=lambda: _mk_bool("trace_verbose"))
    trust_tarballs_from_git_forges: NixBool = field(
        default_factory=lambda: _mk_bool("trust_tarballs_from_git_forges")
    )
    use_case_hack: NixBool = field(default_factory=lambda: _mk_bool("use_case_hack"))
    use_cgroups: NixBool = field(default_factory=lambda: _mk_bool("use_cgroups"))
    use_registries: NixBool = field(default_factory=lambda: _mk_bool("use_registries"))
    use_sqlite_wal: NixBool = field(default_factory=lambda: _mk_bool("use_sqlite_wal"))
    use_xdg_base_directories: NixBool = field(
        default_factory=lambda: _mk_bool("use_xdg_base_directories")
    )
    warn_dirty: NixBool = field(default_factory=lambda: _mk_bool("warn_dirty"))
    warn_short_path_literals: NixBool = field(
        default_factory=lambda: _mk_bool("warn_short_path_literals")
    )
    nix_shell_always_looks_for_shell_nix: NixBool = field(
        default_factory=lambda: _mk_bool("nix_shell_always_looks_for_shell_nix")
    )
    nix_shell_shebang_arguments_relative_to_script: NixBool = field(
        default_factory=lambda: _mk_bool(
            "nix_shell_shebang_arguments_relative_to_script"
        )
    )

    # ── Int settings ───────────────────────────────────────────────

    build_poll_interval: NixInt = field(
        default_factory=lambda: _mk_int("build_poll_interval")
    )
    connect_timeout: NixInt = field(default_factory=lambda: _mk_int("connect_timeout"))
    cores: NixInt = field(default_factory=lambda: _mk_int("cores"))
    download_attempts: NixInt = field(
        default_factory=lambda: _mk_int("download_attempts")
    )
    download_buffer_size: NixInt = field(
        default_factory=lambda: _mk_int("download_buffer_size")
    )
    download_speed: NixInt = field(default_factory=lambda: _mk_int("download_speed"))
    eval_attrset_update_layer_rhs_threshold: NixInt = field(
        default_factory=lambda: _mk_int("eval_attrset_update_layer_rhs_threshold")
    )
    eval_profiler_frequency: NixInt = field(
        default_factory=lambda: _mk_int("eval_profiler_frequency")
    )
    gc_reserved_space: NixInt = field(
        default_factory=lambda: _mk_int("gc_reserved_space")
    )
    http_connections: NixInt = field(
        default_factory=lambda: _mk_int("http_connections")
    )
    id_count: NixInt = field(default_factory=lambda: _mk_int("id_count"))
    log_lines: NixInt = field(default_factory=lambda: _mk_int("log_lines"))
    max_build_log_size: NixInt = field(
        default_factory=lambda: _mk_int("max_build_log_size")
    )
    max_call_depth: NixInt = field(default_factory=lambda: _mk_int("max_call_depth"))
    max_free: NixInt = field(default_factory=lambda: _mk_int("max_free"))
    max_jobs: NixInt = field(default_factory=lambda: _mk_int("max_jobs"))
    max_silent_time: NixInt = field(default_factory=lambda: _mk_int("max_silent_time"))
    max_substitution_jobs: NixInt = field(
        default_factory=lambda: _mk_int("max_substitution_jobs")
    )
    min_free: NixInt = field(default_factory=lambda: _mk_int("min_free"))
    min_free_check_interval: NixInt = field(
        default_factory=lambda: _mk_int("min_free_check_interval")
    )
    nar_buffer_size: NixInt = field(default_factory=lambda: _mk_int("nar_buffer_size"))
    narinfo_cache_meta_ttl: NixInt = field(
        default_factory=lambda: _mk_int("narinfo_cache_meta_ttl")
    )
    narinfo_cache_negative_ttl: NixInt = field(
        default_factory=lambda: _mk_int("narinfo_cache_negative_ttl")
    )
    narinfo_cache_positive_ttl: NixInt = field(
        default_factory=lambda: _mk_int("narinfo_cache_positive_ttl")
    )
    stalled_download_timeout: NixInt = field(
        default_factory=lambda: _mk_int("stalled_download_timeout")
    )
    start_id: NixInt = field(default_factory=lambda: _mk_int("start_id"))
    tarball_ttl: NixInt = field(default_factory=lambda: _mk_int("tarball_ttl"))
    timeout: NixInt = field(default_factory=lambda: _mk_int("timeout"))
    warn_large_path_threshold: NixInt = field(
        default_factory=lambda: _mk_int("warn_large_path_threshold")
    )

    # ── Str settings ───────────────────────────────────────────────

    build_dir: NixStr = field(default_factory=lambda: _mk_str("build_dir"))
    build_hook: NixStr = field(default_factory=lambda: _mk_str("build_hook"))
    build_users_group: NixStr = field(
        default_factory=lambda: _mk_str("build_users_group")
    )
    diff_hook: NixStr = field(default_factory=lambda: _mk_str("diff_hook"))
    eval_profile_file: NixStr = field(
        default_factory=lambda: _mk_str("eval_profile_file")
    )
    eval_profiler: NixStr = field(default_factory=lambda: _mk_str("eval_profiler"))
    eval_system: NixStr = field(default_factory=lambda: _mk_str("eval_system"))
    flake_registry: NixStr = field(default_factory=lambda: _mk_str("flake_registry"))
    json_log_path: NixStr = field(default_factory=lambda: _mk_str("json_log_path"))
    lint_absolute_path_literals: NixStr = field(
        default_factory=lambda: _mk_str("lint_absolute_path_literals")
    )
    lint_short_path_literals: NixStr = field(
        default_factory=lambda: _mk_str("lint_short_path_literals")
    )
    lint_url_literals: NixStr = field(
        default_factory=lambda: _mk_str("lint_url_literals")
    )
    netrc_file: NixStr = field(default_factory=lambda: _mk_str("netrc_file"))
    post_build_hook: NixStr = field(default_factory=lambda: _mk_str("post_build_hook"))
    pre_build_hook: NixStr = field(default_factory=lambda: _mk_str("pre_build_hook"))
    sandbox: NixStr = field(default_factory=lambda: _mk_str("sandbox"))
    sandbox_build_dir: NixStr = field(
        default_factory=lambda: _mk_str("sandbox_build_dir")
    )
    sandbox_dev_shm_size: NixStr = field(
        default_factory=lambda: _mk_str("sandbox_dev_shm_size")
    )
    ssl_cert_file: NixStr = field(default_factory=lambda: _mk_str("ssl_cert_file"))
    store: NixStr = field(default_factory=lambda: _mk_str("store"))
    system: NixStr = field(default_factory=lambda: _mk_str("system"))
    upgrade_nix_store_path_url: NixStr = field(
        default_factory=lambda: _mk_str("upgrade_nix_store_path_url")
    )
    user_agent_suffix: NixStr = field(
        default_factory=lambda: _mk_str("user_agent_suffix")
    )
    commit_lock_file_summary: NixStr = field(
        default_factory=lambda: _mk_str("commit_lock_file_summary")
    )
    bash_prompt: NixStr = field(default_factory=lambda: _mk_str("bash_prompt"))
    bash_prompt_prefix: NixStr = field(
        default_factory=lambda: _mk_str("bash_prompt_prefix")
    )
    bash_prompt_suffix: NixStr = field(
        default_factory=lambda: _mk_str("bash_prompt_suffix")
    )

    # ── List settings ──────────────────────────────────────────────

    access_tokens: NixList = field(default_factory=lambda: _mk_list("access_tokens"))
    allowed_impure_host_deps: NixList = field(
        default_factory=lambda: _mk_list("allowed_impure_host_deps")
    )
    allowed_uris: NixList = field(default_factory=lambda: _mk_list("allowed_uris"))
    allowed_users: NixList = field(default_factory=lambda: _mk_list("allowed_users"))
    builders: NixList = field(default_factory=lambda: _mk_list("builders"))
    experimental_features: NixList = field(
        default_factory=lambda: _mk_list("experimental_features")
    )
    external_builders: NixList = field(
        default_factory=lambda: _mk_list("external_builders")
    )
    extra_platforms: NixList = field(
        default_factory=lambda: _mk_list("extra_platforms")
    )
    hashed_mirrors: NixList = field(default_factory=lambda: _mk_list("hashed_mirrors"))
    ignored_acls: NixList = field(default_factory=lambda: _mk_list("ignored_acls"))
    impure_env: NixList = field(default_factory=lambda: _mk_list("impure_env"))
    nix_path: NixList = field(default_factory=lambda: _mk_list("nix_path"))
    plugin_files: NixList = field(default_factory=lambda: _mk_list("plugin_files"))
    sandbox_paths: NixList = field(default_factory=lambda: _mk_list("sandbox_paths"))
    secret_key_files: NixList = field(
        default_factory=lambda: _mk_list("secret_key_files")
    )
    substituters: NixList = field(default_factory=lambda: _mk_list("substituters"))
    system_features: NixList = field(
        default_factory=lambda: _mk_list("system_features")
    )
    trusted_public_keys: NixList = field(
        default_factory=lambda: _mk_list("trusted_public_keys")
    )
    trusted_substituters: NixList = field(
        default_factory=lambda: _mk_list("trusted_substituters")
    )
    trusted_users: NixList = field(default_factory=lambda: _mk_list("trusted_users"))

    # ── Parent linking ─────────────────────────────────────────────

    def __post_init__(self) -> None:
        for f in type(self).__dataclass_fields__.values():
            val = getattr(self, f.name)
            if isinstance(val, (NixBool, NixInt, NixStr, NixList)):
                if val._parent is not self:
                    object.__setattr__(self, f.name, replace(val, _parent=self))

    # ── Rendering ──────────────────────────────────────────────────

    _EXTRA_PREFIX_SETTINGS = frozenset(
        {
            "experimental-features",
            "substituters",
            "trusted-substituters",
        }
    )

    def _iter_set(self, *, use_extra_prefix: bool = False) -> Iterator[tuple[str, str]]:
        for f in type(self).__dataclass_fields__.values():
            val = getattr(self, f.name)
            if isinstance(val, (NixBool, NixInt, NixStr, NixList)):
                rendered = val.render()
                if rendered is not None:
                    key = f.name.replace("_", "-")
                    if use_extra_prefix and key in self._EXTRA_PREFIX_SETTINGS:
                        key = f"extra-{key}"
                    yield key, rendered

    def to_nix_conf(self) -> str:
        return "\n".join(f"{k} = {v}" for k, v in self._iter_set())

    def to_nix_config_env(self) -> str:
        return "\n".join(f"{k} = {v}" for k, v in self._iter_set(use_extra_prefix=True))

    def to_extra_args(self) -> list[str]:
        args: list[str] = []
        for k, v in self._iter_set():
            args.extend(["--option", k, v])
        return args

    def to_daemon_args(self) -> list[str]:
        args: list[str] = []
        for k, v in self._iter_set():
            if k in ("require-sigs",):
                args.extend(["--option", k, v])
        return args

    # ── Convenience constructors ───────────────────────────────────

    @classmethod
    def for_test_store(
        cls,
        *,
        substituters: tuple[str, ...] = (
            "https://cache.nixos.org/",
            "unix:///nix/var/nix/daemon-socket/socket?root=/",
        ),
        require_sigs: bool = False,
        experimental_features: tuple[str, ...] = (),
    ) -> NixConfig:
        cfg = cls()
        for s in substituters:
            cfg = cfg.substituters.add(s)
        cfg = cfg.require_sigs.set(require_sigs)
        for f in experimental_features:
            cfg = cfg.experimental_features.add(f)
        return cfg

    @classmethod
    def for_dynamic_derivations(
        cls,
        *,
        substituters: tuple[str, ...] = (),
        require_sigs: bool = False,
    ) -> NixConfig:
        cfg = cls()
        for s in substituters:
            cfg = cfg.substituters.add(s)
        cfg = cfg.require_sigs.set(require_sigs)
        cfg = cfg.experimental_features.add("ca-derivations", "dynamic-derivations")
        return cfg

    @classmethod
    def for_ca_derivations(
        cls,
        *,
        substituters: tuple[str, ...] = (),
        require_sigs: bool = False,
    ) -> NixConfig:
        cfg = cls()
        for s in substituters:
            cfg = cfg.substituters.add(s)
        cfg = cfg.require_sigs.set(require_sigs)
        cfg = cfg.experimental_features.add("ca-derivations")
        return cfg
