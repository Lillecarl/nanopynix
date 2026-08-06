from __future__ import annotations

from typing import TYPE_CHECKING, Any

import anyio
import pytest
from pydantic import ValidationError

from nanopynix.rpc import Session
from nanopynix.settings import (
    DEFAULT_EXPERIMENTAL_FEATURES,
    DEFAULT_LINE_EDITORS,
    DEFAULT_WORKER_PRELOAD,
    NanopynixSettings,
    NixEvalSettings,
    NixFetchSettings,
    NixFlakeSettings,
    NixGlobalSettings,
    NixSettingMetadata,
    NixSettings,
    NixSettingsEnv,
    NixStoreDefaults,
    check_all_settings_model_drift,
    check_settings_model_drift,
    merge_defaults,
)

if TYPE_CHECKING:
    from pathlib import Path


def test_nix_settings_render_python_field_names() -> None:
    settings = NixSettings(max_jobs=4, keep_going=True, substituters=["https://cache.nixos.org/"])

    assert settings.to_worker_settings() == {
        "experimental-features": " ".join(DEFAULT_EXPERIMENTAL_FEATURES),
        "keep-going": "true",
        "max-jobs": "4",
        "substituters": "https://cache.nixos.org/",
    }


def test_nix_settings_renders_explicit_empty_string() -> None:
    settings = NixSettings(build_users_group="")

    assert settings.to_worker_settings()["build-users-group"] == ""


def test_nix_settings_accepts_nix_aliases() -> None:
    settings = NixSettings.model_validate({"max-jobs": 2, "trusted-public-keys": ["cache-key"]})

    assert settings.max_jobs == 2
    assert settings.trusted_public_keys == ["cache-key"]


def test_nix_settings_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        NixSettings.model_validate({"not-a-nix-setting": True})


def test_nix_settings_file_path(tmp_path: Path) -> None:
    config = tmp_path / "nix.conf"
    config.write_text("max-jobs = 8\nkeep-going = false\n")

    settings = NixSettings.from_file(anyio.Path(config))

    assert settings.max_jobs == 8
    assert settings.keep_going is False


def test_nix_settings_env_reads_pynix_prefixed_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PYNIX_NIX_MAX_JOBS", "3")
    monkeypatch.setenv("PYNIX_NIX_KEEP_GOING", "true")

    settings = NixSettingsEnv()

    assert settings.max_jobs == 3
    assert settings.keep_going is True


def test_nanopynix_settings_env_reads_runtime_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NANOPYNIX_RPC_TIMEOUT", "30")
    monkeypatch.setenv("NANOPYNIX_SHUTDOWN_TIMEOUT", "10")
    monkeypatch.setenv("NANOPYNIX_LINE_EDITORS", '["hx", "code"]')

    settings = NanopynixSettings()

    assert settings.rpc_timeout == 30
    assert settings.shutdown_timeout == 10
    assert settings.line_editors == ["hx", "code"]


def test_nanopynix_settings_defaults_line_editors() -> None:
    assert NanopynixSettings().line_editors == list(DEFAULT_LINE_EDITORS)


def test_nanopynix_settings_defaults_worker_preload() -> None:
    assert NanopynixSettings().worker_preload == list(DEFAULT_WORKER_PRELOAD)


def test_session_uses_nanopynix_rpc_timeout() -> None:
    session = Session(runtime_settings=NanopynixSettings(rpc_timeout=12))

    assert session._manager.rpc_timeout == 12  # type: ignore[reportPrivateUsage] -- intentional test of internal Session state


@pytest.mark.parametrize("preload", [[], ["nanopynix.rpc.worker._worker", "json"]])
def test_session_forwards_worker_preload_to_its_worker(preload: list[str]) -> None:
    """The list the caller sets is the list the forkserver gets.

    An empty list included, because that is the case the setting exists for:
    it is how a caller keeps a module out of the forkserver, and a falsy value
    is easy to drop on the way through. ``multiprocessing_pipe_pair`` only
    calls ``set_forkserver_preload`` when the list is non-empty, so an empty
    one leaves the default of the interpreter, which preloads nothing.
    """
    session = Session(runtime_settings=NanopynixSettings(worker_preload=preload))

    assert session._manager._worker_preload == preload  # type: ignore[reportPrivateUsage] -- intentional test of internal Session state


def test_settings_drift_reports_missing_and_extra() -> None:
    metadata = {
        "max-jobs": NixSettingMetadata(),
        "new-from-nix": NixSettingMetadata(),
    }

    drift = check_settings_model_drift(metadata)

    assert "new-from-nix" in drift.missing
    assert "show-trace" in drift.extra
    assert not drift.ok


