"""Inspect and configure Nix settings from Python.

Run with::

    python docs/examples/settings_example.py
"""

# ruff: noqa: T201
# The printed output is the example. These are run by hand and by
# nanopynix/tests/test_examples.py; a logger would hide the very thing
# they exist to show.

from __future__ import annotations

import asyncio
import os
from typing import override

from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict

import nanopynix
from nanopynix import NixGlobalSettings, NixSettings, PrefixedEnvSettingsSource, list_settings_metadata
from nanopynix.rpc import Session


# region: prefixed-env
class MySettings(BaseSettings):
    """A settings model whose fields answer to one environment name each.

    Every field below carries a dashed alias, and pydantic-settings reads an
    alias as a second environment name with no prefix. A bare ``cores`` in the
    environment would otherwise reach ``cores`` here.
    """

    model_config = SettingsConfigDict(
        env_prefix="MYTOOL_",
        alias_generator=lambda name: name.replace("_", "-"),
        populate_by_name=True,
    )

    cores: int | None = None
    max_jobs: int | None = None

    @classmethod
    @override
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return (init_settings, PrefixedEnvSettingsSource(settings_cls), dotenv_settings, file_secret_settings)


# endregion: prefixed-env


async def main() -> None:
    # --- build settings from Python values -----------------------------

    settings = NixSettings(max_jobs=4, cores=2, experimental_features=["flakes", "nix-command"])
    print("as nix.conf text:\n" + settings.to_nix_config())

    # Field names are snake_case in Python, rendered as dash-separated
    # nix.conf keys ("max_jobs" -> "max-jobs").
    worker_settings = settings.to_worker_settings()
    assert worker_settings["max-jobs"] == "4"
    assert worker_settings["cores"] == "2"

    # --- add experimental features without clobbering existing ones ----

    extended = settings.with_experimental_features(["ca-derivations"])
    assert extended.experimental_features == ["flakes", "nix-command", "ca-derivations"]
    assert settings.experimental_features == ["flakes", "nix-command"]  # original is untouched
    print("extended features:", extended.experimental_features)

    # --- pass settings into a Session -----------------------------------

    # One object carries every scope, so a session takes one argument rather
    # than one per registry.
    # region: one-object
    async with nanopynix.rpc.Session(
        settings=NixSettings(max_jobs=4, trusted=True, pure_eval=True),
    ) as nix:
        ...
    # endregion: one-object

    # --- read and write the global settings of the session ---------------

    # region: globals
    async with nanopynix.rpc.Session(settings=NixSettings(max_jobs=4)) as nix:
        await nix.settings()  # every setting, with its value
        await nix.settings(overridden_only=True)  # only what something has set
        await nix.settings_provenance()  # host values against ours

        await nix.set_settings(NixGlobalSettings(max_jobs=8))
    # endregion: globals

    async with Session(settings=NixSettings(max_jobs=2)):
        print("session opened with max_jobs=2")

    # --- keep an ambient variable out of a settings model ----------------

    # `os.environ` here stands for the shell that runs the program: `stdenv`
    # exports `system`, so a shell of Nix already sets one of the thirteen
    # single-word `nix.conf` keys.
    os.environ["cores"] = "7"  # noqa: SIM112 -- a lowercase name is the subject; Nix spells its keys this way
    assert MySettings().model_fields_set == set(), "an unprefixed name must reach no field"

    os.environ["MYTOOL_CORES"] = "9"
    assert MySettings().cores == 9
    print("prefixed cores:", MySettings().cores)
    del os.environ["cores"], os.environ["MYTOOL_CORES"]  # noqa: SIM112 -- see above

    # --- query Nix's live setting registry ------------------------------

    metadata = list_settings_metadata()
    assert "max-jobs" in metadata
    print("max-jobs description:", metadata["max-jobs"].description)
    print("max-jobs default:", metadata["max-jobs"].default_value)

    print("\nAll assertions passed.")


if __name__ == "__main__":
    asyncio.run(main())
