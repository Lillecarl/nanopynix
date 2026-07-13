from __future__ import annotations

from typing import Any

import anyio
import pytest
from pydantic import ValidationError

from nanopynix import Session
from nanopynix.settings import (
    NixSettingMetadata,
    NixSettings,
    NixSettingsEnv,
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


def test_nix_settings_file_path(tmp_path) -> None:
    config = tmp_path / "nix.conf"
    config.write_text("max-jobs = 8\nkeep-going = false\n")

    settings = NixSettings.from_file(anyio.Path(config))

    assert settings.max_jobs == 8
    assert settings.keep_going is False


def test_nix_settings_env_reads_pynix_prefixed_values(monkeypatch) -> None:
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


def test_session_defaults_to_flakes_and_nix_command() -> None:
    session = Session()

    assert session._manager._settings["experimental-features"] == "flakes nix-command"


def test_session_rejects_raw_settings_dict() -> None:
    settings: Any = {"max-jobs": "4"}
    with pytest.raises(TypeError, match="settings must be"):
        Session(settings=settings)
