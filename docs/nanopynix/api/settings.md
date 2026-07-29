# Settings

Typed Pydantic models for Nix configuration, plus helpers that compare these
models against Nix's own live setting registry to catch drift when Nix adds
or renames settings. See {doc}`../examples` for a runnable walkthrough.

## When Nix reads a setting

Nix does not have one configuration. It has four, and each is read at a
different moment. This is the single most important thing to know about
configuring Nix, because a setting applied after the moment Nix reads it is
accepted, stored, and then never looked at again.

| Moment | What is configured | Model | Change it later? |
|---|---|---|---|
| Process start | The globals in `globalConfig` | {class}`~nanopynix.NixGlobalSettings` | Only the settings Nix re-reads per operation |
| Store construction | That one store's settings | {mod}`nanopynix.stores` | No. Open another store |
| Evaluator construction | Part of that evaluator's settings | {class}`~nanopynix.NixEvalSettings` | Only the fields tagged live |
| The point of use | The rest | {class}`~nanopynix.NixEvalSettings`, {class}`~nanopynix.NixFetchSettings` | Yes |

Each field of {class}`~nanopynix.NixEvalSettings` records which of the last two
it is. {meth}`configure` uses that record: it applies the live fields and
raises {class}`~nanopynix.SettingNotLiveError` for the others, instead of
sending them to a Nix that will ignore them.

These eight are read once, while Nix constructs the evaluator:
`eval_profile_file`, `eval_profiler`, `eval_profiler_frequency`, `eval_system`,
`nix_path`, `pure_eval`, `restrict_eval` and `trace_function_calls`. Pass them
to `session.eval(store, eval_settings=...)`.

Every fetch setting is live. `EvalState` holds a *reference* to the fetcher
settings rather than a copy, so there is no snapshot to go stale.

## One object, or one per scope

{class}`~nanopynix.NixSettings` inherits every scope, so it is the one object
to hand to a session:

```python
async with nanopynix.rpc.Session(
    settings=NixSettings(max_jobs=4, trusted=True, pure_eval=True),
) as nix:
    ...
```

The global fields go to the process. The store, eval, fetch and flake fields
become that session's *defaults*, and anything passed to an individual store or
evaluator overrides them.

Each scope is also a class of its own, so a narrow parameter can ask for
exactly what it uses and still accept the catch-all.

```{eval-rst}
.. autoclass:: nanopynix.NixSettings
   :members:

.. autoclass:: nanopynix.NixGlobalSettings
   :members:

.. autoclass:: nanopynix.NixStoreDefaults
   :members:

.. autoclass:: nanopynix.NixEvaluatorSettings
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

.. autoclass:: nanopynix.SettingsProvenance
   :members:

.. autofunction:: nanopynix.list_settings_metadata
.. autofunction:: nanopynix.list_eval_settings_metadata
.. autofunction:: nanopynix.list_fetch_settings_metadata
.. autofunction:: nanopynix.list_flake_settings_metadata
.. autofunction:: nanopynix.check_settings_model_drift
.. autofunction:: nanopynix.check_all_settings_model_drift
```

## Provenance

With `load_config=True`, a session reads what `nix.conf` and the environment
supplied *before* it applies its own settings, so it can tell you which host
values it replaced. {class}`~nanopynix.SettingsProvenance` carries the answer,
and its `overridden_from_config` property is the interesting part: the settings
where the host asked for one value and this session uses another.

## Reading and writing the globals of a session

The global settings live in the process that holds Nix. For `nanopynix.rpc`
that process is the worker, not yours. A module-level setter therefore changes
the wrong copy, and the worker never learns. So the read and the write are both
methods of the session, on both engines:

```python
async with nanopynix.rpc.Session(settings=NixSettings(max_jobs=4)) as nix:
    await nix.settings()                       # every setting, with its value
    await nix.settings(overridden_only=True)   # only what something has set
    await nix.settings_provenance()            # host values against ours

    await nix.set_settings(NixGlobalSettings(max_jobs=8))
```

`set_settings` writes only the fields that you name. A field that you leave out
keeps its value, and this includes a field that has a default.

`set_settings` raises {class}`~nanopynix.exceptions.SettingNotLiveError` while a
store or an evaluator of the session is open. Nix builds both of them from the
globals as they stand, and neither looks again, so the write would not reach
what you hold. Close them, write, then open them again. The `nanopynix.inproc`
session has the same two methods, and answers the same, although the process
that they change is your own.

## Version gates

One model serves every Nix that nanopynix supports, so a field that does not
exist on all of them carries the versions that have it. `nix_version_min` is
the first version with the setting; `nix_version_removed` is the first version
without it. Rendering a field the running Nix does not have raises, and the
drift check skips it, so the same model is checked field for field against
each supported Nix.