def test_eval_settings_accepts_nix_aliases() -> None:
    settings = NixEvalSettings.model_validate(
        {
            "allow-import-from-derivation": False,
            "debugger-on-warn": True,
            "nix-path": ["nixpkgs=/tmp/nixpkgs"],
        },
    )

    assert settings.allow_import_from_derivation is False
    assert settings.debugger_on_warn is True
    assert settings.nix_path == ["nixpkgs=/tmp/nixpkgs"]


def test_fetch_settings_accepts_string_map() -> None:
    settings = NixFetchSettings.model_validate({"access-tokens": {"github.com": "token"}, "warn-dirty": False})

    assert settings.access_tokens == {"github.com": "token"}
    assert settings.warn_dirty is False


def test_flake_settings_accepts_nix_aliases() -> None:
    settings = NixFlakeSettings.model_validate({"accept-flake-config": True, "use-registries": False})

    assert settings.accept_flake_config is True
    assert settings.use_registries is False


def test_optional_settings_drift_is_not_checked_by_default() -> None:
    drift = check_all_settings_model_drift()

    assert set(drift) == {"global"}


# ── for_scope: taking one scope out of the catch-all ─────────────────


def test_for_scope_takes_one_scope_out_of_the_catch_all() -> None:
    """``NixSettings`` inherits five scopes, and each goes to a different Nix.

    This is the split that makes the catch-all work. Sending all five to
    ``globalConfig`` is what used to happen, and four of them raised
    ``unknown setting`` because the registries are disjoint.
    """
    everything = NixSettings(max_jobs=4, trusted=True, pure_eval=True, warn_dirty=False, use_registries=False)

    assert NixGlobalSettings.for_scope(everything).max_jobs == 4
    assert NixStoreDefaults.for_scope(everything).trusted is True
    assert NixEvalSettings.for_scope(everything).pure_eval is True
    assert NixFetchSettings.for_scope(everything).warn_dirty is False
    assert NixFlakeSettings.for_scope(everything).use_registries is False


def test_the_global_scope_carries_no_setting_from_another_registry() -> None:
    """What reaches ``globalConfig`` must hold global keys and nothing else.

    ``set_setting`` raises for a name Nix does not know, so one leaked key
    here fails every session that names it. Asserting on the rendering rather
    than on the model is deliberate: the rendering is what travels.
    """
    rendered = NixGlobalSettings.for_scope(
        NixSettings(max_jobs=4, trusted=True, pure_eval=True, warn_dirty=False, use_registries=False),
    ).to_worker_settings()

    assert rendered["max-jobs"] == "4"
    for foreign in ("trusted", "pure-eval", "warn-dirty", "use-registries"):
        assert foreign not in rendered


def test_for_scope_leaves_a_field_the_catch_all_did_not_set_unset() -> None:
    assert NixEvalSettings.for_scope(NixSettings(max_jobs=4)).pure_eval is None


# ── merge_defaults: a per-call value beats a session default ─────────


def test_merge_defaults_fills_only_what_the_call_left_unset() -> None:
    merged = merge_defaults(
        NixEvalSettings(pure_eval=False),
        NixEvalSettings(pure_eval=True, max_call_depth=20),
    )

    assert merged.pure_eval is False, "the value named on the call wins"
    assert merged.max_call_depth == 20, "and the rest come from the session"


def test_merge_defaults_takes_the_defaults_whole_when_the_call_says_nothing() -> None:
    defaults = NixEvalSettings(pure_eval=True)

    assert merge_defaults(None, defaults) is defaults


def test_merge_defaults_does_not_mutate_either_side() -> None:
    """Both sides outlive the call: the defaults belong to the session."""
    spec = NixFlakeSettings(use_registries=False)
    defaults = NixFlakeSettings(use_registries=True, accept_flake_config=True)

    merge_defaults(spec, defaults)

    assert spec.accept_flake_config is None
    assert defaults.use_registries is True


def test_default_experimental_features_are_the_ones_we_intend() -> None:
    """The one place the default list's *contents* are pinned.

    The two tests that render it reference ``DEFAULT_EXPERIMENTAL_FEATURES``
    rather than a literal, so they keep testing what they are about (name
    rendering, and that Session takes its default from NixSettings) instead of
    failing every time the list changes. That leaves nothing asserting *which*
    features are on by default, which is a real decision -- nanopynix enables
    more than Nix's own defaults on purpose -- so it is pinned here, once.
    """
    assert DEFAULT_EXPERIMENTAL_FEATURES == (
        "flakes",
        "nix-command",
        "ca-derivations",
        "dynamic-derivations",
        "recursive-nix",
    )


