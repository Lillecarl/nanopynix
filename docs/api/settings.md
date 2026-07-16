# Settings

Typed Pydantic models for Nix configuration, plus helpers that compare these
models against Nix's own live setting registry to catch drift when Nix adds
or renames settings. See {doc}`../examples` for a runnable walkthrough.

```{eval-rst}
.. autoclass:: nanopynix.NixSettings
   :members:

.. autoclass:: nanopynix.NanopynixSettings
   :members:

.. autoclass:: nanopynix.NixEvalSettings
   :members:

.. autoclass:: nanopynix.NixFetchSettings
   :members:

.. autoclass:: nanopynix.NixFlakeSettings
   :members:

.. autoclass:: nanopynix.NixSettingsEnv
   :members:

.. autoclass:: nanopynix.NixSettingMetadata
   :members:

.. autoclass:: nanopynix.SettingsDrift
   :members:

.. autofunction:: nanopynix.list_settings_metadata
.. autofunction:: nanopynix.list_eval_settings_metadata
.. autofunction:: nanopynix.list_fetch_settings_metadata
.. autofunction:: nanopynix.list_flake_settings_metadata
.. autofunction:: nanopynix.check_settings_model_drift
.. autofunction:: nanopynix.check_all_settings_model_drift
```
