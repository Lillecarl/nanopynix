from __future__ import annotations

from pathlib import Path
from typing import Any

import anyio
import pytest
from pydantic import ValidationError

import nanopynix
from nanopynix import Session
from nanopynix.settings import (
    NixEvalSettings,
    NixFetchSettings,
    NixFlakeSettings,
    NixSettingMetadata,
    NixSettings,
    NixSettingsEnv,
    check_all_settings_model_drift,
    check_settings_model_drift,
)


def test_nix_settings_render_python_field_names() -> None:
    settings = NixSettings(max_jobs=4, keep_going=True, substituters=["https://cache.nixos.org/"])

    assert settings.to_worker_settings() == {
        "experimental-features": "flakes nix-command",
        "keep-going": "true",
        "max-jobs": "4",
        "substituters": "https://cache.nixos.org/",
    }


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
        }
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


def test_session_defaults_to_flakes_and_nix_command() -> None:
    session = Session()

    assert session._manager._settings["experimental-features"] == "flakes nix-command"  # type: ignore[reportPrivateUsage] -- intentional test of internal Session state


def test_session_rejects_raw_settings_dict() -> None:
    settings: Any = {"max-jobs": "4"}
    with pytest.raises(TypeError, match="settings must be"):
        Session(settings=settings)