def test_session_defaults_to_the_default_experimental_features() -> None:
    """The features travel as their own list, and never as a setting.

    The second assertion is the whole point. As an ``experimental-features``
    setting, the default **replaced** what the ``nix.conf`` of the host
    enabled, and a user who turned a feature on lost it to a default nobody
    asked for. ``enable_experimental_feature`` inserts into the current set
    instead, so ``NixCore.initialize`` takes the list separately.
    """
    session = Session()

    assert session._manager._features == list(DEFAULT_EXPERIMENTAL_FEATURES)  # type: ignore[reportPrivateUsage] -- intentional test of internal Session state
    assert "experimental-features" not in session._manager._settings  # type: ignore[reportPrivateUsage] -- intentional test of internal Session state
    assert session._manager._nix_conf is None  # type: ignore[reportPrivateUsage] -- intentional test of internal Session state
    assert session._manager._load_config  # type: ignore[reportPrivateUsage] -- intentional test of internal Session state


def test_session_config_options(tmp_path: Path) -> None:
    nix_conf = tmp_path / "nix.conf"
    nix_conf.touch()

    session = Session(nix_conf=nix_conf, load_config=False)

    assert session._manager._nix_conf == nix_conf  # type: ignore[reportPrivateUsage] -- intentional test of internal Session state
    assert not session._manager._load_config  # type: ignore[reportPrivateUsage] -- intentional test of internal Session state


def test_session_rejects_missing_nix_conf(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        Session(nix_conf=tmp_path / "missing-nix.conf")


def test_session_rejects_string_nix_conf() -> None:
    with pytest.raises(TypeError, match=r"pathlib\.Path"):
        Session(nix_conf="/etc/nix/nix.conf")  # type: ignore[arg-type] -- validates the public runtime boundary


def test_session_rejects_raw_settings_dict() -> None:
    settings: Any = {"max-jobs": "4"}
    with pytest.raises(TypeError, match="settings must be"):
        Session(settings=settings)


# ── the nix.conf spelling of a multi-valued setting ──────────────────
#
# `_render_value` joins a list with spaces and a dict with spaces over
# `key=value` pairs, which is how Nix writes both. Reading that back was
# missing, so the model could write a value it could not read: every one of
# these cases raised `Input should be a valid list`.


def test_from_file_reads_a_nix_conf_that_sets_a_list(tmp_path: Path) -> None:
    """The documented case. Almost every real nix.conf sets `substituters`."""
    nix_conf = tmp_path / "nix.conf"
    nix_conf.write_text("substituters = https://a.example/ https://b.example/\nmax-jobs = 4\n")

    settings = NixSettings.from_file(nix_conf)

    assert settings.substituters == ["https://a.example/", "https://b.example/"]
    assert settings.max_jobs == 4


def test_a_rendered_list_loads_back_into_the_same_list(tmp_path: Path) -> None:
    """Round trip, so the reader stays the inverse of the writer."""
    original = NixSettings(substituters=["https://a.example/", "https://b.example/"], trusted=True)
    nix_conf = tmp_path / "nix.conf"
    nix_conf.write_text(original.to_nix_config())

    assert NixSettings.from_file(nix_conf).substituters == original.substituters


def test_a_dict_setting_reads_back_from_its_key_value_pairs() -> None:
    settings = NixFetchSettings.model_validate({"access-tokens": "github.com=one gitlab.com=two"})

    assert settings.access_tokens == {"github.com": "one", "gitlab.com": "two"}


def test_a_dict_setting_refuses_an_item_that_is_not_a_pair() -> None:
    with pytest.raises(ValidationError, match="key=value"):
        NixFetchSettings.model_validate({"access-tokens": "github.com"})


@pytest.mark.parametrize(
    "spelling",
    ["https://a.example/ https://b.example/", '["https://a.example/", "https://b.example/"]'],
    ids=["nix-conf", "json"],
)
def test_an_environment_variable_takes_either_spelling_of_a_list(
    monkeypatch: pytest.MonkeyPatch,
    spelling: str,
) -> None:
    """JSON was the only accepted spelling, and it is not the one a user knows."""
    monkeypatch.setenv("PYNIX_NIX_SUBSTITUTERS", spelling)

    assert NixSettingsEnv().substituters == ["https://a.example/", "https://b.example/"]


def test_a_list_that_is_already_a_list_is_not_split_again() -> None:
    """A Python caller and a JSON file must reach the model untouched."""
    settings = NixSettings(substituters=["https://one.example/ with a space"])

    assert settings.substituters == ["https://one.example/ with a space"]
