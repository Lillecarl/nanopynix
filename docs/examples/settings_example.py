"""Inspect and configure Nix settings from Python.

Run with::

    python docs/examples/settings_example.py
"""

# ruff: noqa: T201
# The printed output is the example. These are run by hand and by
# tests/nanopynix/test_examples.py; a logger would hide the very thing
# they exist to show.

from __future__ import annotations

import asyncio

import nanopynix
from nanopynix import NixGlobalSettings, NixSettings, list_settings_metadata
from nanopynix.rpc import Session


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

    # --- query Nix's live setting registry ------------------------------

    metadata = list_settings_metadata()
    assert "max-jobs" in metadata
    print("max-jobs description:", metadata["max-jobs"].description)
    print("max-jobs default:", metadata["max-jobs"].default_value)

    print("\nAll assertions passed.")


if __name__ == "__main__":
    asyncio.run(main())
