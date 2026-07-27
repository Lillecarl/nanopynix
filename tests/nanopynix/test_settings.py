from __future__ import annotations

from typing import TYPE_CHECKING, Any

import anyio
import pytest
from pydantic import ValidationError

from nanopynix.rpc import Session
from nanopynix.settings import (
    DEFAULT_EXPERIMENTAL_FEATURES,
    DEFAULT_LINE_EDITORS,
    NanopynixSettings,
    NixEvalSettings,
    NixFetchSettings,
    NixFlakeSettings,
    NixSettingMetadata,
    NixSettings,
    NixSettingsEnv,
    check_all_settings_model_drift,
    check_settings_model_drift,
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


def test_session_uses_nanopynix_rpc_timeout() -> None:
    session = Session(runtime_settings=NanopynixSettings(rpc_timeout=12))

    assert session._manager.rpc_timeout == 12  # type: ignore[reportPrivateUsage] -- intentional test of internal Session state


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
    session = Session()

    expected = " ".join(DEFAULT_EXPERIMENTAL_FEATURES)
    assert session._manager._settings["experimental-features"] == expected  # type: ignore[reportPrivateUsage] -- intentional test of internal Session state
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
