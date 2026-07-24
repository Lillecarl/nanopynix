"""Inspect and configure Nix settings from Python.

Run with::

    python docs/examples/settings_example.py
"""

# ruff: noqa: T201

from __future__ import annotations

import asyncio

from nanopynix import NixSettings, list_settings_metadata
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
